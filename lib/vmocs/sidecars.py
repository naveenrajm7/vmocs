#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

"""Planning and lifecycle management for VM-scoped host sidecars."""

from dataclasses import dataclass, field
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import time
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .error import HypervisorError, InvalidConfigError


_DEFAULT_VIRTIOFSD_CANDIDATES = (
    '/usr/libexec/virtiofsd',
    '/usr/lib/qemu/virtiofsd',
)
_DEFAULT_SWTPM = '/usr/bin/swtpm'
_DEFAULT_ROCJITSU = '/opt/rocjitsu/bin/rocjitsu'
_DEFAULT_ROCJITSU_CONFIG = (
    '/opt/rocjitsu/share/rocjitsu/configs/gfx1250_mi455x.json'
)
_DEFAULT_ERNIC = '/opt/rocm-ernic/bin/rocm-ernic'
_UNIX_SOCKET_PATH_MAX = 107


@dataclass(frozen=True)
class ProcessSpec:
    """Everything the manager needs to own one host process."""

    name: str
    argv: Tuple[str, ...]
    sockets: Tuple[str, ...]
    log_path: str
    startup_timeout: float = 60.0
    stop_timeout: float = 10.0
    env: Mapping[str, str] = field(default_factory=dict)
    directories: Tuple[str, ...] = field(default_factory=tuple)
    required_files: Tuple[str, ...] = field(default_factory=tuple)
    required_directories: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SidecarPlan:
    """Pure sidecar process and QEMU configuration."""

    kind: str
    process: ProcessSpec
    qemu_args: Tuple[str, ...]
    requires_shared_guest_memory: bool = False


@dataclass
class SidecarHandle:
    plan: SidecarPlan
    process: subprocess.Popen
    start_ticks: Optional[int]


def _find_virtiofsd(configured=None):
    if configured:
        return configured
    for path in _DEFAULT_VIRTIOFSD_CANDIDATES:
        if os.path.isfile(path):
            return path
    found = shutil.which('virtiofsd')
    if found:
        return found
    # Preserve a deterministic path in the plan. Preflight will produce the
    # user-facing error without mutating the host.
    return _DEFAULT_VIRTIOFSD_CANDIDATES[0]


def _sidecar_settings(cfg, name):
    settings = cfg.sidecars or {}
    value = settings.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidConfigError(f"sidecars.{name} must be a mapping")
    return value


def _timeouts(cfg, settings):
    common = cfg.sidecars or {}
    startup = settings.get(
        'startup-timeout', common.get('startup-timeout', 60))
    stop = settings.get('stop-timeout', common.get('stop-timeout', 10))
    try:
        startup = float(startup)
        stop = float(stop)
    except (TypeError, ValueError):
        raise InvalidConfigError('sidecar timeouts must be numeric')
    if startup <= 0 or stop < 0:
        raise InvalidConfigError(
            'sidecar startup timeout must be positive and stop timeout '
            'non-negative')
    return startup, stop


def _process_spec(cfg, name, argv, socket_path, runtime_dir,
                  directories=(), required_files=(), required_directories=(),
                  settings_key=None):
    config_key = settings_key or name.split('-', 1)[0]
    if not argv or not isinstance(argv[0], str) or not argv[0]:
        raise InvalidConfigError(
            f'sidecars.{config_key}.binary must be a non-empty string')
    settings = _sidecar_settings(cfg, config_key)
    startup_timeout, stop_timeout = _timeouts(cfg, settings)
    return ProcessSpec(
        name=name,
        argv=tuple(argv),
        sockets=(socket_path,),
        log_path=os.path.join(runtime_dir, 'sidecars', f'{name}.log'),
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        directories=tuple(directories),
        required_files=tuple(required_files),
        required_directories=tuple(required_directories),
    )


def _normalize_emulated_device(raw, index):
    if isinstance(raw, str):
        return {'type': raw}
    if not isinstance(raw, dict):
        raise InvalidConfigError(
            f'emulated-devices[{index}] must be a string or mapping')
    if 'type' not in raw:
        raise InvalidConfigError(
            f"emulated-devices[{index}] has no 'type'")
    return raw


