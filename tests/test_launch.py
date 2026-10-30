"""Tests for VM launch readiness helpers."""

import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vmocs.error import HypervisorError
from vmocs.keys import VAGRANT_KEY
from vmocs.launch import (
    _SSH_AUTHENTICATED,
    _SSH_INDETERMINATE,
    _SSH_REJECTED,
    _capture_ssh_host_keys,
    _is_guest_reboot_qmp_event,
    _is_host_reset_qmp_event,
    _is_per_launch_ssh_key,
    _requires_authenticated_ssh_readiness,
    _rotate_vagrant_key,
    _rotate_windows_vagrant_key,
    _ssh_key_authentication_result,
    _ssh_key_authenticates,
    _verify_windows_ssh_handoff,
    launch_vm,
    _write_metadata,
    teardown_vm,
    wait_for_ssh,
)


class _BannerSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, _size):
        return b'SSH-2.0-OpenSSH_9.6\r\n'


def test_authenticated_ssh_readiness_retries_login(monkeypatch):
    statuses = iter([False, True])
    calls = []
    monkeypatch.setattr(
        'vmocs.launch.socket.create_connection',
        lambda *args, **kwargs: _BannerSocket())
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authenticates',
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(statuses))
    monkeypatch.setattr('vmocs.launch.time.sleep', lambda _seconds: None)

    assert wait_for_ssh(
        '127.0.0.1', 60222, '/tmp/key', 1,
        ssh_user='cluster-user', authenticate=True)
    assert len(calls) == 2
    assert calls[0][0][:4] == (
        '127.0.0.1', 60222, '/tmp/key', 'cluster-user')


def test_authenticated_ssh_readiness_uses_pinned_host_key(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'vmocs.launch.socket.create_connection',
        lambda *args, **kwargs: _BannerSocket())
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authenticates',
        lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    assert wait_for_ssh(
        '127.0.0.1', 60222, '/tmp/key', 1,
        authenticate=True, known_hosts_path='/runtime/ssh_known_hosts')
    assert calls[0][1]['known_hosts_path'] == '/runtime/ssh_known_hosts'


def test_banner_only_ssh_readiness_does_not_attempt_login(monkeypatch):
    authenticate = MagicMock()
    monkeypatch.setattr(
        'vmocs.launch.socket.create_connection',
        lambda *args, **kwargs: _BannerSocket())
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authenticates', authenticate)

    assert wait_for_ssh('127.0.0.1', 60222, '/tmp/key', 1)
    authenticate.assert_not_called()


def test_ssh_key_authentication_uses_only_the_requested_identity(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('vmocs.launch.subprocess.run', fake_run)

    assert _ssh_key_authenticates(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant',
        known_hosts_path='/runtime/ssh_known_hosts',
        isolated_identity=True)

    argv = captured['argv']
    assert argv[:4] == ['ssh', '-vvv', '-F', '/etc/ssh/ssh_config']
    assert argv[argv.index('-i') + 1] == '/runtime/id_ed25519'
    for option in (
            'IdentitiesOnly=yes', 'IdentityAgent=none',
            'CertificateFile=none', 'PreferredAuthentications=publickey',
            'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no',
            'GSSAPIAuthentication=no', 'ControlMaster=no',
            'ControlPath=none', 'StrictHostKeyChecking=yes'):
        assert option in argv
    assert captured['stdin'] == subprocess.DEVNULL
    assert captured['stderr'] == subprocess.PIPE
    assert captured['env']['LC_ALL'] == 'C'
    assert 'LogLevel=ERROR' not in argv


def test_ssh_key_authentication_preserves_legacy_config_by_default(
        monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr('vmocs.launch.subprocess.run', fake_run)

    assert _ssh_key_authenticates(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant')
    assert captured['argv'][:4] == [
        'ssh', '-vvv', '-i', '/runtime/id_ed25519']
    assert '-F' not in captured['argv']
    assert 'IdentitiesOnly=yes' not in captured['argv']


def test_ssh_key_authentication_classifies_explicit_denial(monkeypatch):
    diagnostic = b'''\
debug3: receive packet: type 51
debug1: Offering public key: /runtime/id_ed25519 ED25519 SHA256:test explicit
debug3: send packet: type 50
debug2: we sent a publickey packet, wait for reply
debug3: receive packet: type 51
debug1: Authentications that can continue: publickey
vagrant@127.0.0.1: Permission denied (publickey).
'''
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=255, stderr=diagnostic))

    assert _ssh_key_authentication_result(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant') == (
            _SSH_REJECTED)


@pytest.mark.parametrize('stderr', (
    b'Connection reset by 127.0.0.1 port 60222\n',
    b'Host key verification failed.\n',
    b'vagrant@127.0.0.1: Permission denied (publickey).\n',
    (b'debug1: Will attempt key: /runtime/id_ed25519 explicit\n'
     b'debug3: receive packet: type 51\n'
     b'vagrant@127.0.0.1: Permission denied (publickey).\n'),
    (b'debug1: Offering public key: /runtime/id_ed25519 ED25519 '
     b'SHA256:test explicit\n'
     b'debug3: receive packet: type 51\n'
     b'vagrant@127.0.0.1: Permission denied (publickey).\n'),
    (b'Load key "/runtime/id_ed25519": error in libcrypto\n'
     b'vagrant@127.0.0.1: Permission denied (publickey).\n'),
))
def test_ssh_key_authentication_does_not_misclassify_client_errors(
        monkeypatch, stderr):
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=255, stderr=stderr))

    assert _ssh_key_authentication_result(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant') == (
            _SSH_INDETERMINATE)


