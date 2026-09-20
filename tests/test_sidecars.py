"""Unit and process-level tests for VM sidecar planning and ownership."""

import json
import os
import signal
import sys
from unittest.mock import MagicMock

import pytest

from vmocs.error import HypervisorError, InvalidConfigError
from vmocs.sidecars import (
    ProcessSpec,
    SidecarManager,
    SidecarPlan,
    has_vfio_user,
    plan_sidecars,
    preflight_qemu,
    preflight_sidecars,
    process_start_ticks,
    stop_persisted_sidecars,
)


class FakeConfig:
    def __init__(self, config_path=None):
        self.sidecars = {
            'startup-timeout': 1,
            'stop-timeout': 0.5,
            'swtpm': {'binary': sys.executable},
            'virtiofsd': {'binary': sys.executable},
            'rocm-ernic': {'binary': sys.executable},
            'rocjitsu': {
                'binary': sys.executable,
                'profiles': {'test': config_path},
            },
        }


class FakeTemplate:
    machine_type = 'q35'
    tpm = True
    mount_points = {
        'home': {'path': '/tmp', 'type': 'virtio-fs'},
        'scratch': {'path': '/tmp', 'type': 'virtio-9p'},
    }
    emulated_devices = [
        {'type': 'rocm-ernic'},
        {'type': 'rocjitsu', 'profile': 'test'},
    ]


def _process_plan(tmp_path, name, code, timeout=2):
    socket_path = str(tmp_path / f'{name}.sock')
    spec = ProcessSpec(
        name=name,
        argv=(sys.executable, '-c', code, socket_path),
        sockets=(socket_path,),
        log_path=str(tmp_path / f'{name}.log'),
        startup_timeout=timeout,
        stop_timeout=0.2,
        directories=(str(tmp_path),),
    )
    return SidecarPlan(kind='test', process=spec, qemu_args=())


def test_plan_all_supported_sidecars(tmp_path):
    config_path = tmp_path / 'rocjitsu.json'
    config_path.write_text('{}')

    plans = plan_sidecars(
        FakeConfig(str(config_path)), FakeTemplate(), str(tmp_path / 'rt'))

    assert [plan.process.name for plan in plans] == [
        'swtpm', 'virtiofsd-0', 'rocm-ernic-0', 'rocjitsu-0']
    assert has_vfio_user(plans)
    assert [plan.requires_shared_guest_memory for plan in plans] == [
        False, True, True, True]

    ernic = plans[2]
    assert ernic.process.argv[-2] == '-s'
    assert ernic.process.argv[-1].endswith('/vfu/ernic-0.sock')
    ernic_device = json.loads(ernic.qemu_args[1])
    assert ernic_device['driver'] == 'vfio-user-pci'
    assert ernic_device['rombar'] == 0

    rocjitsu = plans[3]
    assert tuple(rocjitsu.process.argv[1:3]) == (
        '--config', str(config_path))
    assert rocjitsu.process.argv[-2] == '--vfio-socket'
    assert rocjitsu.process.required_files == (str(config_path),)


def test_unknown_emulated_device_is_rejected(tmp_path):
    class UnknownTemplate(FakeTemplate):
        tpm = False
        mount_points = {}
        emulated_devices = [{'type': 'unknown'}]

    with pytest.raises(InvalidConfigError, match='unknown emulated device'):
        plan_sidecars(FakeConfig(), UnknownTemplate(), str(tmp_path))


def test_unknown_rocjitsu_profile_is_rejected(tmp_path):
    class UnknownProfileTemplate(FakeTemplate):
        tpm = False
        mount_points = {}
        emulated_devices = [{'type': 'rocjitsu', 'profile': 'missing'}]

    with pytest.raises(InvalidConfigError, match='unknown rocJitsu profile'):
        plan_sidecars(FakeConfig(), UnknownProfileTemplate(), str(tmp_path))


def test_emulated_devices_must_be_a_list(tmp_path):
    class BadTemplate(FakeTemplate):
        tpm = False
        mount_points = {}
        emulated_devices = 'rocjitsu'

    with pytest.raises(InvalidConfigError, match='must be a list'):
        plan_sidecars(FakeConfig(), BadTemplate(), str(tmp_path))


def test_virtiofs_shared_directory_is_preflighted(tmp_path):
    class MissingShareTemplate(FakeTemplate):
        tpm = False
        mount_points = {
            'missing': {
                'path': str(tmp_path / 'does-not-exist'),
                'type': 'virtio-fs',
            },
        }
        emulated_devices = []

    plans = plan_sidecars(
        FakeConfig(), MissingShareTemplate(), str(tmp_path / 'rt'))
    with pytest.raises(HypervisorError, match='shared directory'):
        preflight_sidecars(plans)


