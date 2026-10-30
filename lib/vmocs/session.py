#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

"""SSH-backed guest task sessions."""

import base64
import json
import logging
import os
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
    _ssh_identity_options,
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
_MAX_WINDOWS_LAUNCH_RECORD_BYTES = 128 * 1024
_WINDOWS_ENV_VERSION = 1
_WINDOWS_STAGE_ID_RE = re.compile(r'^[0-9a-f]{32}$')
WINDOWS_FORWARDING_CHECKPOINT_REASON = (
    'Windows environment forwarding staged sensitive data on the guest disk')


class _WindowsStageCleanupPending(VmocsError):
    """A failed Windows stage whose exact cleanup still needs retrying."""

    def __init__(self, message, stage_id):
        super().__init__(message)
        self.stage_id = stage_id


def _utf16_code_units(value):
    """Return the number of UTF-16 code units Windows uses for VALUE."""
    return len(value.encode('utf-16le')) // 2


def resolve_forwarded_env(names, environ=None, command_style='posix'):
    """Resolve explicitly selected environment names without accepting values."""
    environ = os.environ if environ is None else environ
    resolved = []
    seen = {}
    for name in names:
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f'invalid environment variable name: {name!r}')
        identity = name.upper() if command_style == 'windows' else name
        if identity in seen:
            if command_style == 'windows' and seen[identity] != name:
                raise ValueError(
                    'Windows environment variable names differ only by case: '
                    f'{seen[identity]!r} and {name!r}')
            continue
        if len(resolved) >= _MAX_FORWARDED_ENV:
            raise ValueError(
                f'at most {_MAX_FORWARDED_ENV} environment variables may be forwarded')
        if name not in environ:
            raise ValueError(
                f'requested environment variable is not set: {name}')
        value = environ[name]
        if (command_style == 'windows'
                and ('\0' in value
                     or (_utf16_code_units(name) + 1
                         + _utf16_code_units(value)) > 32767)):
            raise ValueError(
                f'Windows environment variable is too large: {name!r}')
        seen[identity] = name
        resolved.append((name, value))

    if command_style == 'windows':
        payload = _serialize_windows_forwarded_env(resolved)
    else:
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