def plan_sidecars(cfg, template, runtime_dir):
    """Resolve template features into sidecar plans without starting them."""
    plans: List[SidecarPlan] = []
    sidecar_dir = os.path.join(runtime_dir, 'sidecars')

    if template.tpm:
        settings = _sidecar_settings(cfg, 'swtpm')
        binary = settings.get('binary', _DEFAULT_SWTPM)
        state_dir = os.path.join(runtime_dir, 'tpm')
        socket_path = os.path.join(runtime_dir, 'tpm.sock')
        spec = _process_spec(
            cfg, 'swtpm',
            (binary, 'socket', '--tpmstate', f'dir={state_dir}',
             '--ctrl', f'type=unixio,path={socket_path}', '--tpm2'),
            socket_path, runtime_dir,
            directories=(sidecar_dir, state_dir),
        )
        plans.append(SidecarPlan(
            kind='swtpm', process=spec,
            qemu_args=(
                '-chardev', f'socket,id=chrtpm,path={socket_path}',
                '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm',
                '-device', 'tpm-tis,tpmdev=tpm0',
            ),
        ))

    mount_points = template.mount_points or {}
    if not isinstance(mount_points, dict):
        raise InvalidConfigError('mount-points must be a mapping')
    virtiofs_settings = _sidecar_settings(cfg, 'virtiofsd')
    virtiofs_binary = _find_virtiofsd(virtiofs_settings.get('binary'))
    for mount_index, (tag, raw_opts) in enumerate(mount_points.items()):
        opts = {'path': raw_opts} if isinstance(raw_opts, str) else raw_opts
        if not isinstance(opts, dict) or 'path' not in opts:
            raise InvalidConfigError(
                f"mount-points.{tag} must define a path")
        if opts.get('type', 'virtio-9p') != 'virtio-fs':
            continue
        if opts.get('readonly', False):
            raise HypervisorError('read-only mounts not supported with virtio-fs')
        host_path = opts['path']
        socket_path = os.path.join(runtime_dir, f'virtiofs_{mount_index}.sock')
        name = f'virtiofsd-{mount_index}'
        spec = _process_spec(
            cfg, name,
            (virtiofs_binary, '--rlimit-nofile', '0',
             '--socket-path', socket_path, '--sandbox', 'namespace',
             '--shared-dir', host_path),
            socket_path, runtime_dir,
            directories=(sidecar_dir,),
            required_directories=(host_path,),
        )
        plans.append(SidecarPlan(
            kind='virtiofsd', process=spec,
            qemu_args=(
                '-chardev',
                f'socket,id=char_fs_{mount_index},path={socket_path}',
                '-device',
                f'vhost-user-fs-pci,queue-size=1024,'
                f'chardev=char_fs_{mount_index},tag={tag}',
            ),
            requires_shared_guest_memory=True,
        ))

    counters: Dict[str, int] = {'rocjitsu': 0, 'rocm-ernic': 0}
    emulated_devices = getattr(template, 'emulated_devices', None) or []
    if not isinstance(emulated_devices, list):
        raise InvalidConfigError('emulated-devices must be a list')
    for request_index, raw_device in enumerate(emulated_devices):
        device = _normalize_emulated_device(raw_device, request_index)
        device_type = device['type']
        if not isinstance(device_type, str):
            raise InvalidConfigError(
                f"emulated-devices[{request_index}].type must be a string")
        if device_type not in counters:
            raise InvalidConfigError(
                f"unknown emulated device type '{device_type}'")
        instance = counters[device_type]
        counters[device_type] += 1

        vfu_dir = os.path.join(runtime_dir, 'vfu')
        if device_type == 'rocm-ernic':
            settings = _sidecar_settings(cfg, 'rocm-ernic')
            binary = settings.get('binary', _DEFAULT_ERNIC)
            socket_path = os.path.join(vfu_dir, f'ernic-{instance}.sock')
            name = f'rocm-ernic-{instance}'
            argv = (binary, '-s', socket_path)
            required_files = ()
        else:
            settings = _sidecar_settings(cfg, 'rocjitsu')
            binary = settings.get('binary', _DEFAULT_ROCJITSU)
            profiles = settings.get(
                'profiles', {'mi455x': _DEFAULT_ROCJITSU_CONFIG})
            if not isinstance(profiles, dict):
                raise InvalidConfigError('sidecars.rocjitsu.profiles must be a mapping')
            profile = device.get('profile', 'mi455x')
            if profile not in profiles:
                raise InvalidConfigError(
                    f"unknown rocJitsu profile '{profile}'")
            config_path = profiles[profile]
            if not isinstance(config_path, str) or not config_path:
                raise InvalidConfigError(
                    f"rocJitsu profile '{profile}' must resolve to a path")
            socket_path = os.path.join(vfu_dir, f'rocjitsu-{instance}.sock')
            name = f'rocjitsu-{instance}'
            argv = (binary, '--config', config_path,
                    '--vfio-socket', socket_path)
            required_files = (config_path,)

        spec = _process_spec(
            cfg, name, argv, socket_path, runtime_dir,
            directories=(sidecar_dir, vfu_dir),
            required_files=required_files,
            settings_key=device_type,
        )
        dev_json = json.dumps({
            'driver': 'vfio-user-pci',
            'id': f'vfio-user-{name}',
            'rombar': 0,
            'socket': {'path': socket_path, 'type': 'unix'},
        }, separators=(',', ':'))
        plans.append(SidecarPlan(
            kind=device_type, process=spec,
            qemu_args=('-device', dev_json),
            requires_shared_guest_memory=True,
        ))

    return plans