@pytest.mark.parametrize('exception', (
    OSError('ssh unavailable'),
    subprocess.TimeoutExpired('ssh', 10),
))
def test_ssh_key_authentication_classifies_start_and_timeout_errors(
        monkeypatch, exception):
    def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr('vmocs.launch.subprocess.run', fail)

    assert _ssh_key_authentication_result(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant') == (
            _SSH_INDETERMINATE)


def test_capture_ssh_host_keys_writes_private_pin(monkeypatch, tmp_path):
    line = b'[127.0.0.1]:60222 ssh-ed25519 AAAATESTKEY\n'
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *args, **kwargs: type('Result', (), {
            'returncode': 0, 'stdout': line})())

    path = _capture_ssh_host_keys(
        '127.0.0.1', 60222, str(tmp_path), timeout=1)

    assert path == str(tmp_path / 'ssh_known_hosts')
    assert (tmp_path / 'ssh_known_hosts').read_bytes() == line
    assert (tmp_path / 'ssh_known_hosts').stat().st_mode & 0o777 == 0o600


def test_posix_vagrant_rotation_preserves_legacy_ssh_configuration(
        monkeypatch):
    captured = {}

    def fake_call(argv, **kwargs):
        captured['argv'] = argv
        captured.update(kwargs)
        return 0

    monkeypatch.setattr('vmocs.launch.subprocess.call', fake_call)

    _rotate_vagrant_key(
        '127.0.0.1', 60222, 'ssh-ed25519 AAAATEST generated@test', 1)

    assert captured['argv'][:3] == ['scp', '-i', VAGRANT_KEY]
    assert '-F' not in captured['argv']
    assert 'IdentitiesOnly=yes' not in captured['argv']
    assert 'BatchMode=yes' not in captured['argv']
    assert 'stdin' not in captured


