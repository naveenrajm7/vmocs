"""Unit tests for QEMU cmdline builder."""

import subprocess
import os
import pytest

from vmocs.hypervisor import build_qemu_cmdline, block_cmdline, _has_virtiofs
from vmocs.error import HypervisorError


class FakeTemplate:
    name = 'fake'
    machine_type = 'q35'
    disk_model = 'virtio'
    disk_cache = 'unsafe'
    kernel = None
    custom_args = []
    mount_points = {}
    firmware = None
    firmware_vars_template = None
    display = 'none'
    vnc_port = None
    clock_offset = 'utc'
    hyperv = False
    smm = False
    tpm = False


class FakeTemplateUEFI(FakeTemplate):
    name = 'fake-uefi'
    firmware = '/usr/share/OVMF/OVMF_CODE_4M.ms.fd'
    firmware_vars_template = '/usr/share/OVMF/OVMF_VARS_4M.ms.fd'
    display = 'vnc'
    vnc_port = 5910
    clock_offset = 'localtime'
    hyperv = True
    smm = True
    tpm = True


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
    assert '-m' in cmd
    assert any('memory-backend-file' in a and '1024M' in a and '/dev/shm' in a
               for a in cmd)
    assert '-numa' in cmd
    assert any('vhost-user-fs-pci' in a for a in cmd)


# ---------------------------------------------------------------------------
# UEFI / Windows feature tests
# ---------------------------------------------------------------------------

def _uefi_cmd(tmp_path, cow_img, monkeypatch):
    """Helper: build cmdline for FakeTemplateUEFI with swtpm and image_format stubbed."""
    from unittest.mock import MagicMock
    fake_proc = MagicMock()
    monkeypatch.setattr('vmocs.hypervisor.subprocess.Popen', lambda *a, **kw: fake_proc)
    monkeypatch.setattr('vmocs.hypervisor.VMImage.image_format', staticmethod(lambda p: 'qcow2'))
    # Make swtpm socket appear immediately without polling
    monkeypatch.setattr('vmocs.hypervisor.time.monotonic', lambda: 0)
    monkeypatch.setattr('vmocs.hypervisor.os.path.exists', lambda p: True)

    runtime = str(tmp_path / 'runtime')
    os.makedirs(runtime, exist_ok=True)
    nvram = str(tmp_path / 'nvram.fd')
    open(nvram, 'w').close()

    return build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplateUEFI(),
        cores=4, memory_mb=8192,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        firmware_vars=nvram,
    )


def test_uefi_pflash_args(tmp_path, cow_img, monkeypatch):
    """UEFI: CODE drive is readonly=on, VARS drive is writable."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    flat = ' '.join(cmd)
    assert 'OVMF_CODE_4M.ms.fd' in flat
    assert 'readonly=on' in flat
    assert 'nvram.fd' in flat
    # VARS entry must NOT have readonly
    nvram_idx = next(i for i, a in enumerate(cmd) if 'nvram.fd' in a)
    assert 'readonly' not in cmd[nvram_idx]


def test_no_boot_flag_with_firmware(tmp_path, cow_img, monkeypatch):
    """-boot order=cd must be absent when UEFI firmware is set."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    assert '-boot' not in cmd


def test_smm_on_in_machine_line(tmp_path, cow_img, monkeypatch):
    """smm=True → smm=on appears in the -machine argument."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    machine_val = cmd[cmd.index('-machine') + 1]
    assert 'smm=on' in machine_val


def test_hyperv_cpu_flags(tmp_path, cow_img, monkeypatch):
    """hyperv=True → Hyper-V flags + kvm=off in -cpu; absent for plain template."""
    cmd_uefi = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    cpu_val = cmd_uefi[cmd_uefi.index('-cpu') + 1]
    for flag in ('hv_relaxed', 'hv_vapic', 'hv_spinlocks', 'kvm=off'):
        assert flag in cpu_val, f'missing {flag}'

    # Plain template must not have any of these
    runtime2 = str(tmp_path / 'runtime2')
    os.makedirs(runtime2, exist_ok=True)
    cmd_plain = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime2,
        ssh_port=60223,
        qmp_socket=os.path.join(runtime2, 'qmp.sock'),
    )
    plain_cpu_val = cmd_plain[cmd_plain.index('-cpu') + 1]
    assert 'kvm=off' not in plain_cpu_val
    assert 'hv_relaxed' not in plain_cpu_val


def test_localtime_clock(tmp_path, cow_img, monkeypatch):
    """clock-offset=localtime → localtime + driftfix=slew in -rtc arg."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    rtc_val = cmd[cmd.index('-rtc') + 1]
    assert 'localtime' in rtc_val
    assert 'driftfix=slew' in rtc_val


def test_vnc_display(tmp_path, cow_img, monkeypatch):
    """display=vnc → VNC address in -display arg and virtio-vga device present."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    display_val = cmd[cmd.index('-display') + 1]
    assert display_val.startswith('vnc=')
    assert ':10' in display_val   # port 5910 → display :10
    assert any('virtio-vga' in a for a in cmd)


def test_tpm_args_present(tmp_path, cow_img, monkeypatch):
    """tpm=True → tpmdev emulator and tpm-tis device appear in cmdline."""
    cmd = _uefi_cmd(tmp_path, cow_img, monkeypatch)
    flat = ' '.join(cmd)
    assert 'tpmdev' in flat
    assert 'emulator' in flat
    assert 'tpm-tis' in flat


def test_firmware_vars_required_when_firmware_set(tmp_path, cow_img):
    """firmware set but firmware_vars omitted → HypervisorError."""
    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)

    class FakeUEFINoVars(FakeTemplateUEFI):
        tpm = False  # skip swtpm for this test

    with pytest.raises(HypervisorError, match='firmware_vars'):
        build_qemu_cmdline(
            qemu_bin='/usr/bin/qemu-system-x86_64',
            template=FakeUEFINoVars(),
            cores=2, memory_mb=1024,
            disk_path=cow_img, runtime_dir=runtime,
            ssh_port=60222,
            qmp_socket=os.path.join(runtime, 'qmp.sock'),
            firmware_vars=None,   # intentionally missing
        )
