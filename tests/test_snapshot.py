"""Unit tests for snapshot-related code paths."""

import os
import subprocess
import pytest

from vmocs.hypervisor import build_qemu_cmdline
from vmocs.error import HypervisorError


class FakeTemplate:
    machine_type = 'q35'
    disk_model = 'virtio'
    disk_cache = 'unsafe'
    kernel = None
    custom_args = []
    mount_points = {}


@pytest.fixture
def cow_img(tmp_path):
    base = str(tmp_path / 'base.qcow2')
    overlay = str(tmp_path / 'cow.qcow2')
    subprocess.check_call(['qemu-img', 'create', '-f', 'qcow2', base, '64M'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
                           '-b', base, overlay],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return overlay


def test_cmdline_no_snapshot(tmp_path, cow_img):
    """Without snapshot_mem, -incoming must not appear and -S must be present."""
    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_pubkey='ssh-ed25519 AAAA test',
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
    )
    assert '-incoming' not in cmd
    assert '-S' in cmd


def test_cmdline_with_snapshot(tmp_path, cow_img):
    """With snapshot_mem, -incoming must appear and contain the memory path."""
    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)
    mem_path = '/tmp/ubuntu-snap/memory'
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_pubkey='ssh-ed25519 AAAA test',
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        snapshot_mem=mem_path,
    )
    assert '-incoming' in cmd
    incoming_arg = cmd[cmd.index('-incoming') + 1]
    assert mem_path in incoming_arg
    assert 'lzop' in incoming_arg
    # -S must still be present (QEMU waits paused for incoming migration)
    assert '-S' in cmd