def test_windows_key_rotation_sends_public_key_only_on_stdin(monkeypatch):
    captured = {}
    ordering = []

    def fake_run(argv, **kwargs):
        ordering.append('run')
        captured['argv'] = argv
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=b'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED\r\n')

    monkeypatch.setattr('vmocs.launch.subprocess.run', fake_run)
    monkeypatch.setattr('vmocs.launch.wait_for_ssh', lambda *a, **k: True)
    public_key = 'ssh-ed25519 AAAATEST generated@test'
    reboot_observed = MagicMock()
    qmp_barrier = MagicMock(side_effect=lambda: ordering.append('barrier'))
    reboot_observed.clear.side_effect = lambda: ordering.append('clear')
    reboot_observed.wait.side_effect = (
        lambda _timeout: ordering.append('wait') or True)

    _rotate_windows_vagrant_key(
        '127.0.0.1', 60222, public_key, 1,
        known_hosts_path='/runtime/ssh_known_hosts',
        ephemeral_key_path='/runtime/id_ed25519',
        reboot_observed=reboot_observed,
        qmp_barrier=qmp_barrier)

    assert captured['input'] == (public_key + '\n').encode('ascii')
    assert public_key not in '\0'.join(captured['argv'])
    assert 'StrictHostKeyChecking=yes' in captured['argv']
    assert 'IdentitiesOnly=yes' in captured['argv']
    encoded = captured['argv'][-1].split('-EncodedCommand ', 1)[1]
    script = base64.b64decode(encoded).decode('utf-16le')
    assert 'administrators_authorized_keys' in script
    assert 'WindowsPrincipal' not in script
    assert 'IsInRole' not in script
    assert "GetFolderPath('System'),'whoami.exe'" in script
    assert '/groups /fo csv /nh' in script
    assert 'ConvertFrom-Csv' in script
    assert 'S-1-5-32-544' in script
    assert "if($parsedGroups.Count -eq 0){throw 'groups'}" in script
    assert '[Security.Principal.SecurityIdentifier]::new' in script
    assert "catch{throw 'groups'}" in script
    assert 'break' not in script
    assert script.index("if($LASTEXITCODE -ne 0){throw 'groups'}") < (
        script.index('if($isAdministrator)'))
    assert '[IO.FileMode]::Truncate' in script
    assert '[IO.FileShare]::None' in script
    assert '$stream.Flush($true)' in script
    assert script.index('if($actual -cne $key)') < script.index(
        'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED')
    assert script.index('VMOCS_KEY_ROTATED_REBOOT_SCHEDULED') < script.index(
        '& $shutdown /r /t 5 /f')
    assert len(captured['argv'][-1]) < 8191
    assert 0 < captured['timeout'] <= 1
    assert reboot_observed.clear.call_count == 2
    reboot_observed.wait.assert_called_once()
    qmp_barrier.assert_called_once_with()
    assert ordering == ['clear', 'run', 'barrier', 'clear', 'wait']


def test_windows_key_rotation_accepts_marker_from_timed_out_ssh(monkeypatch):
    marker = b'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED\r\n'

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('ssh', 15, output=marker)

    monkeypatch.setattr('vmocs.launch.subprocess.run', time_out)
    monkeypatch.setattr('vmocs.launch.wait_for_ssh', lambda *a, **k: True)
    reboot_observed = MagicMock()
    reboot_observed.wait.return_value = True
    qmp_barrier = MagicMock()

    _rotate_windows_vagrant_key(
        '127.0.0.1', 60222, 'ssh-ed25519 AAAATEST generated@test', 1,
        known_hosts_path='/runtime/ssh_known_hosts',
        ephemeral_key_path='/runtime/id_ed25519',
        reboot_observed=reboot_observed,
        qmp_barrier=qmp_barrier)

    assert reboot_observed.clear.call_count == 2
    reboot_observed.wait.assert_called_once()
    qmp_barrier.assert_called_once_with()


def test_windows_key_rotation_recovers_lost_marker_after_qmp_reboot(
        monkeypatch):
    run = MagicMock(return_value=SimpleNamespace(returncode=255, stdout=b''))
    wait_for_ssh = MagicMock(return_value=True)
    monkeypatch.setattr('vmocs.launch.subprocess.run', run)
    monkeypatch.setattr('vmocs.launch.wait_for_ssh', wait_for_ssh)
    reboot_observed = MagicMock()
    reboot_observed.is_set.return_value = True
    qmp_barrier = MagicMock()
    force_reset = MagicMock()
    forced_reset_observed = MagicMock()
    forced_reset_observed.wait.return_value = True

    _rotate_windows_vagrant_key(
        '127.0.0.1', 60222, 'ssh-ed25519 AAAATEST generated@test', 1,
        ssh_user='vagrant', known_hosts_path='/runtime/ssh_known_hosts',
        ephemeral_key_path='/runtime/id_ed25519',
        reboot_observed=reboot_observed, qmp_barrier=qmp_barrier,
        force_reset=force_reset,
        forced_reset_observed=forced_reset_observed)

    run.assert_called_once()
    reboot_observed.clear.assert_called_once_with()
    reboot_observed.wait.assert_not_called()
    assert wait_for_ssh.call_count == 2
    assert all(call.args[:3] == (
        '127.0.0.1', 60222, '/runtime/id_ed25519'
    ) for call in wait_for_ssh.call_args_list)
    assert all(call.kwargs == {
        'authenticate': True,
        'known_hosts_path': '/runtime/ssh_known_hosts',
        'isolated_identity': True,
    } for call in wait_for_ssh.call_args_list)
    assert qmp_barrier.call_count == 1
    force_reset.assert_called_once_with()
    assert forced_reset_observed.clear.call_count == 2
    forced_reset_observed.wait.assert_called_once()


