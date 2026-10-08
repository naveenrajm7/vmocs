#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

"""SSH-backed guest task sessions."""

import os
import logging
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time

from .checkpoint import create_checkpoint
from .error import VmocsError
from .launch import (
    _kill_qemu,
    _write_metadata,
    is_vm_stopping,
    stop_vm_sidecars,
    wait_for_ssh,
)
from .monitor import QemuMonitor


_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_FORWARDED_ENV = 16
_MAX_FORWARDED_ENV_BYTES = 64 * 1024


def resolve_forwarded_env(names, environ=None):
    """Resolve explicitly selected environment names without accepting values."""
    environ = os.environ if environ is None else environ
    resolved = []
    seen = set()
    for name in names:
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f'invalid environment variable name: {name!r}')
        if name in seen:
            continue
        if len(resolved) >= _MAX_FORWARDED_ENV:
            raise ValueError(
                f'at most {_MAX_FORWARDED_ENV} environment variables may be forwarded')
        if name not in environ:
            raise ValueError(
                f'requested environment variable is not set: {name}')
        seen.add(name)
        resolved.append((name, environ[name]))

    payload = _serialize_forwarded_env(resolved)
    if len(payload) > _MAX_FORWARDED_ENV_BYTES:
        raise ValueError(
            f'forwarded environment exceeds {_MAX_FORWARDED_ENV_BYTES} bytes')
    return tuple(resolved)


def _serialize_forwarded_env(environment):
    """Encode environment assignments as a POSIX shell file."""
    lines = []
    for name, value in environment:
        lines.append(f'{name}={shlex.quote(value)}\nexport {name}\n')
    return ''.join(lines).encode()


def _ssh_base_command(meta, tty=False):
    """Build the common SSH argv without a remote command."""
    argv = [
        'ssh',
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
    ]
    if meta.get('key_path'):
        argv += ['-i', meta['key_path']]
    argv += ['-tt' if tty else '-T']
    argv += [
        '-p', str(meta['ssh_port']),
        f'{meta["ssh_user"]}@127.0.0.1',
    ]
    return argv


def build_ssh_command(meta, command=(), tty=False, env_file=None):
    """Build an SSH argv for COMMAND without involving a host-side shell."""
    argv = _ssh_base_command(meta, tty=tty)
    if command:
        # ssh sends a command string to the login shell. shlex.join preserves
        # the original POSIX argv across that required shell boundary.
        remote_command = 'exec ' + shlex.join(command)
        if env_file:
            remote_command = (
                f'env_file={shlex.quote(env_file)}; '
                'env_dir=${env_file%/*}; '
                'cleanup() { rm -f -- "$env_file" 2>/dev/null; '
                'rmdir -- "$env_dir" 2>/dev/null; }; '
                'trap cleanup 0 1 2 15; '
                'if ! . "$env_file"; then exit 125; fi; '
                'cleanup; trap - 0 1 2 15; ' + remote_command)
        argv.append(remote_command)
    return argv


