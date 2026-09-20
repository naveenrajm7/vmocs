"""CLI lifecycle supervision tests."""

from unittest.mock import MagicMock

import pytest

pytest.importorskip('click')

from vmocs import VmocsError
from vmocs import cli


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