def _serialize_windows_forwarded_env(environment):
    """Encode Windows environment assignments as versioned UTF-8 JSON data."""
    record = {
        'version': _WINDOWS_ENV_VERSION,
        'environment': [
            {'name': name, 'value': value} for name, value in environment
        ],
    }
    return json.dumps(
        record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _serialize_windows_launch_record(environment, command):
    """Encode environment and Windows child-process metadata for staging."""
    if not command or not command[0]:
        raise VmocsError('Windows environment forwarding requires an executable')
    if any('\0' in argument for argument in command):
        raise VmocsError('Windows guest command contains a NUL character')
    windows_command_line = subprocess.list2cmdline(command)
    if len(windows_command_line.encode('utf-16le')) // 2 > 32767:
        raise VmocsError('Windows guest command line is too large')
    record = {
        'version': _WINDOWS_ENV_VERSION,
        'environment': [
            {'name': name, 'value': value} for name, value in environment
        ],
        'command': {
            'fileName': command[0],
            'arguments': subprocess.list2cmdline(command[1:]),
        },
    }
    payload = json.dumps(
        record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > _MAX_WINDOWS_LAUNCH_RECORD_BYTES:
        raise VmocsError('Windows environment launch record is too large')
    return payload


def _encode_powershell(script):
    """Encode a Windows PowerShell 5.1 command without embedding shell data."""
    return base64.b64encode(script.encode('utf-16le')).decode('ascii')


def _validate_windows_stage_id(stage_id):
    if not _WINDOWS_STAGE_ID_RE.fullmatch(stage_id):
        raise VmocsError('invalid Windows environment staging identifier')


def _windows_stage_script(stage_id):
    """Return the secret-free PowerShell staging command."""
    _validate_windows_stage_id(stage_id)
    return r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=[IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'),'VMoCS')
$dir=[IO.Path]::Combine($root,'vmocs-env-{stage_id}')
$partial=[IO.Path]::Combine($dir,'environment.partial')
$payload=[IO.Path]::Combine($dir,'environment.json')
$stream=$null
$source=$null
$safeDir=$false
try{
[IO.Directory]::CreateDirectory($root)|Out-Null
if([IO.Directory]::Exists($dir)){throw 'collision'}
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$inherit=[Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,[Security.AccessControl.FileSystemRights]::FullControl,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
[IO.Directory]::CreateDirectory($dir,$acl)|Out-Null
if(([IO.File]::GetAttributes($dir) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'reparse'}
$actual=[IO.Directory]::GetAccessControl($dir)
$rules=@($actual.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
if(-not $actual.AreAccessRulesProtected -or $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value -or $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or ($rules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne [Security.AccessControl.FileSystemRights]::FullControl){throw 'acl'}
$safeDir=$true
$source=[Console]::OpenStandardInput()
$stream=New-Object IO.FileStream($partial,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
$buffer=New-Object byte[] 8192
$total=0
while(($count=$source.Read($buffer,0,$buffer.Length)) -gt 0){
$total+=$count
if($total -gt 131072){throw 'oversize'}
$stream.Write($buffer,0,$count)
}
$stream.Flush($true)
$stream.Dispose();$stream=$null
$source.Dispose();$source=$null
[IO.File]::Move($partial,$payload)
exit 0
}catch{
try{if($stream){$stream.Dispose()}}catch{}
try{if($source){$source.Dispose()}}catch{}
if($safeDir){
try{[IO.File]::Delete($partial)}catch{}
try{[IO.File]::Delete($payload)}catch{}
try{[IO.Directory]::Delete($dir,$false)}catch{}
}
[Console]::Error.WriteLine('VMoCS environment staging failed.')
exit 86
}
""".strip().replace('{stage_id}', stage_id)


def _windows_launch_script(stage_id):
    """Return the secret-free PowerShell child launcher."""
    _validate_windows_stage_id(stage_id)
    return r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=[IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'),'VMoCS')
$dir=[IO.Path]::Combine($root,'vmocs-env-{stage_id}')
$payload=[IO.Path]::Combine($dir,'environment.json')
$stream=$null
$safeDir=$false
try{
if(([IO.File]::GetAttributes($dir) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'reparse'}
$safeDir=$true
$stream=[IO.File]::Open($payload,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
if($stream.Length -gt 131072){throw 'oversize'}
$bytes=New-Object byte[] ([int]$stream.Length)
$offset=0
while($offset -lt $bytes.Length){
$count=$stream.Read($bytes,$offset,$bytes.Length-$offset)
if($count -eq 0){throw 'truncated'}
$offset+=$count
}
$stream.Dispose();$stream=$null
$utf8=New-Object Text.UTF8Encoding($false,$true)
$record=ConvertFrom-Json -InputObject $utf8.GetString($bytes)
if(($record.version -isnot [int]) -or $record.version -ne 1){throw 'version'}
$items=@($record.environment)
if($items.Count -gt 16){throw 'count'}
if($null -eq $record.command -or $record.command.fileName -isnot [string] -or [string]::IsNullOrEmpty($record.command.fileName) -or $record.command.arguments -isnot [string]){throw 'command'}
$seen=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$psi=New-Object Diagnostics.ProcessStartInfo
$psi.FileName=$record.command.fileName
$psi.Arguments=$record.command.arguments
$psi.UseShellExecute=$false
$psi.RedirectStandardInput=$false
$psi.RedirectStandardOutput=$false
$psi.RedirectStandardError=$false
foreach($item in $items){
if($null -eq $item -or $item.name -isnot [string] -or $item.value -isnot [string]){throw 'type'}
$name=$item.name;$value=$item.value
if($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or -not $seen.Add($name)){throw 'name'}
if($value.IndexOf([char]0) -ge 0 -or ($name.Length+1+$value.Length) -gt 32767){throw 'value'}
$psi.EnvironmentVariables[$name]=$value
}
$environmentChars=1
foreach($entry in $psi.EnvironmentVariables.GetEnumerator()){
$environmentChars+=([string]$entry.Key).Length+1+([string]$entry.Value).Length+1
}
if($environmentChars -gt 32767){throw 'environment'}
[IO.File]::Delete($payload)
[IO.Directory]::Delete($dir,$false)
$safeDir=$false
$record=$null;$items=$null;$bytes=$null
$process=[Diagnostics.Process]::Start($psi)
if($null -eq $process){throw 'start'}
$psi=$null
$process.WaitForExit()
exit $process.ExitCode
}catch{
try{if($stream){$stream.Dispose()}}catch{}
if($safeDir){
try{[IO.File]::Delete($payload)}catch{}
try{[IO.Directory]::Delete($dir,$false)}catch{}
}
[Console]::Error.WriteLine('VMoCS environment launch failed.')
exit 87
}
""".strip().replace('{stage_id}', stage_id)


def _windows_cleanup_script(stage_id):
    """Return a best-effort, exact-path cleanup command."""
    _validate_windows_stage_id(stage_id)
    return r"""
$ProgressPreference='SilentlyContinue'
$root=[IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'),'VMoCS')
$dir=[IO.Path]::Combine($root,'vmocs-env-{stage_id}')
try{
if(-not [IO.Directory]::Exists($dir)){exit 0}
if(([IO.File]::GetAttributes($dir) -band [IO.FileAttributes]::ReparsePoint) -ne 0){exit 1}
[IO.File]::Delete([IO.Path]::Combine($dir,'environment.partial'))
[IO.File]::Delete([IO.Path]::Combine($dir,'environment.json'))
[IO.Directory]::Delete($dir,$false)
if([IO.Directory]::Exists($dir)){exit 1}
exit 0
}catch{exit 1}
""".strip().replace('{stage_id}', stage_id)


def _windows_powershell_command(script):
    """Build a cmd.exe-safe invocation of fixed PowerShell code."""
    return subprocess.list2cmdline((
        'powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
        '-EncodedCommand', _encode_powershell(script)))


def _ssh_base_command(meta, tty=False):
    """Build the common SSH argv without a remote command."""
    known_hosts_path = meta.get('known_hosts_path')
    argv = [
        'ssh',
        '-o', 'BatchMode=yes',
        '-o', ('StrictHostKeyChecking=yes'
               if known_hosts_path else 'StrictHostKeyChecking=no'),
        '-o', (f'UserKnownHostsFile={known_hosts_path}'
               if known_hosts_path else 'UserKnownHostsFile=/dev/null'),
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
    ]
    if meta.get('key_path'):
        argv += _ssh_identity_options(meta['key_path'])
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
        command_style = meta.get('ssh_command_style', 'posix')
        if command_style == 'windows':
            if env_file:
                remote_command = _windows_powershell_command(
                    _windows_launch_script(env_file))
            else:
                # Windows OpenSSH passes the command string to cmd.exe.  It has
                # no POSIX `exec` builtin, and its argv quoting follows the
                # Microsoft command-line rules implemented by list2cmdline().
                remote_command = subprocess.list2cmdline(command)
        else:
            # ssh sends a command string to the login shell. shlex.join
            # preserves the original POSIX argv across that required shell
            # boundary, and exec keeps signals attached to the guest process.
            remote_command = 'exec ' + shlex.join(command)
        if env_file and command_style != 'windows':
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


def _stage_posix_forwarded_env(meta, environment):
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


def _stage_windows_forwarded_env(meta, environment, command):
    """Stage a Windows launch record without placing values in process argv."""
    if not meta.get('ephemeral_ssh_key') or not meta.get('key_path'):
        raise VmocsError(
            'Windows environment forwarding requires insert-key: true')
    if not meta.get('ssh_host_key_pinned') or not meta.get('known_hosts_path'):
        raise VmocsError(
            'Windows environment forwarding requires a pinned SSH host key')
    if not meta.get('secure_ssh_handoff_verified'):
        raise VmocsError(
            'Windows environment forwarding requires a verified SSH handoff')
    stage_id = secrets.token_hex(16)
    payload = _serialize_windows_launch_record(environment, command)
    remote_command = _windows_powershell_command(
        _windows_stage_script(stage_id))
    try:
        result = subprocess.run(
            _ssh_base_command(meta) + [remote_command],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False)
    except OSError as exc:
        message = 'failed to start SSH for environment forwarding'
        if not _cleanup_windows_forwarded_env(meta, stage_id):
            raise _WindowsStageCleanupPending(message, stage_id) from exc
        raise VmocsError(message) from exc
    if result.returncode != 0:
        message = 'failed to stage forwarded environment in guest'
        if not _cleanup_windows_forwarded_env(meta, stage_id):
            raise _WindowsStageCleanupPending(message, stage_id)
        raise VmocsError(message)
    return stage_id


def _stage_forwarded_env(meta, environment, command=()):
    """Stage selected values using the guest command style."""
    if meta.get('ssh_command_style', 'posix') == 'windows':
        return _stage_windows_forwarded_env(meta, environment, command)
    return _stage_posix_forwarded_env(meta, environment)


def _cleanup_windows_forwarded_env(meta, stage_id):
    """Best-effort cleanup if the command SSH session never consumed a stage."""
    remote_command = _windows_powershell_command(
        _windows_cleanup_script(stage_id))
    try:
        result = subprocess.run(
            _ssh_base_command(meta) + [remote_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
        if (failure and watcher.guest_shutdown is not True
                and not is_vm_stopping(meta['runtime_dir'])):
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
    windows_forwarding = bool(
        forwarded_env and meta.get('ssh_command_style') == 'windows')
    if windows_forwarding:
        if not meta.get('ephemeral_ssh_key') or not meta.get('key_path'):
            raise VmocsError(
                'Windows environment forwarding requires insert-key: true')
        if (not meta.get('ssh_host_key_pinned')
                or not meta.get('known_hosts_path')):
            raise VmocsError(
                'Windows environment forwarding requires a pinned SSH host key')
        if not meta.get('secure_ssh_handoff_verified'):
            raise VmocsError(
                'Windows environment forwarding requires a verified SSH handoff')
        if save_path or meta.get('save_path'):
            raise VmocsError(
                'Windows environment forwarding cannot be combined with saving')
        if not meta.get('checkpoint_blocked_reason'):
            meta['checkpoint_blocked_reason'] = (
                WINDOWS_FORWARDING_CHECKPOINT_REASON)
            _write_metadata(meta['runtime_dir'], meta)

    qemu_pid = meta['pid']
    tty = os.isatty(0)
    watcher = _LifecycleWatcher(meta['qmp_socket'])
    status = 255
    checkpoint_consistency = 'crash-consistent'
    stop_event = threading.Event()
    termination_signal = [None]
    old_handlers = {}
    pending_windows_stages = set()

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
                try:
                    env_file = _stage_forwarded_env(
                        meta, forwarded_env, command=command)
                except _WindowsStageCleanupPending as exc:
                    pending_windows_stages.add(exc.stage_id)
                    raise
                if windows_forwarding:
                    pending_windows_stages.add(env_file)
            try:
                status = _run_ssh(
                    meta, command, tty, watcher,
                    stop_requested=stop_event.is_set, env_file=env_file)
            finally:
                if (windows_forwarding and env_file
                        and _cleanup_windows_forwarded_env(meta, env_file)):
                    pending_windows_stages.discard(env_file)
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
                        stop_requested=stop_event.is_set,
                        known_hosts_path=meta.get('known_hosts_path')):
                    for stage_id in tuple(pending_windows_stages):
                        if _cleanup_windows_forwarded_env(meta, stage_id):
                            pending_windows_stages.discard(stage_id)
                    continue
            return status
    finally:
        for stage_id in tuple(pending_windows_stages):
            if _cleanup_windows_forwarded_env(meta, stage_id):
                pending_windows_stages.discard(stage_id)
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
