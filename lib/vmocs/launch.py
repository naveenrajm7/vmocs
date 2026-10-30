#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  VM lifecycle: launch, SSH wait, teardown.
#  QEMU is fork/exec'd so it inherits the caller's cgroup automatically
#  (same principle as pcocc Hypervisor.py:1664-1674).

import base64
import json
import logging
import os
import pwd
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from .checkpoint import (
    checkpoint_path,
    create_checkpoint,
    load_checkpoint,
    prepare_checkpoint_destination,
)
from .error import HypervisorError
from .image import VMImage
from .hypervisor import build_qemu_cmdline, _make_cloud_init_iso, _find_free_port
from .keys import VAGRANT_KEY
from .monitor import wait_for_monitor
from .sidecars import (
    SidecarManager,
    plan_sidecars,
    preflight_qemu,
    read_boot_id,
    stop_persisted_sidecars,
)


def _current_host_user():
    """Return the submitter identity that QEMU and shared files run as."""
    uid = os.getuid()
    gid = os.getgid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise HypervisorError(f'no passwd entry for host uid {uid}') from exc
    if (len(username) > 32 or not re.fullmatch(
            r'[A-Za-z_][A-Za-z0-9_.-]*\$?', username)):
        raise HypervisorError(
            f'host username cannot be created safely in the guest: {username!r}')
    return {'name': username, 'uid': uid, 'gid': gid}


