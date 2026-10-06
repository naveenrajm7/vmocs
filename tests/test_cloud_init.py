"""Tests for per-launch NoCloud data."""

import yaml

from vmocs.hypervisor import _make_cloud_init_iso


def test_host_identity_cloud_init_creates_matching_user(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        'vmocs.hypervisor.subprocess.check_call',
        lambda argv, **kwargs: calls.append(argv))

    iso = _make_cloud_init_iso(
        str(tmp_path),
        'ssh-ed25519 AAAATEST vmocs',
        host_user={'name': 'cluster-user', 'uid': 244235765,
                   'gid': 1274200513})

    cloud_config = yaml.safe_load(
        (tmp_path / 'user-data').read_text().removeprefix('#cloud-config\n'))
    script = cloud_config['write_files'][0]['content']
    assert 'username=cluster-user' in script
    assert 'uid=244235765' in script
    assert 'gid=1274200513' in script
    assert 'useradd --uid "$uid" --gid "$gid"' in script
    assert 'NOPASSWD:ALL' in script
    assert cloud_config['runcmd'] == [
        ['/usr/local/sbin/vmocs-create-host-user']]
    assert iso == str(tmp_path / 'cloud-init.iso')
    assert calls[0][calls[0].index('-volid') + 1] == 'cidata'
