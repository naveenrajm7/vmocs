"""CLI lifecycle supervision tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip('click')

from vmocs import VmocsError
from vmocs import cli


def test_run_rejects_windows_forward_env_with_save_before_launch(monkeypatch):
    from click.testing import CliRunner

    template = SimpleNamespace(
        ssh_command_style='windows', insert_key=True, boot_mode='vagrant')
    monkeypatch.setattr(cli, '_load', lambda _path: (object(), {'win': template}))
    launch = MagicMock()
    monkeypatch.setattr(cli, 'launch_vm', launch)

    result = CliRunner().invoke(
        cli.cli,
        ['run', 'win', '--save', '/checkpoint', '--forward-env', 'TOKEN',
         '--', 'powershell.exe'],
        env={'TOKEN': 'secret'})

    assert result.exit_code == 1
    assert isinstance(result.exception, cli.VmocsError)
    assert 'cannot be combined with --save' in str(result.exception)
    launch.assert_not_called()


def test_run_rejects_windows_forward_env_without_ephemeral_key(monkeypatch):
    from click.testing import CliRunner

    template = SimpleNamespace(
        ssh_command_style='windows', insert_key=False, boot_mode='vagrant')
    monkeypatch.setattr(cli, '_load', lambda _path: (object(), {'win': template}))
    launch = MagicMock()
    monkeypatch.setattr(cli, 'launch_vm', launch)

    result = CliRunner().invoke(
        cli.cli,
        ['run', 'win', '--forward-env', 'TOKEN', '--', 'powershell.exe'],
        env={'TOKEN': 'secret'})

    assert result.exit_code == 1
    assert isinstance(result.exception, cli.VmocsError)
    assert 'insert-key: true' in str(result.exception)
    launch.assert_not_called()


def test_run_rejects_windows_forward_env_without_vagrant_handoff(monkeypatch):
    from click.testing import CliRunner

    template = SimpleNamespace(
        ssh_command_style='windows', insert_key=True, boot_mode='cloud-init')
    monkeypatch.setattr(cli, '_load', lambda _path: (object(), {'win': template}))
    launch = MagicMock()
    monkeypatch.setattr(cli, 'launch_vm', launch)

    result = CliRunner().invoke(
        cli.cli,
        ['run', 'win', '--forward-env', 'TOKEN', '--', 'powershell.exe'],
        env={'TOKEN': 'secret'})

    assert result.exit_code == 1
    assert isinstance(result.exception, cli.VmocsError)
    assert 'boot-mode: vagrant' in str(result.exception)
    launch.assert_not_called()


def test_run_marks_windows_forwarding_as_not_checkpointable(monkeypatch):
    from click.testing import CliRunner

    template = SimpleNamespace(
        ssh_command_style='windows', insert_key=True, boot_mode='vagrant')
    monkeypatch.setattr(cli, '_load', lambda _path: (object(), {'win': template}))
    launch = MagicMock(return_value={
        'job_id': 42,
        'key_path': '/runtime/id_ed25519',
        'ssh_port': 60222,
        'ssh_user': 'vagrant',
        'ssh_timeout': 180,
    })
    monkeypatch.setattr(cli, 'launch_vm', launch)
    attached = MagicMock(return_value=0)
    monkeypatch.setattr(cli, 'run_attached_session', attached)

    result = CliRunner().invoke(
        cli.cli,
        ['run', 'win', '--forward-env', 'TOKEN', '--', 'powershell.exe'],
        env={'TOKEN': 'secret'})

    assert result.exit_code == 0
    assert launch.call_args.kwargs['checkpoint_blocked_reason'] == (
        cli.WINDOWS_FORWARDING_CHECKPOINT_REASON)
    assert launch.call_args.kwargs['pin_ssh_host_key'] is True
    attached.assert_called_once()


def test_blocking_launch_fails_when_critical_sidecar_exits(monkeypatch):
    pid = 4321
    waits = iter([(0, 0), (pid, 0)])
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(cli.os, 'waitpid', lambda *_args: next(waits))
    monkeypatch.setattr(cli.time, 'sleep', lambda _delay: None)
    kill = MagicMock()
    monkeypatch.setattr('vmocs.launch._kill_qemu', kill)

    class Monitor:
        def __init__(self, _path):
            pass

        def quit(self):
            pass

        def close(self):
            pass

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(cli, 'QemuMonitor', Monitor)
    monkeypatch.setattr(cli.threading, 'Thread', ImmediateThread)
    manager = MagicMock()
    manager.failure.return_value = ('rocjitsu-0', 7)

    with pytest.raises(VmocsError, match='rocjitsu-0 exited with status 7'):
        cli._block_until_exit(pid, '/tmp/qmp.sock', manager)

    kill.assert_called_once_with(pid, timeout=2)


def test_blocking_launch_accepts_sidecar_exit_during_external_stop(
        monkeypatch, tmp_path):
    pid = 4321
    waits = iter([(0, 0), (pid, 0)])
    (tmp_path / 'vm.json').write_text('{"state": "stopping"}')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(cli.os, 'waitpid', lambda *_args: next(waits))
    monkeypatch.setattr(cli.time, 'sleep', lambda _delay: None)
    kill = MagicMock()
    monkeypatch.setattr('vmocs.launch._kill_qemu', kill)
    manager = MagicMock()
    manager.failure.return_value = ('swtpm', 0)

    cli._block_until_exit(pid, str(tmp_path / 'qmp.sock'), manager)

    kill.assert_not_called()


def test_blocking_launch_accepts_clean_sidecar_exit_as_qemu_exits(monkeypatch):
    pid = 4321
    waits = iter([(0, 0), (pid, 0)])
    monkeypatch.setattr(cli.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(cli.os, 'waitpid', lambda *_args: next(waits))
    monkeypatch.setattr(cli.time, 'sleep', lambda _delay: None)
    manager = MagicMock()
    manager.failure.return_value = ('swtpm', 0)

    cli._block_until_exit(pid, '/tmp/qmp.sock', manager)