def has_vfio_user(plans):
    return any(plan.kind in ('rocjitsu', 'rocm-ernic') for plan in plans)


def _resolve_executable(binary):
    if os.path.sep in binary:
        return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def preflight_sidecars(plans):
    """Validate every process before starting the first one."""
    for plan in plans:
        spec = plan.process
        if not _resolve_executable(spec.argv[0]):
            raise HypervisorError(
                f"{spec.name} binary is missing or not executable: {spec.argv[0]}")
        for path in spec.required_files:
            if not os.path.isfile(path) or not os.access(path, os.R_OK):
                raise HypervisorError(
                    f'{spec.name} required file is missing or unreadable: {path}')
        for path in spec.required_directories:
            if not os.path.isdir(path) or not os.access(path, os.R_OK | os.X_OK):
                raise HypervisorError(
                    f'{spec.name} shared directory is missing or inaccessible: {path}')
        for socket_path in spec.sockets:
            if len(os.fsencode(socket_path)) > _UNIX_SOCKET_PATH_MAX:
                raise HypervisorError(
                    f'{spec.name} socket path is too long: {socket_path}')


def preflight_qemu(qemu_bin, template, plans, using_snapshot=False):
    """Validate vfio-user-specific QEMU requirements before sidecar startup."""
    if not has_vfio_user(plans):
        return
    if using_snapshot:
        raise HypervisorError(
            'snapshots are not yet supported with vfio-user emulated devices')
    if template.machine_type != 'q35':
        raise HypervisorError(
            'vfio-user emulated devices currently require machine-type: q35')
    resolved = _resolve_executable(qemu_bin)
    if not resolved:
        raise HypervisorError(
            f'QEMU binary is missing or not executable: {qemu_bin}')
    try:
        result = subprocess.run(
            [resolved, '-device', 'help'], capture_output=True, text=True,
            timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HypervisorError(f'cannot probe QEMU binary {qemu_bin}: {exc}')
    output = (result.stdout or '') + (result.stderr or '')
    if result.returncode != 0:
        detail = output.strip().splitlines()
        suffix = f': {detail[-1]}' if detail else ''
        raise HypervisorError(f'QEMU capability probe failed for {qemu_bin}{suffix}')
    if 'vfio-user-pci' not in output:
        raise HypervisorError(
            f'QEMU binary does not support vfio-user-pci: {qemu_bin}')


def _socket_ready(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _tail(path, limit=20):
    try:
        with open(path, 'r', errors='replace') as stream:
            lines = stream.readlines()
        return ''.join(lines[-limit:]).strip()
    except OSError:
        return ''


def read_boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id') as stream:
            return stream.read().strip()
    except OSError:
        return None


def process_start_ticks(pid):
    """Return Linux /proc starttime for PID, or None if it is not alive."""
    try:
        with open(f'/proc/{pid}/stat') as stream:
            data = stream.read()
    except OSError:
        return None
    close_paren = data.rfind(')')
    if close_paren < 0:
        return None
    fields = data[close_paren + 2:].split()
    try:
        return int(fields[19])  # field 22 overall; fields starts at field 3
    except (IndexError, ValueError):
        return None


class SidecarManager:
    """Own the child processes for one VM launch."""

    def __init__(self, plans: Sequence[SidecarPlan]):
        self.plans = list(plans)
        self.handles: List[SidecarHandle] = []

    def start_all(self):
        preflight_sidecars(self.plans)
        try:
            for plan in self.plans:
                self._start(plan)
            self._wait_ready()
        except Exception:
            self.stop_all()
            raise

    def _start(self, plan):
        spec = plan.process
        for directory in spec.directories:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        for socket_path in spec.sockets:
            if os.path.lexists(socket_path):
                if os.path.isdir(socket_path) and not os.path.islink(socket_path):
                    raise HypervisorError(
                        f'{spec.name} socket path is a directory: {socket_path}')
                os.unlink(socket_path)
        os.makedirs(os.path.dirname(spec.log_path), mode=0o700, exist_ok=True)
        env = os.environ.copy()
        env.update(spec.env)
        logging.info('Starting sidecar %s', spec.name)
        try:
            with open(spec.log_path, 'ab', buffering=0) as log_file:
                process = subprocess.Popen(
                    list(spec.argv), close_fds=True, start_new_session=True,
                    stdout=log_file, stderr=subprocess.STDOUT, env=env)
        except OSError as exc:
            raise HypervisorError(f'failed to start {spec.name}: {exc}')
        self.handles.append(SidecarHandle(
            plan=plan, process=process,
            start_ticks=process_start_ticks(process.pid)))

    def _wait_ready(self):
        if not self.handles:
            return
        started = time.monotonic()
        while True:
            all_ready = True
            now = time.monotonic()
            for handle in self.handles:
                spec = handle.plan.process
                returncode = handle.process.poll()
                if returncode is not None:
                    detail = _tail(spec.log_path)
                    suffix = f'\n{detail}' if detail else ''
                    raise HypervisorError(
                        f'{spec.name} exited before readiness '
                        f'(status {returncode}); see {spec.log_path}{suffix}')
                if not all(_socket_ready(path) for path in spec.sockets):
                    all_ready = False
                    if now - started > spec.startup_timeout:
                        detail = _tail(spec.log_path)
                        suffix = f'\n{detail}' if detail else ''
                        raise HypervisorError(
                            f'{spec.name} socket readiness timed out; '
                            f'see {spec.log_path}{suffix}')
            if all_ready:
                return
            time.sleep(0.1)

    def metadata(self):
        items = []
        for handle in self.handles:
            spec = handle.plan.process
            items.append({
                'name': spec.name,
                'kind': handle.plan.kind,
                'pid': handle.process.pid,
                'pgid': handle.process.pid,
                'start_ticks': handle.start_ticks,
                'sockets': list(spec.sockets),
                'log': spec.log_path,
            })
        return items

    def failure(self):
        """Return (name, status) for the first dead sidecar, if any."""
        for handle in self.handles:
            returncode = handle.process.poll()
            if returncode is not None:
                return handle.plan.process.name, returncode
        return None

    def stop_all(self):
        live = [handle for handle in reversed(self.handles)
                if handle.process.poll() is None]
        for handle in live:
            try:
                os.killpg(handle.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + max(
            (handle.plan.process.stop_timeout for handle in live), default=0)
        while live and time.monotonic() < deadline:
            live = [handle for handle in live if handle.process.poll() is None]
            if live:
                time.sleep(0.1)
        for handle in live:
            try:
                os.killpg(handle.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for handle in self.handles:
            try:
                handle.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _persisted_process_matches(item, boot_id):
    if not boot_id or boot_id != read_boot_id():
        return False
    pid = item.get('pid')
    expected = item.get('start_ticks')
    if not isinstance(pid, int) or expected is None:
        return False
    return process_start_ticks(pid) == expected


def stop_persisted_sidecars(meta, timeout=10):
    """Best-effort recovery cleanup using verified manifest identities."""
    items = list(reversed(meta.get('sidecars') or []))
    verified = [item for item in items
                if _persisted_process_matches(item, meta.get('boot_id'))]
    for item in verified:
        try:
            os.killpg(item.get('pgid', item['pid']), signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while verified and time.monotonic() < deadline:
        verified = [item for item in verified
                    if _persisted_process_matches(item, meta.get('boot_id'))]
        if verified:
            time.sleep(0.1)
    for item in verified:
        try:
            os.killpg(item.get('pgid', item['pid']), signal.SIGKILL)
        except ProcessLookupError:
            pass