def test_sidecar_binary_must_be_a_string(tmp_path):
    cfg = FakeConfig()
    cfg.sidecars['rocm-ernic']['binary'] = None

    with pytest.raises(InvalidConfigError, match='binary must be'):
        plan_sidecars(cfg, FakeTemplate(), str(tmp_path))


def test_manager_waits_for_readiness_and_stops_group(tmp_path, monkeypatch):
    code = (
        'import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); '
        'time.sleep(60)')
    # AF_UNIX creation is blocked by the unit-test sandbox. Keep this test at
    # the manager boundary; the regular-file test below exercises the real
    # production readiness predicate independently.
    monkeypatch.setattr(
        'vmocs.sidecars._socket_ready', lambda path: os.path.exists(path))
    manager = SidecarManager([_process_plan(tmp_path, 'ready', code)])

    manager.start_all()
    try:
        path = manager.plans[0].process.sockets[0]
        assert os.path.exists(path)
        assert manager.failure() is None
        assert process_start_ticks(manager.handles[0].process.pid) is not None
    finally:
        manager.stop_all()

    assert manager.handles[0].process.poll() is not None


def test_manager_reports_early_exit_and_rolls_back(tmp_path):
    code = 'import sys; print("startup failed", flush=True); sys.exit(7)'
    manager = SidecarManager([
        _process_plan(tmp_path, 'early-exit', code, timeout=1)])

    with pytest.raises(HypervisorError, match='status 7') as exc:
        manager.start_all()

    assert 'startup failed' in str(exc.value)
    assert manager.handles[0].process.poll() == 7


def test_later_startup_failure_rolls_back_ready_sidecar(tmp_path, monkeypatch):
    ready = (
        'import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); '
        'time.sleep(60)')
    fail = 'import sys,time; time.sleep(0.1); sys.exit(9)'
    monkeypatch.setattr(
        'vmocs.sidecars._socket_ready', lambda path: os.path.exists(path))
    manager = SidecarManager([
        _process_plan(tmp_path, 'first', ready),
        _process_plan(tmp_path, 'second', fail),
    ])

    with pytest.raises(HypervisorError, match='status 9'):
        manager.start_all()

    assert manager.handles[0].process.poll() is not None
    assert manager.handles[1].process.poll() == 9


def test_regular_file_does_not_satisfy_socket_readiness(tmp_path):
    code = (
        'import pathlib,sys,time; '
        'pathlib.Path(sys.argv[1]).write_text("not a socket"); time.sleep(60)')
    manager = SidecarManager([
        _process_plan(tmp_path, 'regular-file', code, timeout=0.2)])

    with pytest.raises(HypervisorError, match='readiness timed out'):
        manager.start_all()

    assert manager.handles[0].process.poll() is not None


def test_vfio_preflight_rejects_snapshots_before_start(tmp_path):
    config_path = tmp_path / 'rocjitsu.json'
    config_path.write_text('{}')
    plans = plan_sidecars(
        FakeConfig(str(config_path)), FakeTemplate(), str(tmp_path / 'rt'))

    with pytest.raises(HypervisorError, match='snapshots'):
        preflight_qemu('/bin/true', FakeTemplate(), plans, using_snapshot=True)


def test_vfio_preflight_requires_qemu_capability(tmp_path):
    config_path = tmp_path / 'rocjitsu.json'
    config_path.write_text('{}')
    plans = plan_sidecars(
        FakeConfig(str(config_path)), FakeTemplate(), str(tmp_path / 'rt'))

    with pytest.raises(HypervisorError, match='does not support vfio-user-pci'):
        preflight_qemu('/bin/true', FakeTemplate(), plans)


def test_persisted_cleanup_signals_only_verified_process(monkeypatch):
    alive = [True]
    signals = []
    monkeypatch.setattr('vmocs.sidecars.read_boot_id', lambda: 'boot-a')
    monkeypatch.setattr(
        'vmocs.sidecars.process_start_ticks',
        lambda _pid: 42 if alive[0] else None)

    def killpg(pgid, sig):
        signals.append((pgid, sig))
        alive[0] = False

    monkeypatch.setattr('vmocs.sidecars.os.killpg', killpg)
    stop_persisted_sidecars({
        'boot_id': 'boot-a',
        'sidecars': [{'pid': 123, 'pgid': 123, 'start_ticks': 42}],
    })

    assert signals == [(123, signal.SIGTERM)]


def test_persisted_cleanup_ignores_rebooted_host(monkeypatch):
    killpg = MagicMock()
    monkeypatch.setattr('vmocs.sidecars.read_boot_id', lambda: 'new-boot')
    monkeypatch.setattr('vmocs.sidecars.os.killpg', killpg)

    stop_persisted_sidecars({
        'boot_id': 'old-boot',
        'sidecars': [{'pid': 123, 'pgid': 123, 'start_ticks': 42}],
    })

    killpg.assert_not_called()
