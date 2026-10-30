"""Unit tests for seamless SSH guest sessions."""

import base64
import json
import subprocess
from unittest.mock import MagicMock

import pytest

from vmocs import session
from vmocs.session import build_ssh_command, resolve_forwarded_env


def _meta(key_path='/tmp/vmocs/42/id_ed25519'):
    return {
        'key_path': key_path,
        'ephemeral_ssh_key': True,
        'ssh_host_key_pinned': True,
        'secure_ssh_handoff_verified': True,
        'known_hosts_path': '/tmp/vmocs/42/ssh_known_hosts',
        'ssh_port': 60222,
        'ssh_user': 'ubuntu',
    }


def _mock_ssh_process(status):
    process = MagicMock()
    process.poll.return_value = status
    process.wait.return_value = status
    return process


def _decode_powershell(remote_command):
    encoded = remote_command.split('-EncodedCommand ', 1)[1]
    return base64.b64decode(encoded).decode('utf-16le')


def _runtime_meta(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    overlay = runtime / 'disk.qcow2'
    overlay.write_bytes(b'disk')
    meta = _meta()
    meta.update({
        'pid': 123,
        'qmp_socket': str(runtime / 'qmp.sock'),
        'runtime_dir': str(runtime),
        'overlay': str(overlay),
        'state': 'running',
    })
    return meta


def test_build_ssh_command_preserves_remote_arguments():
    argv = build_ssh_command(
        _meta(), ('bash', '-c', 'printf "%s\\n" "$1"', 'bash', 'hello world'))

    assert argv[0] == 'ssh'
    assert argv[argv.index('-i') + 1] == '/tmp/vmocs/42/id_ed25519'
    assert '-F' not in argv
    assert 'IdentitiesOnly=yes' not in argv
    assert 'IdentityAgent=none' not in argv
    assert 'CertificateFile=none' not in argv
    assert '-T' in argv
    assert '-tt' not in argv
    assert argv[-2] == 'ubuntu@127.0.0.1'
    assert argv[-1] == (
        "exec bash -c 'printf \"%s\\n\" \"$1\"' bash 'hello world'")


def test_build_ssh_command_requests_tty_for_interactive_session():
    argv = build_ssh_command(_meta(), ('bash', '-l'), tty=True)

    assert '-tt' in argv
    assert '-T' not in argv
    assert argv[-1] == "exec bash -l"


def test_build_ssh_command_uses_windows_command_line_without_exec():
    meta = _meta()
    meta['ssh_command_style'] = 'windows'

    argv = build_ssh_command(
        meta, ('powershell.exe', '-NoLogo', '-Command',
               'Write-Output "Hello from Windows"'), tty=True)

    assert '-tt' in argv
    assert argv[argv.index('-F') + 1] == '/etc/ssh/ssh_config'
    assert 'IdentitiesOnly=yes' in argv
    assert 'IdentityAgent=none' in argv
    assert 'CertificateFile=none' in argv
    assert 'ControlMaster=no' in argv
    assert 'ControlPath=none' in argv
    assert argv[-1] == (
        'powershell.exe -NoLogo -Command '
        '"Write-Output \\"Hello from Windows\\""')


def test_build_ssh_command_uses_secret_free_windows_launcher():
    meta = _meta()
    meta['ssh_command_style'] = 'windows'
    stage_id = 'a' * 32

    argv = build_ssh_command(
        meta, ('powershell.exe', '-NoProfile'), env_file=stage_id)

    script = _decode_powershell(argv[-1])
    assert 'powershell.exe -NoProfile' not in argv[-1]
    assert stage_id in script
    assert 'ProcessStartInfo' in script
    assert 'EnvironmentVariables[$name]=$value' in script
    assert '$environmentChars=1' in script
    assert '$psi.EnvironmentVariables.GetEnumerator()' in script
    assert "if($environmentChars -gt 32767){throw 'environment'}" in script
    assert '[IO.File]::Delete($payload)' in script
    assert script.index(
        "if($environmentChars -gt 32767){throw 'environment'}"
    ) < script.index('[IO.File]::Delete($payload)')
    assert script.index('[IO.File]::Delete($payload)') < script.index(
        '[Diagnostics.Process]::Start($psi)')
    assert '$process.WaitForExit()' in script
    assert 'exit $process.ExitCode' in script
    assert 'env_file=' not in argv[-1]
    assert len(argv[-1]) < 8191


def test_build_ssh_command_allows_configured_ssh_identity():
    argv = build_ssh_command(_meta(key_path=None), ())

    assert '-i' not in argv
    assert argv[-1] == 'ubuntu@127.0.0.1'


def test_resolve_forwarded_env_is_name_only_and_preserves_values():
    resolved = resolve_forwarded_env(
        ('TOKEN', 'URL', 'TOKEN'),
        {'TOKEN': "line one\nline 'two'", 'URL': 'https://example.test/a?b=c'})

    assert resolved == (
        ('TOKEN', "line one\nline 'two'"),
        ('URL', 'https://example.test/a?b=c'))


@pytest.mark.parametrize('name', ('', 'BAD-NAME', '1TOKEN', 'A=B'))
def test_resolve_forwarded_env_rejects_invalid_names(name):
    with pytest.raises(ValueError, match='invalid environment variable name'):
        resolve_forwarded_env((name,), {name: 'value'})


def test_resolve_forwarded_env_requires_present_value():
    with pytest.raises(ValueError, match='is not set'):
        resolve_forwarded_env(('MISSING',), {})


def test_resolve_forwarded_env_limits_variable_count():
    names = tuple(f'VAR_{index}' for index in range(17))
    environ = {name: 'value' for name in names}

    with pytest.raises(ValueError, match='at most 16'):
        resolve_forwarded_env(names, environ)


def test_resolve_forwarded_env_limits_payload_size():
    with pytest.raises(ValueError, match='exceeds 65536 bytes'):
        resolve_forwarded_env(('TOKEN',), {'TOKEN': 'x' * (64 * 1024)})


def test_resolve_windows_forwarded_env_rejects_case_collisions():
    with pytest.raises(ValueError, match='differ only by case'):
        resolve_forwarded_env(
            ('TOKEN', 'token'), {'TOKEN': 'one', 'token': 'two'},
            command_style='windows')


def test_resolve_windows_forwarded_env_rejects_oversized_entry():
    with pytest.raises(ValueError, match='Windows environment variable is too large'):
        resolve_forwarded_env(
            ('TOKEN',), {'TOKEN': 'x' * 32767}, command_style='windows')


def test_resolve_windows_forwarded_env_counts_utf16_code_units():
    with pytest.raises(
            ValueError,
            match='Windows environment variable is too large'):
        resolve_forwarded_env(
            ('TOKEN',), {'TOKEN': '\U0001f600' * 16381},
            command_style='windows')


def test_resolve_windows_forwarded_env_limits_aggregate_json_size():
    values = {'FIRST': 'x' * 32750, 'SECOND': 'y' * 32750}

    with pytest.raises(ValueError, match='exceeds 65536 bytes'):
        resolve_forwarded_env(
            tuple(values), values, command_style='windows')


def test_windows_environment_json_preserves_values_as_data():
    values = (
        ('EMPTY', ''),
        ('HOSTILE', "line one\r\n'`$() % & | < > ^ \"line two\""),
        ('UNICODE', 'snowman ☃'),
    )

    payload = session._serialize_windows_forwarded_env(values)
    decoded = json.loads(payload)

    assert decoded == {
        'version': 1,
        'environment': [
            {'name': name, 'value': value} for name, value in values
        ],
    }


def test_windows_launch_record_rejects_oversized_command_line():
    with pytest.raises(session.VmocsError, match='command line is too large'):
        session._serialize_windows_launch_record(
            (('TOKEN', 'value'),), ('powershell.exe', 'x' * 32768))


def test_serialized_environment_preserves_shell_metacharacters(tmp_path):
    marker = tmp_path / 'injected'
    secret = f"line one\n'line two' $(touch {marker})"
    env_file = tmp_path / 'environment'
    env_file.write_bytes(session._serialize_forwarded_env((('TOKEN', secret),)))

    result = subprocess.run(
        ['/bin/sh', '-c', '. "$1"; printf %s "$TOKEN"', 'sh', str(env_file)],
        stdout=subprocess.PIPE, check=True)

    assert result.stdout.decode() == secret
    assert not marker.exists()


def test_stage_forwarded_env_keeps_secret_out_of_ssh_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured.update(kwargs)
        return MagicMock(returncode=0)

    monkeypatch.setattr(session.secrets, 'token_hex', lambda _size: 'abc123')
    monkeypatch.setattr(session.subprocess, 'run', fake_run)
    secret = "claim token with 'quotes' and\nnewlines"

    env_file = session._stage_forwarded_env(
        _meta(), (('SLURM_GHA_CLAIM_TOKEN', secret),))

    assert env_file == '/dev/shm/vmocs-env-abc123/environment'
    assert secret not in '\0'.join(captured['argv'])
    assert b'claim token with ' in captured['input']
    assert b'newlines' in captured['input']
    assert b'export SLURM_GHA_CLAIM_TOKEN' in captured['input']


def test_stage_windows_forwarded_env_uses_non_tty_stdin_and_private_acl(
        monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured.update(kwargs)
        return MagicMock(returncode=0)

    meta = _meta()
    meta['ssh_command_style'] = 'windows'
    secret = "claim token with 'quotes',\r\nUnicode ☃, and $()"
    monkeypatch.setattr(session.secrets, 'token_hex', lambda _size: 'b' * 32)
    monkeypatch.setattr(session.subprocess, 'run', fake_run)

    stage_id = session._stage_forwarded_env(
        meta, (('SLURM_GHA_CLAIM_TOKEN', secret),),
        command=('powershell.exe', '-NoProfile', '-File', 'bootstrap.ps1'))

    assert stage_id == 'b' * 32
    assert '-T' in captured['argv']
    assert '-tt' not in captured['argv']
    assert secret not in '\0'.join(captured['argv'])
    record = json.loads(captured['input'])
    assert record['environment'] == [
        {'name': 'SLURM_GHA_CLAIM_TOKEN', 'value': secret}]
    assert record['command'] == {
        'fileName': 'powershell.exe',
        'arguments': '-NoProfile -File bootstrap.ps1',
    }

    script = _decode_powershell(captured['argv'][-1])
    assert '$acl.SetAccessRuleProtection($true,$false)' in script
    assert 'WindowsIdentity]::GetCurrent().User' in script
    assert '$actual.AreAccessRulesProtected' in script
    assert '[IO.FileMode]::CreateNew' in script
    assert '[IO.FileShare]::None' in script
    assert '[IO.File]::Move($partial,$payload)' in script
    assert "$ProgressPreference='SilentlyContinue'" in script
    assert len(captured['argv'][-1]) < 8191


def test_stage_windows_forwarded_env_does_not_leak_failed_payload(
        monkeypatch):
    secret = 'do-not-report-this-token'
    meta = _meta()
    meta['ssh_command_style'] = 'windows'
    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(session, '_cleanup_windows_forwarded_env', cleanup)
    monkeypatch.setattr(session.secrets, 'token_hex', lambda _size: 'c' * 32)
    monkeypatch.setattr(
        session.subprocess, 'run',
        lambda *_args, **_kwargs: MagicMock(
            returncode=86, stderr=secret.encode()))

    with pytest.raises(
            session.VmocsError,
            match='failed to stage forwarded environment in guest') as error:
        session._stage_forwarded_env(
            meta, (('TOKEN', secret),), command=('powershell.exe',))

    assert secret not in str(error.value)
    cleanup.assert_called_once_with(meta, 'c' * 32)


def test_stage_windows_forwarded_env_requires_ephemeral_key():
    meta = _meta()
    meta.update(ssh_command_style='windows', ephemeral_ssh_key=False)

    with pytest.raises(session.VmocsError, match='insert-key: true'):
        session._stage_forwarded_env(
            meta, (('TOKEN', 'secret'),), command=('powershell.exe',))


def test_stage_windows_forwarded_env_requires_pinned_host_key():
    meta = _meta()
    meta.update(
        ssh_command_style='windows', ssh_host_key_pinned=False,
        known_hosts_path=None)

    with pytest.raises(session.VmocsError, match='pinned SSH host key'):
        session._stage_forwarded_env(
            meta, (('TOKEN', 'secret'),), command=('powershell.exe',))


def test_stage_windows_forwarded_env_requires_verified_ssh_handoff():
    meta = _meta()
    meta.update(
        ssh_command_style='windows', secure_ssh_handoff_verified=False)

    with pytest.raises(session.VmocsError, match='verified SSH handoff'):
        session._stage_forwarded_env(
            meta, (('TOKEN', 'secret'),), command=('powershell.exe',))


def test_build_ssh_command_sources_and_removes_staged_environment():
    argv = build_ssh_command(
        _meta(), ('/opt/slurm-gha/bootstrap.sh',),
        env_file='/dev/shm/vmocs-env-abc/environment')

    assert argv[-1].endswith('exec /opt/slurm-gha/bootstrap.sh')
    assert '. "$env_file"' in argv[-1]
    assert 'rm -f -- "$env_file"' in argv[-1]


def test_process_alive_reaps_exited_qemu(monkeypatch):
    monkeypatch.setattr(session.os, 'waitpid', lambda pid, flags: (pid, 0))
    kill = MagicMock()
    monkeypatch.setattr(session.os, 'kill', kill)

    assert session._process_alive(123) is False
    kill.assert_not_called()


def test_interactive_session_reconnects_after_reset(monkeypatch, tmp_path):
    class Watcher:
        def __init__(self, _socket):
            self.monitor = MagicMock()
            self.reset_count = 0
            self.guest_shutdown = False
            self.drains = 0

        def drain(self):
            self.drains += 1
            if self.drains == 1:
                self.reset_count += 1

        def close(self):
            pass

    statuses = iter([255, 0])
    monkeypatch.setattr(session, '_LifecycleWatcher', Watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: True)
    monkeypatch.setattr(
        session.subprocess, 'Popen',
        lambda _argv: _mock_ssh_process(next(statuses)))
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, 'wait_for_ssh', lambda *args, **kwargs: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _runtime_meta(tmp_path)

    assert session.run_attached_session(meta, ('bash', '-l')) == 0
    assert not (tmp_path / 'runtime').exists()


def test_noninteractive_command_is_not_replayed_on_transport_error(
        monkeypatch, tmp_path):
    watcher = MagicMock()
    watcher.reset_count = 0
    watcher.guest_shutdown = False
    monkeypatch.setattr(session, '_LifecycleWatcher', lambda _socket: watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: False)
    ssh = MagicMock(return_value=_mock_ssh_process(255))
    monkeypatch.setattr(session.subprocess, 'Popen', ssh)
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _runtime_meta(tmp_path)

    assert session.run_attached_session(meta, ('train.py',)) == 255
    assert ssh.call_count == 1


@pytest.mark.parametrize('status', (1, 37, 255))
def test_windows_forwarding_always_attempts_exact_stage_cleanup(
        monkeypatch, tmp_path, status):
    watcher = MagicMock()
    watcher.reset_count = 0
    watcher.guest_shutdown = False
    monkeypatch.setattr(session, '_LifecycleWatcher', lambda _socket: watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: False)
    monkeypatch.setattr(session.time, 'sleep', lambda _delay: None)
    monkeypatch.setattr(
        session, '_stage_forwarded_env', MagicMock(return_value='d' * 32))
    monkeypatch.setattr(session, '_run_ssh', MagicMock(return_value=status))
    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(session, '_cleanup_windows_forwarded_env', cleanup)
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _runtime_meta(tmp_path)
    meta.update({
        'ssh_command_style': 'windows',
        'checkpoint_blocked_reason':
            session.WINDOWS_FORWARDING_CHECKPOINT_REASON,
    })

    assert session.run_attached_session(
        meta, ('powershell.exe',), forwarded_env=(('TOKEN', 'secret'),)
    ) == status
    cleanup.assert_called_once_with(meta, 'd' * 32)


def test_windows_forwarding_retries_pending_cleanup_after_reconnect(
        monkeypatch, tmp_path):
    watcher = MagicMock()
    watcher.reset_count = 0
    watcher.guest_shutdown = False
    monkeypatch.setattr(session, '_LifecycleWatcher', lambda _socket: watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: True)
    monkeypatch.setattr(session.time, 'sleep', lambda _delay: None)
    monkeypatch.setattr(
        session, '_stage_forwarded_env',
        MagicMock(side_effect=('a' * 32, 'b' * 32)))
    monkeypatch.setattr(
        session, '_run_ssh', MagicMock(side_effect=(255, 0)))
    cleanup = MagicMock(side_effect=(False, True, True))
    monkeypatch.setattr(session, '_cleanup_windows_forwarded_env', cleanup)
    monkeypatch.setattr(session, 'wait_for_ssh', lambda *args, **kwargs: True)
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _runtime_meta(tmp_path)
    meta.update({
        'ssh_command_style': 'windows',
        'checkpoint_blocked_reason':
            session.WINDOWS_FORWARDING_CHECKPOINT_REASON,
    })

    assert session.run_attached_session(
        meta, ('powershell.exe',), forwarded_env=(('TOKEN', 'secret'),)
    ) == 0
    assert cleanup.call_args_list == [
        ((meta, 'a' * 32),),
        ((meta, 'a' * 32),),
        ((meta, 'b' * 32),),
    ]


def test_windows_failed_stage_retries_preserved_cleanup_id_in_finally(
        monkeypatch, tmp_path):
    watcher = MagicMock()
    watcher.reset_count = 0
    watcher.guest_shutdown = False
    monkeypatch.setattr(session, '_LifecycleWatcher', lambda _socket: watcher)
    monkeypatch.setattr(session.secrets, 'token_hex', lambda _size: 'e' * 32)
    monkeypatch.setattr(
        session.subprocess, 'run',
        lambda *_args, **_kwargs: MagicMock(returncode=86, stderr=b'private'))
    cleanup = MagicMock(side_effect=(False, True))
    monkeypatch.setattr(session, '_cleanup_windows_forwarded_env', cleanup)
    run_ssh = MagicMock()
    monkeypatch.setattr(session, '_run_ssh', run_ssh)
    monkeypatch.setattr(session, '_process_alive', lambda _pid: False)

    meta = _runtime_meta(tmp_path)
    meta.update({
        'ssh_command_style': 'windows',
        'checkpoint_blocked_reason':
            session.WINDOWS_FORWARDING_CHECKPOINT_REASON,
    })

    with pytest.raises(
            session.VmocsError,
            match='failed to stage forwarded environment in guest'):
        session.run_attached_session(
            meta, ('powershell.exe',),
            forwarded_env=(('TOKEN', 'secret'),))

    run_ssh.assert_not_called()
    assert cleanup.call_args_list == [
        ((meta, 'e' * 32),),
        ((meta, 'e' * 32),),
    ]


def test_windows_forwarding_rejects_session_save(tmp_path):
    meta = _runtime_meta(tmp_path)
    meta['ssh_command_style'] = 'windows'

    with pytest.raises(session.VmocsError, match='cannot be combined with saving'):
        session.run_attached_session(
            meta, ('powershell.exe',), save_path=str(tmp_path / 'save'),
            forwarded_env=(('TOKEN', 'secret'),))


def test_clean_logout_wins_over_delayed_reset_event(monkeypatch, tmp_path):
    class Watcher:
        def __init__(self, _socket):
            self.monitor = MagicMock()
            self.reset_count = 0
            self.guest_shutdown = False

        def drain(self):
            self.reset_count += 1

        def close(self):
            pass

    monkeypatch.setattr(session, '_LifecycleWatcher', Watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: True)
    ssh = MagicMock(return_value=_mock_ssh_process(0))
    monkeypatch.setattr(session.subprocess, 'Popen', ssh)
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, 'wait_for_ssh', MagicMock(return_value=True))
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _runtime_meta(tmp_path)

    assert session.run_attached_session(meta, ('bash', '-l')) == 0
    assert ssh.call_count == 1
    session.wait_for_ssh.assert_not_called()


def test_sidecar_failure_terminates_ssh_and_quits_qemu(monkeypatch):
    process = MagicMock()
    process.poll.side_effect = [None, None]
    process.wait.return_value = -15
    monkeypatch.setattr(session.subprocess, 'Popen', lambda _argv: process)
    monkeypatch.setattr(session.time, 'sleep', lambda _delay: None)

    watcher = MagicMock()
    manager = MagicMock()
    manager.failure.return_value = ('rocjitsu-0', 7)
    meta = _meta()
    meta['runtime_dir'] = '/tmp/nonexistent-vmocs-test-runtime'
    meta['_sidecar_manager'] = manager

    assert session._run_ssh(meta, ('true',), False, watcher) == -15
    watcher.monitor.quit.assert_called_once_with()
    process.terminate.assert_called_once_with()


def test_sidecar_exit_during_external_stop_does_not_fail_ssh(
        monkeypatch, tmp_path):
    (tmp_path / 'vm.json').write_text('{"state": "stopping"}')
    process = MagicMock()
    process.poll.side_effect = [None, 0]
    process.wait.return_value = 0
    monkeypatch.setattr(session.subprocess, 'Popen', lambda _argv: process)
    monkeypatch.setattr(session.time, 'sleep', lambda _delay: None)

    watcher = MagicMock()
    manager = MagicMock()
    manager.failure.return_value = ('swtpm', 0)
    meta = _meta()
    meta['runtime_dir'] = str(tmp_path)
    meta['_sidecar_manager'] = manager

    assert session._run_ssh(meta, ('true',), False, watcher) == 0
    watcher.monitor.quit.assert_not_called()
    process.terminate.assert_not_called()


def test_sidecar_exit_after_guest_shutdown_is_not_a_failure(monkeypatch):
    process = MagicMock()
    process.poll.side_effect = [None, 0]
    process.wait.return_value = 0
    monkeypatch.setattr(session.subprocess, 'Popen', lambda _argv: process)
    monkeypatch.setattr(session.time, 'sleep', lambda _delay: None)

    watcher = MagicMock()
    watcher.guest_shutdown = True
    manager = MagicMock()
    manager.failure.return_value = ('swtpm', 0)
    meta = _meta()
    meta['runtime_dir'] = '/tmp/nonexistent-vmocs-test-runtime'
    meta['_sidecar_manager'] = manager

    assert session._run_ssh(meta, ('true',), False, watcher) == 0
    watcher.monitor.quit.assert_not_called()
    process.terminate.assert_not_called()


def test_stop_request_terminates_ssh_without_waiting_for_transport(monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = -15
    monkeypatch.setattr(session.subprocess, 'Popen', lambda _argv: process)

    watcher = MagicMock()
    assert session._run_ssh(
        _meta(), ('true',), False, watcher,
        stop_requested=lambda: True) == -15

    process.terminate.assert_called_once_with()


def test_checkpoint_failure_preserves_stopped_runtime(monkeypatch, tmp_path):
    watcher = MagicMock()
    watcher.reset_count = 0
    watcher.guest_shutdown = False
    monkeypatch.setattr(session, '_LifecycleWatcher', lambda _socket: watcher)
    monkeypatch.setattr(session.os, 'isatty', lambda _fd: False)
    monkeypatch.setattr(
        session.subprocess, 'Popen',
        lambda _argv: _mock_ssh_process(0))
    monkeypatch.setattr(session, '_process_alive', lambda _pid: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session, 'create_checkpoint',
        MagicMock(side_effect=RuntimeError('save failed')))

    meta = _runtime_meta(tmp_path)
    meta['save_path'] = str(tmp_path / 'checkpoint')
    meta['checkpoint_id'] = 'checkpoint-42'

    with pytest.raises(RuntimeError, match='save failed'):
        session.run_attached_session(meta, ('true',))

    assert (tmp_path / 'runtime' / 'disk.qcow2').exists()
