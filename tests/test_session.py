"""Unit tests for seamless SSH guest sessions."""

from unittest.mock import MagicMock

from vmocs import session
from vmocs.session import build_ssh_command


def _meta(key_path='/tmp/vmocs/42/id_ed25519'):
    return {
        'key_path': key_path,
        'ssh_port': 60222,
        'ssh_user': 'ubuntu',
    }


def _mock_ssh_process(status):
    process = MagicMock()
    process.poll.return_value = status
    process.wait.return_value = status
    return process


def test_build_ssh_command_preserves_remote_arguments():
    argv = build_ssh_command(
        _meta(), ('bash', '-c', 'printf "%s\\n" "$1"', 'bash', 'hello world'))

    assert argv[0] == 'ssh'
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


def test_build_ssh_command_allows_configured_ssh_identity():
    argv = build_ssh_command(_meta(key_path=None), ())

    assert '-i' not in argv
    assert argv[-1] == 'ubuntu@127.0.0.1'


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
    monkeypatch.setattr(session, 'wait_for_ssh', lambda *args: True)
    monkeypatch.setattr(session, '_wait_for_qemu', lambda *args, **kwargs: True)

    meta = _meta()
    meta.update({
        'pid': 123,
        'qmp_socket': '/tmp/qmp.sock',
        'runtime_dir': str(tmp_path / 'runtime'),
        'overlay': str(tmp_path / 'disk.qcow2'),
    })

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

    meta = _meta()
    meta.update({
        'pid': 123,
        'qmp_socket': '/tmp/qmp.sock',
        'runtime_dir': str(tmp_path / 'runtime'),
        'overlay': str(tmp_path / 'disk.qcow2'),
    })

    assert session.run_attached_session(meta, ('train.py',)) == 255
    assert ssh.call_count == 1


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

    meta = _meta()
    meta.update({
        'pid': 123,
        'qmp_socket': '/tmp/qmp.sock',
        'runtime_dir': str(tmp_path / 'runtime'),
        'overlay': str(tmp_path / 'disk.qcow2'),
    })

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