def _write_metadata(runtime_dir, meta, missing_ok=False):
    """Atomically persist public VM state."""
    path = os.path.join(runtime_dir, 'vm.json')
    temp_path = path + '.tmp'
    public = {key: value for key, value in meta.items()
              if not key.startswith('_')}
    try:
        with open(temp_path, 'w') as stream:
            json.dump(public, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        descriptor = os.open(
            runtime_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except FileNotFoundError:
        if not missing_ok:
            raise
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        return False


def is_vm_stopping(runtime_dir):
    """Return whether another vmocs process has begun an explicit stop."""
    try:
        with open(os.path.join(runtime_dir, 'vm.json')) as stream:
            return json.load(stream).get('state') == 'stopping'
    except (OSError, ValueError, TypeError):
        return False


def stop_vm_sidecars(meta):
    """Stop live launch handles or recover sidecars from persisted metadata."""
    manager = meta.get('_sidecar_manager')
    if manager is not None:
        manager.stop_all()
    else:
        stop_persisted_sidecars(meta)


def generate_ssh_keypair(runtime_dir):
    """Generate an ED25519 keypair in runtime_dir. Return (privkey_path, pubkey_str)."""
    key_path = os.path.join(runtime_dir, 'id_ed25519')
    subprocess.check_call(
        ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', key_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(key_path + '.pub') as f:
        pubkey = f.read().strip()
    return key_path, pubkey


def _is_per_launch_ssh_key(insert_key, using_snapshot, boot_mode):
    """Return whether this launch creates and installs a fresh SSH key."""
    return bool(
        insert_key
        and not (using_snapshot and boot_mode == 'cloud-init'))


def _ssh_host_key_options(known_hosts_path=None):
    """Return strict pinned-host options, or legacy permissive options."""
    if known_hosts_path:
        return [
            '-o', 'StrictHostKeyChecking=yes',
            '-o', f'UserKnownHostsFile={known_hosts_path}',
        ]
    return [
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
    ]


def _ssh_identity_options(key_path):
    """Select exactly one SSH identity instead of consulting an agent."""
    return [
        '-F', '/dev/null',
        '-i', key_path,
        '-o', 'IdentitiesOnly=yes',
        '-o', 'IdentityAgent=none',
        '-o', 'CertificateFile=none',
        '-o', 'PreferredAuthentications=publickey',
        '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no',
        '-o', 'GSSAPIAuthentication=no',
        '-o', 'ControlMaster=no',
        '-o', 'ControlPath=none',
    ]


def _ssh_key_authenticates(host, port, key_path, ssh_user='root',
                           known_hosts_path=None, timeout=10):
    """Return whether KEY_PATH alone authenticates to the guest."""
    command = [
        'ssh',
        *_ssh_identity_options(key_path),
        *_ssh_host_key_options(known_hosts_path),
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=2',
        '-T', '-p', str(port), f'{ssh_user}@{host}', 'exit 0',
    ]
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _verify_windows_ssh_handoff(host, port, ephemeral_key_path,
                                ssh_user, known_hosts_path):
    """Prove new-key success, bootstrap-key failure, then new-key success."""
    authenticate = lambda key: _ssh_key_authenticates(
        host, port, key, ssh_user, known_hosts_path=known_hosts_path)
    if not authenticate(ephemeral_key_path):
        raise HypervisorError(
            'Windows ephemeral SSH key could not be verified after reboot')
    if authenticate(VAGRANT_KEY):
        raise HypervisorError(
            'Windows bootstrap SSH key is still accepted after rotation')
    if not authenticate(ephemeral_key_path):
        raise HypervisorError(
            'Windows ephemeral SSH key could not be reconfirmed '
            'after bootstrap-key revocation')


def _is_guest_reboot_qmp_event(event):
    """Match QMP's two documented representations of a guest reboot."""
    if not isinstance(event, dict):
        return False
    if event.get('event') not in ('RESET', 'SHUTDOWN'):
        return False
    data = event.get('data')
    return isinstance(data, dict) and data.get('reason') == 'guest-reset'


def _is_host_reset_qmp_event(event):
    """Match the RESET emitted for a host QMP system_reset command."""
    if not isinstance(event, dict) or event.get('event') != 'RESET':
        return False
    data = event.get('data')
    return (isinstance(data, dict)
            and data.get('reason') == 'host-qmp-system-reset')


def _capture_ssh_host_keys(host, port, runtime_dir, timeout):
    """Capture the first SSH host keys from the running QEMU forward (TOFU)."""
    known_hosts_path = os.path.join(runtime_dir, 'ssh_known_hosts')
    expected_prefix = f'[{host}]:{port} '
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ['ssh-keyscan', '-T', '2', '-p', str(port), host],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            lines = [
                line for line in result.stdout.decode(
                    'ascii', errors='strict').splitlines()
                if line and not line.startswith('#')
            ]
            if (lines and all(
                    line.startswith(expected_prefix)
                    and len(line.split()) >= 3 for line in lines)):
                with open(known_hosts_path, 'x', encoding='ascii') as stream:
                    stream.write('\n'.join(lines) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(known_hosts_path, 0o600)
                return known_hosts_path
        time.sleep(2)
    raise HypervisorError(
        f'VM SSH host key not available after {timeout}s')


def _windows_key_rotation_script():
    """Replace the bootstrap key and schedule a reboot to revoke sessions."""
    return r"""
$ErrorActionPreference='Stop'
$source=$null
$stream=$null
try{
$source=[Console]::OpenStandardInput()
$memory=New-Object IO.MemoryStream
$buffer=New-Object byte[] 4096
$total=0
while(($count=$source.Read($buffer,0,$buffer.Length)) -gt 0){
$total+=$count
if($total -gt 16384){throw 'oversize'}
$memory.Write($buffer,0,$count)
}
$source.Dispose();$source=$null
$utf8=New-Object Text.UTF8Encoding($false,$true)
$key=$utf8.GetString($memory.ToArray()).TrimEnd([char[]]@([char]13,[char]10))
if($key -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [\x20-\x7e]*)?$'){throw 'key'}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=New-Object Security.Principal.WindowsPrincipal($identity)
if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
$target=[IO.Path]::Combine($env:ProgramData,'ssh','administrators_authorized_keys')
}else{
$target=[IO.Path]::Combine([Environment]::GetFolderPath('UserProfile'),'.ssh','authorized_keys')
}
$parent=[IO.Path]::GetDirectoryName($target)
if(-not [IO.File]::Exists($target)){throw 'missing'}
if(([IO.File]::GetAttributes($parent) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'reparse'}
if(([IO.File]::GetAttributes($target) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'reparse'}
$bytes=[Text.Encoding]::ASCII.GetBytes($key+"`n")
$stream=New-Object IO.FileStream($target,[IO.FileMode]::Truncate,[IO.FileAccess]::Write,[IO.FileShare]::None)
$stream.Write($bytes,0,$bytes.Length)
$stream.Flush($true)
$stream.Dispose();$stream=$null
$actual=[IO.File]::ReadAllText($target,[Text.Encoding]::ASCII).TrimEnd([char[]]@([char]13,[char]10))
if($actual -cne $key){throw 'verify'}
[Console]::Out.WriteLine('VMOCS_KEY_ROTATED_REBOOT_SCHEDULED')
[Console]::Out.Flush()
$shutdown=[IO.Path]::Combine($env:SystemRoot,'System32','shutdown.exe')
& $shutdown /r /t 5 /f
if($LASTEXITCODE -ne 0){throw 'reboot'}
exit 0
}catch{
try{if($stream){$stream.Dispose()}}catch{}
try{if($source){$source.Dispose()}}catch{}
[Console]::Error.WriteLine('VMoCS SSH key rotation failed.')
exit 88
}
""".strip()


def _windows_powershell_command(script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return subprocess.list2cmdline((
        'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
        '-EncodedCommand', encoded))


def _rotate_windows_vagrant_key(host, port, pubkey, timeout,
                                ssh_user='vagrant', known_hosts_path=None,
                                ephemeral_key_path=None,
                                reboot_observed=None, qmp_barrier=None,
                                force_reset=None, forced_reset_observed=None):
    """Replace the Windows bootstrap key, reboot, and verify the new key."""
    if not ephemeral_key_path:
        raise HypervisorError(
            'Windows Vagrant key rotation requires an ephemeral key')
    if reboot_observed is None:
        raise HypervisorError(
            'Windows Vagrant key rotation requires QMP reboot observation')
    if qmp_barrier is None:
        raise HypervisorError(
            'Windows Vagrant key rotation requires a QMP ordering barrier')
    command = [
        'ssh', *_ssh_identity_options(VAGRANT_KEY),
        *_ssh_host_key_options(known_hosts_path),
        '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=2',
        '-T', '-p', str(port), f'{ssh_user}@{host}',
        _windows_powershell_command(_windows_key_rotation_script()),
    ]
    deadline = time.monotonic() + timeout
    # Discard boot-time events before invoking the fixed rotation command.
    # A second, synchronous QMP barrier after its marker establishes the
    # security-relevant reboot boundary.
    reboot_observed.clear()
    while time.monotonic() < deadline:
        remaining = max(0, deadline - time.monotonic())
        try:
            result = subprocess.run(
                command, input=(pubkey + '\n').encode('ascii'),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=max(0.1, min(15, remaining)))
        except subprocess.TimeoutExpired as exc:
            # subprocess.run() kills and reaps ssh on timeout but preserves
            # output received before the disconnect. The fixed script flushes
            # its marker after replacing the key and before requesting reboot.
            result = exc
        except OSError:
            result = None
        stdout = getattr(result, 'stdout', b'') or b''
        if isinstance(stdout, str):
            stdout = stdout.encode('utf-8', errors='replace')
        if b'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED' in stdout:
            # The script schedules its reboot five seconds after flushing the
            # marker. Drain all earlier QMP traffic, then require a newer guest
            # reset. If the reboot races ahead of this barrier, fail closed.
            qmp_barrier()
            reboot_observed.clear()
            break
        if reboot_observed.is_set():
            # A hard reboot can sever OpenSSH before its channel delivers the
            # already-flushed marker. The event is newer than clear() above;
            # accept this path only after the per-launch key authenticates.
            # The caller subsequently proves old-key failure and new-key
            # success again before allowing sensitive guest staging.
            logging.info(
                'Windows reboot observed without SSH rotation marker; '
                'verifying the ephemeral key')
            remaining = max(0, deadline - time.monotonic())
            if not wait_for_ssh(
                    host, port, ephemeral_key_path, remaining, ssh_user,
                    authenticate=True, known_hosts_path=known_hosts_path):
                raise HypervisorError(
                    'Windows ephemeral SSH key was not accepted after the '
                    'observed key-rotation reboot')
            if force_reset is None or forced_reset_observed is None:
                raise HypervisorError(
                    'lost Windows key-rotation marker requires a '
                    'host-controlled QMP reset')
            # The guest reset may have been delayed or unrelated to this SSH
            # attempt. After proving the new key was installed, issue a fresh
            # host-controlled reset and observe its distinct QMP reason. This
            # causally revokes every session authenticated with the old key.
            forced_reset_observed.clear()
            qmp_barrier()
            forced_reset_observed.clear()
            force_reset()
            remaining = max(0, deadline - time.monotonic())
            if not forced_reset_observed.wait(remaining):
                raise HypervisorError(
                    'QEMU did not observe the host-controlled Windows '
                    'SSH handoff reset')
            remaining = max(0, deadline - time.monotonic())
            if not wait_for_ssh(
                    host, port, ephemeral_key_path, remaining, ssh_user,
                    authenticate=True, known_hosts_path=known_hosts_path):
                raise HypervisorError(
                    'Windows ephemeral SSH key was not accepted after the '
                    'host-controlled SSH handoff reset')
            return
        returncode = getattr(result, 'returncode', None)
        if returncode is not None and returncode != 255:
            raise HypervisorError('Windows Vagrant key rotation failed')
        remaining = max(0, deadline - time.monotonic())
        if remaining:
            time.sleep(min(2, remaining))
    else:
        raise HypervisorError(
            f'vagrant key rotation timed out after {timeout}s')

    remaining = max(0, deadline - time.monotonic())
    if not reboot_observed.wait(remaining):
        raise HypervisorError(
            'QEMU did not observe the Windows SSH handoff guest-reset event')
    remaining = max(0, deadline - time.monotonic())
    if not wait_for_ssh(
            host, port, ephemeral_key_path, remaining, ssh_user,
            authenticate=True, known_hosts_path=known_hosts_path):
        raise HypervisorError(
            'Windows ephemeral SSH key was not accepted after reboot')


def _rotate_vagrant_key(host, port, pubkey, timeout, ssh_user='vagrant',
                        known_hosts_path=None, command_style='posix',
                        ephemeral_key_path=None, reboot_observed=None,
                        qmp_barrier=None, force_reset=None,
                        forced_reset_observed=None):
    """Install an ephemeral key, revoking the well-known Vagrant key.

    POSIX guests use scp. Windows guests replace the active authorized-keys
    file through a fixed PowerShell command with the public key on SSH stdin.
    """
    if command_style == 'windows':
        return _rotate_windows_vagrant_key(
            host, port, pubkey, timeout, ssh_user, known_hosts_path,
            ephemeral_key_path, reboot_observed, qmp_barrier, force_reset,
            forced_reset_observed)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.pub', delete=False) as f:
        f.write(pubkey + '\n')
        tmp_pub = f.name
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ret = subprocess.call(
                ['scp',
                 *_ssh_identity_options(VAGRANT_KEY),
                 *_ssh_host_key_options(known_hosts_path),
                 '-o', 'BatchMode=yes',
                 '-o', 'ConnectTimeout=2',
                 '-P', str(port),
                 tmp_pub,
                 f'{ssh_user}@{host}:.ssh/authorized_keys'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            if ret == 0:
                return
            time.sleep(2)
        raise HypervisorError(f'vagrant key rotation timed out after {timeout}s')
    finally:
        os.unlink(tmp_pub)


def wait_for_ssh(host, port, key_path, timeout, ssh_user='root',
                 stop_requested=None, authenticate=False,
                 known_hosts_path=None):
    """Poll for an SSH banner and, when requested, a successful key login."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_requested and stop_requested():
            return False
        try:
            with socket.create_connection((host, port), timeout=2) as s:
                if s.recv(256).startswith(b'SSH-'):
                    if not authenticate:
                        return True
                    remaining = max(0.1, deadline - time.monotonic())
                    if key_path and _ssh_key_authenticates(
                            host, port, key_path, ssh_user,
                            known_hosts_path=known_hosts_path,
                            timeout=min(10, remaining)):
                        return True
        except OSError:
            pass
        time.sleep(2)
    return False


def launch_vm(cfg, template, cores, memory_mb, job_id=None, pci_devices=(),
              supervised=False, save_path=None, resume_path=None,
              checkpoint_blocked_reason=None, pin_ssh_host_key=False):
    """
    Launch a VM and wait for SSH. Returns a dict with runtime metadata.

    Steps (mirrors pcocc Hypervisor.py:1328-1710):
      1. Create per-job runtime dir
      2. Create COW overlay over base image
      3. Generate SSH keypair
      4. Allocate SSH port
      5. Build QEMU cmdline
      6. fork/exec QEMU (inherits caller's cgroup)
      7. Connect QMP, bind vCPUs, cont()
      8. Poll SSH until ready
      9. Write vm.json metadata
    """
    if job_id is None:
        job_id = os.getpid()

    if save_path and checkpoint_blocked_reason:
        raise HypervisorError(
            f'checkpoint is disabled: {checkpoint_blocked_reason}')
    if save_path:
        save_path = prepare_checkpoint_destination(save_path)
    if resume_path:
        resume_path = checkpoint_path(resume_path)
        _, resume_disk = load_checkpoint(
            resume_path, expected_template=template.name)
    else:
        resume_disk = None

    # Detect snapshot: template.snapshot points to a snapshot dir
    snap_dir = template.snapshot
    using_snapshot = bool(not resume_disk and snap_dir and os.path.isfile(
        os.path.join(snap_dir, 'memory')))
    boot_mode = template.boot_mode
    ephemeral_ssh_key = _is_per_launch_ssh_key(
        template.insert_key, using_snapshot, boot_mode)
    if (pin_ssh_host_key and template.ssh_command_style == 'windows'
            and boot_mode != 'vagrant'):
        raise HypervisorError(
            'Windows environment forwarding currently requires '
            'boot-mode: vagrant for a verified SSH credential handoff')
    if pin_ssh_host_key and not ephemeral_ssh_key:
        raise HypervisorError(
            'Windows environment forwarding requires a fresh per-launch '
            'SSH key and does not support cloud-init memory snapshots')

    runtime_dir = os.path.join(cfg.runtime_dir, str(job_id))
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    if os.path.exists(os.path.join(runtime_dir, 'vm.json')):
        raise HypervisorError(
            f'runtime already exists for job {job_id}: {runtime_dir}')

    # Resolve base image: a cold checkpoint overrides the template's boot
    # snapshot but keeps the template's current device/sidecar configuration.
    if resume_disk:
        base_image = resume_disk
    elif using_snapshot:
        base_image = os.path.join(snap_dir, 'disk.qcow2')
    elif template.image_dir:
        import glob
        candidates = glob.glob(os.path.join(template.image_dir, '*.qcow2'))
        if not candidates:
            raise HypervisorError(f'no qcow2 image found in {template.image_dir}')
        base_image = candidates[0]
    elif template.image:
        base_image = template.image
    else:
        raise HypervisorError('template has no image or image-dir')
    base_image = os.path.abspath(base_image)

    # 1. COW overlay
    logging.info('Creating disk overlay...')
    overlay = os.path.join(runtime_dir, 'disk.qcow2')
    VMImage.create_cow_overlay(base_image, overlay)

    # 2. SSH keypair — cloud-init memory snapshots reuse their snapshot key.
    host_user = None
    if template.host_user_identity:
        if boot_mode != 'cloud-init':
            raise HypervisorError(
                'host-user-identity requires boot-mode: cloud-init')
        if not template.insert_key:
            raise HypervisorError(
                'host-user-identity requires insert-key: true')
        host_user = _current_host_user()
        ssh_user = host_user['name']
    else:
        ssh_user = template.ssh_user
    snapshot_mem = os.path.join(snap_dir, 'memory') if using_snapshot else None

    # boot-mode + insert-key decide how (or whether) the key reaches the VM
    insert_key = template.insert_key
    pubkey = None

    if not insert_key:
        # Mirrors Vagrant's config.ssh.insert_key = false:
        # use the pre-installed key from the image, no ephemeral keypair needed.
        key_path = template.ssh_key  # None is fine — user relies on their own SSH config
        cloud_init_iso = None
    elif using_snapshot and boot_mode == 'cloud-init':
        key_path = os.path.join(snap_dir, 'id_ed25519')
        with open(key_path + '.pub') as f:
            pubkey = f.read().strip()
        cloud_init_iso = None  # set below
    else:
        key_path, pubkey = generate_ssh_keypair(runtime_dir)

    if insert_key:
        if boot_mode == 'cloud-init':
            cloud_init_iso = _make_cloud_init_iso(
                runtime_dir, pubkey, ssh_user=ssh_user,
                host_user=host_user)
        elif boot_mode == 'vagrant':
            cloud_init_iso = None   # key is rotated post-boot via _rotate_vagrant_key
        else:
            raise HypervisorError(f'unknown boot-mode: {boot_mode!r}')

    # 3. SSH port
    port_range = cfg.network.get('ssh-port-range', [60222, 60322])
    ssh_port = _find_free_port(port_range)

    qmp_socket = os.path.join(runtime_dir, 'qmp.sock')
    qemu_bin = template.qemu_bin or cfg.qemu_bin

    # Templates refine the site defaults without inheriting the management
    # port allocator, which is consumed by vmocs rather than QEMU.
    network = dict(cfg.network or {})
    network.pop('ssh-port-range', None)
    template_network = template.network or {}
    if not isinstance(template_network, dict):
        raise HypervisorError("template 'network' must be a mapping")
    network.update(template_network)

    # UEFI NVRAM: copy vars template into runtime_dir so each job gets an isolated store
    firmware_vars = None
    if template.firmware:
        vars_template = template.firmware_vars_template
        if not vars_template:
            raise HypervisorError(
                f"template '{template.name}' sets 'firmware' but has no 'firmware-vars-template'")
        import shutil
        firmware_vars = os.path.join(runtime_dir, 'nvram.fd')
        shutil.copy2(vars_template, firmware_vars)

    # Resolve and validate every host sidecar before the first process starts.
    sidecar_plans = plan_sidecars(cfg, template, runtime_dir)
    preflight_qemu(qemu_bin, template, sidecar_plans,
                   using_snapshot=using_snapshot)

    # 4. Build QEMU cmdline
    cmd = build_qemu_cmdline(
        qemu_bin=qemu_bin,
        template=template,
        cores=cores,
        memory_mb=memory_mb,
        disk_path=overlay,
        runtime_dir=runtime_dir,
        ssh_port=ssh_port,
        qmp_socket=qmp_socket,
        cloud_init_iso=cloud_init_iso,
        snapshot_mem=snapshot_mem,
        firmware_vars=firmware_vars,
        pci_devices=pci_devices,
        extra_disks=template.extra_disks or [],
        sidecar_plans=sidecar_plans,
        network=network,
        supervised=supervised,
    )

    ssh_timeout = template.ssh_timeout
    known_hosts_path = None
    meta = {
        'job_id': job_id,
        'pid': None,
        'boot_mode': boot_mode,
        'ssh_port': ssh_port,
        'ssh_user': ssh_user,
        'ssh_command_style': template.ssh_command_style,
        'key_path': key_path,
        'ephemeral_ssh_key': ephemeral_ssh_key,
        'ssh_host_key_pinned': False,
        'secure_ssh_handoff_verified': False,
        'known_hosts_path': None,
        'overlay': overlay,
        'base_image': base_image,
        'qmp_socket': qmp_socket,
        'runtime_dir': runtime_dir,
        'template': template.name,
        'cores': cores,
        'memory_mb': memory_mb,
        'network': network,
        'pci_devices': list(pci_devices),
        'save_path': save_path,
        'resume_path': resume_path,
        'checkpoint_id': str(uuid.uuid4()) if save_path else None,
        'checkpoint_blocked_reason': checkpoint_blocked_reason,
        'ssh_timeout': ssh_timeout,
        'state': 'starting',
        'boot_id': read_boot_id(),
        'sidecars': [],
    }
    _write_metadata(runtime_dir, meta)

    manager = SidecarManager(sidecar_plans)
    qemu_pid = None
    mon = None
    try:
        manager.start_all()
        meta['sidecars'] = manager.metadata()
        _write_metadata(runtime_dir, meta)

        # 5. fork/exec QEMU — child inherits our cgroup (pcocc:1664-1674)
        logging.info('Starting QEMU...')
        qemu_pid = os.fork()
        if qemu_pid == 0:
            os.setpgid(0, 0)
            logfd = os.open(os.path.join(runtime_dir, 'qemu.log'),
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(logfd, 1)
            os.dup2(logfd, 2)
            try:
                os.execvp(cmd[0], cmd)
            except OSError as exc:
                os.write(2, f'QEMU exec failed: {exc}\n'.encode())
                os._exit(127)

        meta['pid'] = qemu_pid
        meta['state'] = 'qemu-starting'
        _write_metadata(runtime_dir, meta)

        # 6. Connect QMP and start VM (pcocc:1676-1723)
        logging.info('Waiting for QEMU monitor...')
        try:
            mon = wait_for_monitor(qmp_socket, timeout=30)
        except HypervisorError as exc:
            log_path = os.path.join(runtime_dir, 'qemu.log')
            try:
                with open(log_path, errors='replace') as stream:
                    qemu_log = stream.read().strip()
            except OSError:
                qemu_log = ''
            if qemu_log:
                raise HypervisorError(
                    f'{exc}; QEMU startup log: {qemu_log[-4096:]}') from exc
            raise

        reboot_observed = None
        forced_reset_observed = None
        if (boot_mode == 'vagrant' and insert_key
                and template.ssh_command_style == 'windows'):
            reboot_observed = threading.Event()
            forced_reset_observed = threading.Event()

            def _observe_rotation_reboot(event):
                if _is_guest_reboot_qmp_event(event):
                    reboot_observed.set()
                if _is_host_reset_qmp_event(event):
                    forced_reset_observed.set()
                return True

            mon.add_event_handler(_observe_rotation_reboot)

        # Snapshot restore: wait for incoming migration to finish, then cont
        if using_snapshot:
            while mon.query_status() == 'inmigrate':
                time.sleep(1)
        mon.cont()
        if pin_ssh_host_key:
            known_hosts_path = _capture_ssh_host_keys(
                '127.0.0.1', ssh_port, runtime_dir, ssh_timeout)
            if mon.query_status() != 'running':
                raise HypervisorError(
                    'QEMU stopped while the SSH host key was being pinned')
            meta['ssh_host_key_pinned'] = True
            meta['known_hosts_path'] = known_hosts_path
            _write_metadata(runtime_dir, meta)
        logging.info('VM booting, waiting for SSH...')

        # Vagrant: replace the well-known key with the ephemeral key.
        if boot_mode == 'vagrant' and insert_key:
            _rotate_vagrant_key(
                '127.0.0.1', ssh_port, pubkey, ssh_timeout, ssh_user,
                known_hosts_path=known_hosts_path,
                command_style=template.ssh_command_style,
                ephemeral_key_path=key_path,
                reboot_observed=reboot_observed,
                qmp_barrier=mon.query_status,
                force_reset=mon.reset,
                forced_reset_observed=forced_reset_observed)

        mon.close()  # Release QMP connection — QEMU serves one client at a time
        mon = None

        # 7. Wait for SSH (ephemeral key for both modes)
        if not wait_for_ssh(
                '127.0.0.1', ssh_port, key_path, ssh_timeout, ssh_user,
                authenticate=insert_key or host_user is not None,
                known_hosts_path=known_hosts_path):
            raise HypervisorError(f'VM SSH not ready after {ssh_timeout}s')

        if (pin_ssh_host_key and boot_mode == 'vagrant'
                and template.ssh_command_style == 'windows'):
            _verify_windows_ssh_handoff(
                '127.0.0.1', ssh_port, key_path, ssh_user,
                known_hosts_path)
            meta['secure_ssh_handoff_verified'] = True
            _write_metadata(runtime_dir, meta)

        # 8. Write final metadata and retain live manager handles in memory.
        meta['state'] = 'running'
        _write_metadata(runtime_dir, meta)
        meta['_sidecar_manager'] = manager
        return meta
    except Exception:
        if mon is not None:
            mon.close()
        if qemu_pid is not None:
            _kill_qemu(qemu_pid, timeout=2)
            try:
                os.waitpid(qemu_pid, 0)
            except ChildProcessError:
                pass
        manager.stop_all()
        meta['state'] = 'failed'
        meta['sidecars'] = manager.metadata()
        _write_metadata(runtime_dir, meta)
        raise


def _kill_qemu(pid, timeout=10):
    """SIGTERM → wait → SIGKILL."""
    try:
        os.kill(pid, 15)
        for _ in range(timeout):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _graceful_shutdown(qmp_socket, pid, timeout=60):
    """Send ACPI powerdown via QMP so the guest flushes filesystems cleanly.

    Falls back to SIGTERM if the QMP socket is gone or the guest doesn't
    shut down within timeout seconds.
    """
    try:
        from .monitor import QemuMonitor
        mon = QemuMonitor(qmp_socket)
        mon._validate('{"execute": "system_powerdown"}\n\n')
        mon.close()
    except Exception:
        _kill_qemu(pid)
        return False

    # Wait for QEMU to exit naturally (guest runs shutdown, syncs filesystems)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # QEMU exited cleanly

    # Guest didn't shut down in time — force kill
    _kill_qemu(pid)
    return False


def teardown_vm(job_id, runtime_dir=None, runtime_base='/var/run/vmocs',
                save_path=None):
    """Stop QEMU, optionally publish a cold checkpoint, then clean up.

    Args:
        save_path: Optional checkpoint directory.  Persisted launch-time save
                   intent is used when this is omitted, which lets the Slurm
                   exit hook recover an interrupted supervisor.
    """
    if runtime_dir is None:
        runtime_dir = os.path.join(runtime_base, str(job_id))

    vm_json = os.path.join(runtime_dir, 'vm.json')
    if not os.path.exists(vm_json):
        raise HypervisorError(f'no vm.json in {runtime_dir}')

    with open(vm_json) as f:
        meta = json.load(f)

    requested_save = save_path or meta.get('save_path')
    if requested_save and meta.get('checkpoint_blocked_reason'):
        raise HypervisorError(
            f'checkpoint is disabled: {meta["checkpoint_blocked_reason"]}')
    if save_path:
        requested_save = prepare_checkpoint_destination(save_path)
        meta['save_path'] = requested_save
        meta['checkpoint_id'] = str(uuid.uuid4())

    # Publish intent before QEMU closes its sidecar connections. A blocking
    # supervisor can then distinguish expected shutdown exits from failures.
    meta['state'] = 'stopping'
    if not _write_metadata(runtime_dir, meta, missing_ok=True):
        if requested_save:
            raise HypervisorError(
                'VM runtime disappeared before the checkpoint could be saved')
        return meta

    pid = meta.get('pid')

    remove_runtime = False
    try:
        if requested_save and pid is not None:
            # A clean shutdown is the Stage-1 consistency boundary.  The disk
            # is copied only after QEMU can no longer write to it.
            clean = _graceful_shutdown(meta['qmp_socket'], pid, timeout=60)
            meta['checkpoint_consistency'] = (
                'clean-shutdown' if clean else 'crash-consistent')
        elif pid is not None:
            _kill_qemu(pid)

        stop_vm_sidecars(meta)
        if requested_save:
            meta['state'] = 'checkpointing'
            if not _write_metadata(runtime_dir, meta, missing_ok=True):
                raise HypervisorError(
                    'VM runtime disappeared before the checkpoint could be saved')
            create_checkpoint(meta, requested_save)
            meta['state'] = 'checkpointed'
            _write_metadata(runtime_dir, meta, missing_ok=True)
        remove_runtime = True
    finally:
        # Sidecar cleanup is idempotent and must still happen after a failed
        # checkpoint; the stopped source overlay is intentionally retained.
        stop_vm_sidecars(meta)

    # Backward-compatible cleanup for runtimes created before sidecar metadata.
    import glob, signal, shutil
    for pid_file in glob.glob(os.path.join(runtime_dir, '*.pid')):
        try:
            with open(pid_file) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except OSError:
            pass
    if remove_runtime:
        shutil.rmtree(runtime_dir, ignore_errors=True)
    return meta
