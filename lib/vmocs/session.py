#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

"""SSH-backed guest task sessions."""

import os
import queue
import shlex
import shutil
import subprocess
import time

from .launch import _kill_qemu, wait_for_ssh
from .image import VMImage
from .monitor import QemuMonitor


def build_ssh_command(meta, command=(), tty=False):
    """Build an SSH argv for COMMAND without involving a host-side shell."""
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
    if command:
        # ssh sends a command string to the login shell. shlex.join preserves
        # the original POSIX argv across that required shell boundary.
        argv.append('exec ' + shlex.join(command))
    return argv


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


def run_attached_session(meta, command=(), reconnect_timeout=180,
                         save_path=None):
    """Run COMMAND in the guest and return its exit status.

    Interactive sessions reconnect after a QMP RESET or an SSH transport loss
    while QEMU remains alive. Non-interactive commands are never replayed.
    """
    qemu_pid = meta['pid']
    tty = os.isatty(0)
    watcher = _LifecycleWatcher(meta['qmp_socket'])
    status = 255

    try:
        while True:
            status = subprocess.call(build_ssh_command(meta, command, tty=tty))
            # QMP events and the SSH child's exit can race slightly.
            time.sleep(0.1)
            watcher.drain()

            if watcher.guest_shutdown or not _process_alive(qemu_pid):
                return 0 if watcher.guest_shutdown else status

            transport_lost = status == 255
            # A clean SSH exit is an intentional logout, even if a delayed
            # RESET event from an earlier reboot arrives at the same time.
            # Reconnect only for an actual SSH transport failure.
            if tty and transport_lost:
                if wait_for_ssh(
                        '127.0.0.1', meta['ssh_port'], meta.get('key_path'),
                        reconnect_timeout, meta['ssh_user']):
                    continue
            return status
    finally:
        watcher.drain()
        if _process_alive(qemu_pid):
            try:
                watcher.monitor.powerdown()
            except Exception:
                pass
            if not _wait_for_qemu(qemu_pid, 20, watcher.drain):
                try:
                    watcher.monitor.quit()
                except Exception:
                    pass
                if not _wait_for_qemu(qemu_pid, 5, watcher.drain):
                    _kill_qemu(qemu_pid, timeout=1)
                    _wait_for_qemu(qemu_pid, 2)
        remove_runtime = True
        try:
            if save_path:
                VMImage.convert_standalone(meta['overlay'], save_path)
        except Exception:
            # Preserve the stopped overlay for manual recovery if saving fails.
            remove_runtime = False
            raise
        finally:
            watcher.close()
            if remove_runtime:
                shutil.rmtree(meta['runtime_dir'], ignore_errors=True)
