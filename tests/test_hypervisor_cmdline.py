"""Unit tests for QEMU cmdline builder."""

import os
import pytest

from vmocs.hypervisor import build_qemu_cmdline, block_cmdline, _has_virtiofs
from vmocs.error import HypervisorError
from vmocs.sidecars import plan_sidecars


class FakeConfig:
    sidecars = {
        'swtpm': {'binary': '/usr/bin/swtpm'},
        'virtiofsd': {'binary': '/usr/libexec/virtiofsd'},
        'rocm-ernic': {'binary': '/opt/rocm-ernic/bin/rocm-ernic'},
        'rocjitsu': {
            'binary': '/opt/rocjitsu/bin/rocjitsu',
            'profiles': {'mi455x': '/opt/rocjitsu/mi455x.json'},
        },
    }


class FakeTemplate:
    name = 'fake'
    machine_type = 'q35'
    disk_model = 'virtio'
    disk_cache = 'unsafe'
    kernel = None
    custom_args = []
    mount_points = {}
    extra_hostfwd = []
    extra_disks = []
    pci_root_port = False
    pci_roms = {}
    firmware = None
    firmware_vars_template = None
    display = 'none'
    vnc_port = None
    clock_offset = 'utc'
    hyperv = False
    smm = False
    tpm = False
    cpu_model = None
    emulated_devices = []


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


@pytest.fixture(autouse=True)
def _stub_image_format(monkeypatch):
    monkeypatch.setattr('vmocs.hypervisor.VMImage.image_format', staticmethod(lambda p: 'qcow2'))


@pytest.fixture
def base_img(tmp_path):
    p = tmp_path / 'base.qcow2'
    p.touch()
    return str(p)


@pytest.fixture
def cow_img(tmp_path, base_img):
    p = tmp_path / 'cow.qcow2'
    p.touch()
    return str(p)


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


def test_restricted_network_preserves_ssh_and_explicit_guest_forward(
        tmp_path, cow_img):
    runtime = str(tmp_path / 'restricted-network')
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
        network={
            'mode': 'user',
            'restrict': True,
            'ipv6': False,
            'guestfwd': [
                'tcp:10.0.2.100:443-cmd:/usr/bin/nc api.example.com 443',
            ],
        },
    )
    netdev = cmd[cmd.index('-netdev') + 1]
    assert 'restrict=on' in netdev
    assert 'ipv6=off' in netdev
    assert 'hostfwd=tcp:127.0.0.1:60222-:22' in netdev
    assert ('guestfwd=tcp:10.0.2.100:443-'
            'cmd:/usr/bin/nc api.example.com 443') in netdev


def test_network_options_and_host_forward_are_passed_to_qemu(tmp_path, cow_img):
    runtime = str(tmp_path / 'network-options')
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
        network={
            'options': ['net=10.55.0.0/24', 'dns=10.55.0.3'],
            'hostfwd': ['tcp:127.0.0.1:8080-:80'],
        },
    )
    netdev = cmd[cmd.index('-netdev') + 1]
    assert 'net=10.55.0.0/24' in netdev
    assert 'dns=10.55.0.3' in netdev
    assert 'hostfwd=tcp:127.0.0.1:8080-:80' in netdev


def test_passt_network_preserves_ssh_and_explicit_forwards(tmp_path, cow_img):
    runtime = str(tmp_path / 'passt-network')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/opt/qemu-vfio/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        network={
            'mode': 'passt',
            'ipv6': False,
            'bind': '0.0.0.0',
            'tcp-ports': ['8080:80'],
            'udp-ports': ['5353:53'],
            'options': ['mtu=1500'],
        },
    )
    netdev = cmd[cmd.index('-netdev') + 1]
    assert netdev.startswith('passt,id=net0,')
    assert 'ipv6=off' in netdev
    assert 'mtu=1500' in netdev
    assert 'tcp-ports=0.0.0.0/60222:22,tcp-ports=8080:80' in netdev
    assert 'udp-ports=0.0.0.0/5353:53' in netdev


def test_passt_defaults_to_loopback_ssh_and_no_udp(tmp_path, cow_img):
    runtime = str(tmp_path / 'passt-defaults')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/opt/qemu-vfio/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        network={'mode': 'passt'},
    )
    netdev = cmd[cmd.index('-netdev') + 1]
    assert 'tcp-ports=127.0.0.1/60222:22' in netdev
    assert 'udp-ports=none' in netdev