def test_windows_key_rotation_lost_marker_still_requires_ephemeral_key(
        monkeypatch):
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *a, **k: SimpleNamespace(returncode=255, stdout=b''))
    monkeypatch.setattr('vmocs.launch.wait_for_ssh', lambda *a, **k: False)
    reboot_observed = MagicMock()
    reboot_observed.is_set.return_value = True

    with pytest.raises(HypervisorError, match='observed key-rotation reboot'):
        _rotate_windows_vagrant_key(
            '127.0.0.1', 60222,
            'ssh-ed25519 AAAATEST generated@test', 1,
            known_hosts_path='/runtime/ssh_known_hosts',
            ephemeral_key_path='/runtime/id_ed25519',
            reboot_observed=reboot_observed,
            qmp_barrier=MagicMock())


def test_windows_key_rotation_requires_qmp_reset(monkeypatch):
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *a, **k: SimpleNamespace(
            returncode=255,
            stdout=b'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED\n'))
    reboot_observed = MagicMock()
    reboot_observed.wait.return_value = False

    with pytest.raises(HypervisorError, match='guest-reset event'):
        _rotate_windows_vagrant_key(
            '127.0.0.1', 60222,
            'ssh-ed25519 AAAATEST generated@test', 1,
            known_hosts_path='/runtime/ssh_known_hosts',
            ephemeral_key_path='/runtime/id_ed25519',
            reboot_observed=reboot_observed,
            qmp_barrier=MagicMock())


@pytest.mark.parametrize('name', ('RESET', 'SHUTDOWN'))
def test_guest_reboot_qmp_event_accepts_documented_variants(name):
    assert _is_guest_reboot_qmp_event({
        'event': name,
        'data': {'guest': True, 'reason': 'guest-reset'},
    })


@pytest.mark.parametrize('event', (
    None,
    {'event': 'RESET', 'data': {'reason': 'host-qmp-system-reset'}},
    {'event': 'SHUTDOWN', 'data': {'reason': 'guest-shutdown'}},
    {'event': 'STOP'},
))
def test_guest_reboot_qmp_event_rejects_non_reboots(event):
    assert not _is_guest_reboot_qmp_event(event)


def test_host_reset_qmp_event_accepts_only_qmp_system_reset():
    assert _is_host_reset_qmp_event({
        'event': 'RESET',
        'data': {'guest': False, 'reason': 'host-qmp-system-reset'},
    })
    assert not _is_host_reset_qmp_event({
        'event': 'RESET',
        'data': {'guest': True, 'reason': 'guest-reset'},
    })


def test_windows_key_rotation_requires_new_key_after_reset(monkeypatch):
    monkeypatch.setattr(
        'vmocs.launch.subprocess.run',
        lambda *a, **k: SimpleNamespace(
            returncode=255,
            stdout=b'VMOCS_KEY_ROTATED_REBOOT_SCHEDULED\n'))
    monkeypatch.setattr('vmocs.launch.wait_for_ssh', lambda *a, **k: False)
    reboot_observed = MagicMock()
    reboot_observed.wait.return_value = True

    with pytest.raises(HypervisorError, match='was not accepted after reboot'):
        _rotate_windows_vagrant_key(
            '127.0.0.1', 60222,
            'ssh-ed25519 AAAATEST generated@test', 1,
            known_hosts_path='/runtime/ssh_known_hosts',
            ephemeral_key_path='/runtime/id_ed25519',
            reboot_observed=reboot_observed,
            qmp_barrier=MagicMock())