def _stage_forwarded_env(meta, environment):
    """Send selected values over SSH stdin into a private guest tmpfs file."""
    token = secrets.token_hex(16)
    env_dir = f'/dev/shm/vmocs-env-{token}'
    env_file = f'{env_dir}/environment'
    remote_command = (
        f'env_dir={shlex.quote(env_dir)}; '
        'env_file="$env_dir/environment"; '
        'umask 077; '
        'mkdir -m 700 -- "$env_dir" || exit 1; '
        'if ! cat > "$env_file"; then '
        'rm -f -- "$env_file"; rmdir -- "$env_dir"; exit 1; fi; '
        'chmod 600 -- "$env_file"')
    try:
        result = subprocess.run(
            _ssh_base_command(meta) + [remote_command],
            input=_serialize_forwarded_env(environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False)
    except OSError as exc:
        raise VmocsError(
            'failed to start SSH for environment forwarding') from exc
    if result.returncode != 0:
        raise VmocsError('failed to stage forwarded environment in guest')
    return env_file


def _process_alive(pid):
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class _LifecycleWatcher:
    """Turn QMP lifecycle events into supervisor state.

    Supervised sessions launch QEMU with -no-shutdown. RESET therefore keeps
    QEMU alive for a reboot. SHUTDOWN is converted into QMP quit so a guest
    poweroff still ends the Slurm task.
    """

    def __init__(self, qmp_socket):
        self.monitor = QemuMonitor(qmp_socket)
        self.events = queue.Queue()
        self.reset_count = 0
        self.guest_shutdown = False
        self.monitor.add_event_handler(self._on_event)

    def _on_event(self, event):
        self.events.put(event)
        return True

    def drain(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if event is None:
                continue
            name = event.get('event')
            if name == 'RESET':
                self.reset_count += 1
            elif name == 'SHUTDOWN':
                self.guest_shutdown = True
                try:
                    self.monitor.quit()
                except Exception:
                    pass

    def close(self):
        self.monitor.close()


def _wait_for_qemu(pid, timeout, on_poll=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if on_poll:
            on_poll()
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited == pid:
            return True
        time.sleep(0.2)
    return False


def _run_ssh(meta, command, tty, watcher, stop_requested=None, env_file=None):
    """Run SSH while continuing to supervise critical VM sidecars."""
    process = subprocess.Popen(
        build_ssh_command(meta, command, tty=tty, env_file=env_file))
    manager = meta.get('_sidecar_manager')
    while process.poll() is None:
        watcher.drain()
        if stop_requested and stop_requested():
            process.terminate()
            break
        failure = manager.failure() if manager is not None else None
        if failure and not is_vm_stopping(meta['runtime_dir']):
            name, status = failure
            logging.error(
                'critical sidecar %s exited with status %s; stopping VM',
                name, status)
            try:
                watcher.monitor.quit()
            except Exception:
                pass
            process.terminate()
            break
        time.sleep(0.1)
    return process.wait()


def run_attached_session(meta, command=(), reconnect_timeout=180,
                         save_path=None, forwarded_env=()):
    """Run COMMAND in the guest and return its exit status.

    Interactive sessions reconnect after a QMP RESET or an SSH transport loss
    while QEMU remains alive. Non-interactive commands are never replayed.
    """
    qemu_pid = meta['pid']
    tty = os.isatty(0)
    watcher = _LifecycleWatcher(meta['qmp_socket'])
    status = 255
    checkpoint_consistency = 'crash-consistent'
    stop_event = threading.Event()
    termination_signal = [None]
    old_handlers = {}

    def _on_signal(signum, _frame):
        termination_signal[0] = termination_signal[0] or signum
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _on_signal)

    try:
        while True:
            env_file = None
            if forwarded_env:
                env_file = _stage_forwarded_env(meta, forwarded_env)
            status = _run_ssh(
                meta, command, tty, watcher,
                stop_requested=stop_event.is_set, env_file=env_file)
            # QMP events and the SSH child's exit can race slightly.
            time.sleep(0.1)
            watcher.drain()

            if stop_event.is_set():
                return 128 + termination_signal[0]

            if watcher.guest_shutdown or not _process_alive(qemu_pid):
                if watcher.guest_shutdown:
                    checkpoint_consistency = 'clean-shutdown'
                return 0 if watcher.guest_shutdown else status

            transport_lost = status == 255
            # A clean SSH exit is an intentional logout, even if a delayed
            # RESET event from an earlier reboot arrives at the same time.
            # Reconnect only for an actual SSH transport failure.
            if tty and transport_lost:
                if wait_for_ssh(
                        '127.0.0.1', meta['ssh_port'], meta.get('key_path'),
                        reconnect_timeout, meta['ssh_user'],
                        stop_requested=stop_event.is_set):
                    continue
            return status
    finally:
        meta['state'] = 'stopping'
        _write_metadata(meta['runtime_dir'], meta)
        watcher.drain()
        if _process_alive(qemu_pid):
            try:
                watcher.monitor.powerdown()
            except Exception:
                pass
            if _wait_for_qemu(qemu_pid, 20, watcher.drain):
                checkpoint_consistency = 'clean-shutdown'
            else:
                try:
                    watcher.monitor.quit()
                except Exception:
                    pass
                if not _wait_for_qemu(qemu_pid, 5, watcher.drain):
                    _kill_qemu(qemu_pid, timeout=1)
                    _wait_for_qemu(qemu_pid, 2)
        remove_runtime = False
        try:
            watcher.close()
            stop_vm_sidecars(meta)
            checkpoint_destination = save_path or meta.get('save_path')
            if checkpoint_destination:
                meta['checkpoint_consistency'] = checkpoint_consistency
                meta['state'] = 'checkpointing'
                _write_metadata(meta['runtime_dir'], meta)
                create_checkpoint(meta, checkpoint_destination)
                meta['state'] = 'checkpointed'
                _write_metadata(meta['runtime_dir'], meta)
            remove_runtime = True
        except Exception:
            # Preserve the stopped overlay for manual recovery if saving fails.
            raise
        finally:
            stop_vm_sidecars(meta)
            if remove_runtime:
                shutil.rmtree(meta['runtime_dir'], ignore_errors=True)
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