def test_legacy_extra_host_forward_remains_supported(tmp_path, cow_img):
    class TemplateWithLegacyForward(FakeTemplate):
        extra_hostfwd = ['tcp:127.0.0.1:3389-:3389']

    runtime = str(tmp_path / 'legacy-hostfwd')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=TemplateWithLegacyForward(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
    )

    netdev = cmd[cmd.index('-netdev') + 1]
    assert 'hostfwd=tcp:127.0.0.1:3389-:3389' in netdev


@pytest.mark.parametrize('network,match', [
    ('restricted', "template 'network' must be a mapping"),
    ({'mode': 'bridge'}, 'unsupported network mode'),
    ({'restrct': True}, 'unknown network setting'),
    ({'restrict': 'yes'}, 'network.restrict must be true or false'),
    ({'options': 'ipv6=off'}, 'network.options must be a list of strings'),
    ({'restrict': True, 'options': ['restrict=off']},
     'network.options cannot override'),
    ({'guestfwd': 'tcp:10.0.2.100:443-tcp:example.com:443'},
     'network.guestfwd must be a list of strings'),
    ({'guestfwd': ['tcp:10.0.2.100:443-udp:example.com:443']},
     'network.guestfwd entries must forward to tcp or cmd'),
    ({'hostfwd': ['tcp:127.0.0.1:8080-:80,restrict=off']},
     'network.hostfwd entries cannot contain commas'),
    ({'mode': 'passt', 'restrict': True},
     "network mode 'passt' does not support setting"),
    ({'mode': 'passt', 'bind': '127.0.0.1/8'},
     'network.bind must be a non-empty address'),
    ({'mode': 'passt', 'tcp-ports': ['8080:80,8443:443']},
     'network.tcp-ports entries cannot contain commas'),
    ({'mode': 'passt', 'options': ['tcp-ports=all']},
     'network.options cannot override'),
])
def test_invalid_network_configuration_fails_early(
        tmp_path, cow_img, network, match):
    runtime = str(tmp_path / ('invalid-' + str(abs(hash(match)))))
    os.makedirs(runtime)
    with pytest.raises(HypervisorError, match=match):
        build_qemu_cmdline(
            qemu_bin='/usr/bin/qemu-system-x86_64',
            template=FakeTemplate(),
            cores=2,
            memory_mb=1024,
            disk_path=cow_img,
            runtime_dir=runtime,
            ssh_port=60222,
            qmp_socket=os.path.join(runtime, 'qmp.sock'),
            network=network,
        )


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


def test_supervised_session_keeps_qemu_alive_for_qmp_lifecycle(tmp_path, cow_img):
    runtime = str(tmp_path / 'supervised')
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
        supervised=True,
    )
    assert cmd.count('-no-shutdown') == 1


def test_supervised_session_does_not_duplicate_template_flag(tmp_path, cow_img):
    class TemplateWithNoShutdown(FakeTemplate):
        custom_args = ['-no-shutdown']

    runtime = str(tmp_path / 'supervised-existing')
    os.makedirs(runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=TemplateWithNoShutdown(),
        cores=2,
        memory_mb=1024,
        disk_path=cow_img,
        runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        supervised=True,
    )
    assert cmd.count('-no-shutdown') == 1


# ---------------------------------------------------------------------------
# Mount point tests
# ---------------------------------------------------------------------------

class FakeTemplateWith9p(FakeTemplate):
    mount_points = {'home': {'path': '/tmp', 'type': 'virtio-9p'}}


class FakeTemplateWithVirtioFs(FakeTemplate):
    mount_points = {'home': {'path': '/tmp', 'type': 'virtio-fs'}}


class FakeTemplateWithVfioUser(FakeTemplateWithVirtioFs):
    cpu_model = 'EPYC'
    emulated_devices = [
        {'type': 'rocm-ernic'},
        {'type': 'rocjitsu', 'profile': 'mi455x'},
    ]


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


def test_virtiofs_uses_shared_memory_backend(tmp_path, cow_img):
    """virtio-fs mounts: one machine-bound shared memory backend."""
    runtime = str(tmp_path / 'rt')
    os.makedirs(runtime)
    template = FakeTemplateWithVirtioFs()
    plans = plan_sidecars(FakeConfig(), template, runtime)
    cmd = build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=template,
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        sidecar_plans=plans,
    )
    assert '-m' in cmd
    assert any('memory-backend-memfd' in a and '1024M' in a for a in cmd)
    assert '-numa' not in cmd
    assert cmd.count('-machine') == 1
    assert 'memory-backend=vmocs.ram' in cmd[cmd.index('-machine') + 1]
    assert any('vhost-user-fs-pci' in a for a in cmd)