def test_windows_ssh_handoff_checks_new_old_new(monkeypatch):
    calls = []
    statuses = iter((_SSH_AUTHENTICATED, _SSH_REJECTED, _SSH_AUTHENTICATED))
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authentication_result',
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(statuses))

    _verify_windows_ssh_handoff(
        '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant',
        '/runtime/ssh_known_hosts')

    assert [call[0][2] for call in calls] == [
        '/runtime/id_ed25519',
        VAGRANT_KEY,
        '/runtime/id_ed25519',
    ]
    assert all(call[1]['isolated_identity'] for call in calls)


def test_windows_ssh_handoff_rejects_live_bootstrap_key(monkeypatch):
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authentication_result',
        MagicMock(side_effect=(_SSH_AUTHENTICATED, _SSH_AUTHENTICATED)))

    with pytest.raises(HypervisorError, match='bootstrap SSH key is still accepted'):
        _verify_windows_ssh_handoff(
            '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant',
            '/runtime/ssh_known_hosts')


def test_windows_ssh_handoff_fails_closed_on_indeterminate_bootstrap_probe(
        monkeypatch):
    probe = MagicMock(side_effect=(
        _SSH_AUTHENTICATED, _SSH_INDETERMINATE, _SSH_AUTHENTICATED))
    monkeypatch.setattr(
        'vmocs.launch._ssh_key_authentication_result', probe)

    with pytest.raises(HypervisorError, match='revocation could not be proven'):
        _verify_windows_ssh_handoff(
            '127.0.0.1', 60222, '/runtime/id_ed25519', 'vagrant',
            '/runtime/ssh_known_hosts')

    assert probe.call_count == 2


def test_metadata_write_can_tolerate_removed_runtime(tmp_path):
    runtime = tmp_path / 'already-removed'

    assert _write_metadata(str(runtime), {'state': 'stopping'},
                           missing_ok=True) is False


@pytest.mark.parametrize(
    'insert_key,using_snapshot,boot_mode,expected', (
        (True, False, 'cloud-init', True),
        (True, True, 'cloud-init', False),
        (True, True, 'vagrant', True),
        (False, False, 'vagrant', False),
    ))
def test_per_launch_ssh_key_provenance(
        insert_key, using_snapshot, boot_mode, expected):
    assert _is_per_launch_ssh_key(
        insert_key, using_snapshot, boot_mode) is expected


@pytest.mark.parametrize(
    'host_user,secure_windows_handoff,expected', (
        (None, False, False),
        ({'name': 'submitter'}, False, True),
        (None, True, True),
    ))
def test_authenticated_readiness_is_limited_to_identity_bound_launches(
        host_user, secure_windows_handoff, expected):
    assert _requires_authenticated_ssh_readiness(
        host_user, secure_windows_handoff) is expected


def test_windows_forwarding_rejects_cloud_init_snapshot_before_runtime(
        tmp_path):
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    (snapshot / 'memory').write_bytes(b'memory')
    template = SimpleNamespace(
        snapshot=str(snapshot), boot_mode='cloud-init', insert_key=True,
        ssh_command_style='windows')
    cfg = SimpleNamespace(runtime_dir=str(tmp_path / 'runtime'))

    with pytest.raises(HypervisorError, match='boot-mode: vagrant'):
        launch_vm(
            cfg, template, 2, 2048, job_id=77,
            pin_ssh_host_key=True)

    assert not (tmp_path / 'runtime').exists()


def test_launch_rejects_save_when_checkpoint_is_blocked(tmp_path):
    with pytest.raises(HypervisorError, match='checkpoint is disabled'):
        launch_vm(
            None, None, 2, 2048, save_path=str(tmp_path / 'checkpoint'),
            checkpoint_blocked_reason='sensitive guest staging')


def test_teardown_rejects_late_save_when_checkpoint_is_blocked(tmp_path):
    runtime = tmp_path / '42'
    runtime.mkdir()
    (runtime / 'vm.json').write_text(json.dumps({
        'job_id': 42,
        'checkpoint_blocked_reason': 'sensitive guest staging',
    }))
    destination = tmp_path / 'checkpoint'

    with pytest.raises(HypervisorError, match='checkpoint is disabled'):
        teardown_vm(42, runtime_dir=str(runtime), save_path=str(destination))

    assert not destination.exists()
