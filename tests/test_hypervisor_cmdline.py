"""Unit tests for QEMU cmdline builder."""

import subprocess
import os
import pytest

from vmocs.hypervisor import build_qemu_cmdline, block_cmdline, _has_virtiofs
from vmocs.error import HypervisorError


class FakeTemplate:
    machine_type = 'q35'
    disk_model = 'virtio'
    disk_cache = 'unsafe'
    kernel = None
    custom_args = []
    mount_points = {}


@pytest.fixture
def base_img(tmp_path):
    p = str(tmp_path / 'base.qcow2')
    subprocess.check_call(['qemu-img', 'create', '-f', 'qcow2', p, '64M'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


@pytest.fixture
def cow_img(tmp_path, base_img):
    p = str(tmp_path / 'cow.qcow2')
    subprocess.check_call(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', base_img, p],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


def test_block_cmdline_virtio(cow_img):
    args = block_cmdline('virtio', cow_img, 'drive0', 0, 'unsafe')
    assert any('virtio-blk-pci' in a for a in args)
    assert any('drive0' in a for a in args)
    assert any('node-name=drive0' in a for a in args)


def test_block_cmdline_unknown_model(cow_img):
    with pytest.raises(HypervisorError):
        block_cmdline('bogus', cow_img, 'drive0', 0, 'unsafe')


def test_build_qemu_cmdline_structure(tmp_path, cow_img):
    runtime = str(tmp_path / 'runtime')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
    )
    assert cmd[0] == '/usr/bin/qemu-system-x86_64'
    assert '-m' in cmd
    assert '1024' in cmd
    assert '-smp' in cmd
    assert any('hostfwd=tcp:127.0.0.1:60222-:22' in a for a in cmd)
    assert any('unix:' in a and 'qmp.sock' in a for a in cmd)
    assert '-S' in cmd
    # vagrant mode (no cloud_init_iso): no cdrom should appear
    assert 'cdrom0' not in ' '.join(cmd)


def test_build_qemu_cmdline_with_cloud_init_iso(tmp_path, cow_img):
    runtime = str(tmp_path / 'runtime2')
    os.makedirs(runtime)
    fake_iso = str(tmp_path / 'cloud-init.iso')
    open(fake_iso, 'w').close()
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        cloud_init_iso=fake_iso,
    )
    assert 'cdrom0' in ' '.join(cmd)
    assert fake_iso in ' '.join(cmd)


# ---------------------------------------------------------------------------
# Mount point tests
# ---------------------------------------------------------------------------

class FakeTemplateWith9p(FakeTemplate):
    mount_points = {'home': {'path': '/tmp', 'type': 'virtio-9p'}}


class FakeTemplateWithVirtioFs(FakeTemplate):
    mount_points = {'home': {'path': '/tmp', 'type': 'virtio-fs'}}


def test_has_virtiofs_false():
    assert _has_virtiofs({}) is False
    assert _has_virtiofs({'home': {'path': '/tmp', 'type': 'virtio-9p'}}) is False
    assert _has_virtiofs({'home': '/tmp'}) is False


def test_has_virtiofs_true():
    assert _has_virtiofs({'home': {'path': '/tmp', 'type': 'virtio-fs'}}) is True


def test_9p_uses_plain_memory_and_fsdev(tmp_path, cow_img):
    """9p mounts: plain -m, -fsdev local args, no shared memory backend."""
    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplateWith9p(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
    )
    assert '-m' in cmd
    assert 'memory-backend-file' not in ' '.join(cmd)
    assert '-fsdev' in cmd
    assert any('local' in a and 'security_model=none' in a for a in cmd)
    assert any('virtio-9p-pci' in a for a in cmd)


def test_virtiofs_uses_shared_memory_backend(tmp_path, cow_img, monkeypatch):
    """virtio-fs mounts: shared memory backend replaces plain -m."""
    # Stub out virtiofsd finder, Popen (daemon), and image_format so the test
    # doesn't need virtiofsd installed or a real qcow2 image.
    monkeypatch.setattr('vmocs.hypervisor._find_virtiofsd',
                        lambda: '/usr/libexec/virtiofsd')
    from unittest.mock import MagicMock, patch
    fake_proc = MagicMock()
    monkeypatch.setattr('vmocs.hypervisor.subprocess.Popen', lambda *a, **kw: fake_proc)
    monkeypatch.setattr('vmocs.hypervisor.VMImage.image_format', staticmethod(lambda p: 'qcow2'))

    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplateWithVirtioFs(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
    )
    assert '-m' not in cmd
    assert any('memory-backend-file' in a and '1024M' in a and '/dev/shm' in a
               for a in cmd)
    assert '-numa' in cmd
    assert any('vhost-user-fs-pci' in a for a in cmd)