def test_vfio_user_and_virtiofs_compose_one_machine_backend(tmp_path, cow_img):
    runtime = str(tmp_path / 'rt-vfio-user')
    os.makedirs(runtime)
    template = FakeTemplateWithVfioUser()
    plans = plan_sidecars(FakeConfig(), template, runtime)

    cmd = build_qemu_cmdline(
        qemu_bin='/opt/qemu-vfio/bin/qemu-system-x86_64',
        template=template,
        cores=4, memory_mb=8192,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        sidecar_plans=plans,
    )
    flat = ' '.join(cmd)

    assert cmd.count('-machine') == 1
    assert sum('memory-backend-memfd' in arg for arg in cmd) == 1
    assert 'memory-backend=vmocs.ram' in cmd[cmd.index('-machine') + 1]
    assert 'share=on' in flat
    assert '-numa' not in cmd
    assert cmd[cmd.index('-cpu') + 1] == 'EPYC'
    assert flat.count('vfio-user-pci') == 2
    assert 'vhost-user-fs-pci' in flat
    assert 'vfio-pci,host=' not in flat
    assert flat.count('"rombar":0') == 2


# ---------------------------------------------------------------------------
# UEFI / Windows feature tests
# ---------------------------------------------------------------------------


def _uefi_cmd(tmp_path, cow_img, monkeypatch):
    """Helper: build cmdline for FakeTemplateUEFI with a pure swtpm plan."""
    runtime = str(tmp_path / 'runtime')
    os.makedirs(runtime, exist_ok=True)
    nvram = str(tmp_path / 'nvram.fd')
    open(nvram, 'w').close()
    template = FakeTemplateUEFI()
    plans = plan_sidecars(FakeConfig(), template, runtime)

    return build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=template,
        cores=4, memory_mb=8192,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        firmware_vars=nvram,
        sidecar_plans=plans,
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


# ---------------------------------------------------------------------------
# Extra disks tests
# ---------------------------------------------------------------------------

def _build_with_extra_disks(tmp_path, cow_img, extra_disks):
    runtime = str(tmp_path / 'rt-extra')
    os.makedirs(runtime, exist_ok=True)
    return build_qemu_cmdline(
        qemu_bin='/usr/bin/qemu-system-x86_64',
        template=FakeTemplate(),
        cores=2, memory_mb=1024,
        disk_path=cow_img, runtime_dir=runtime,
        ssh_port=60222,
        qmp_socket=os.path.join(runtime, 'qmp.sock'),
        extra_disks=extra_disks,
    )


def test_extra_disk_nvme(tmp_path, cow_img):
    extra = [{'file': '/data/scratch.qcow2', 'device': 'nvme'}]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'node-name=drive1' in flat
    assert 'nvme,drive=drive1' in flat


def test_extra_disk_virtio(tmp_path, cow_img):
    extra = [{'file': '/data/scratch.qcow2', 'device': 'virtio'}]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'node-name=drive1' in flat
    assert 'virtio-blk-pci' in flat
    assert 'drive=drive1' in flat


def test_extra_disk_multiple(tmp_path, cow_img):
    extra = [
        {'file': '/data/d1.qcow2', 'device': 'nvme'},
        {'file': '/data/d2.qcow2', 'device': 'virtio'},
    ]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'node-name=drive1' in flat
    assert 'node-name=drive2' in flat


def test_extra_disk_cache_override(tmp_path, cow_img):
    extra = [{'file': '/data/scratch.qcow2', 'device': 'nvme', 'cache': 'none'}]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'cache.direct=on' in flat


def test_extra_disk_serial(tmp_path, cow_img):
    extra = [{'file': '/data/scratch.qcow2', 'device': 'nvme', 'serial': 'MY-SN'}]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'serial=MY-SN' in flat


def test_extra_disk_defaults_to_template_model(tmp_path, cow_img):
    """Extra disk with no 'device' inherits the template's disk-model (virtio)."""
    extra = [{'file': '/data/scratch.qcow2'}]
    cmd = _build_with_extra_disks(tmp_path, cow_img, extra)
    flat = ' '.join(cmd)
    assert 'virtio-blk-pci' in flat
    assert 'node-name=drive1' in flat
