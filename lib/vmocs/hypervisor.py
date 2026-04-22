#  Copyright (C) 2014-2015 CEA/DAM/DIF
#  Copyright (C) 2026 Naveenraj Muthuraj
#
#  Based on pcocc Hypervisor.py by CEA/DAM/DIF
#  Key extractions:
#    - Block device cmdline helpers (lines 2363-2467)
#    - Machine/CPU/memory/network/QMP/cloud-init args (lines 1328-1646)
#    - Mount point setup (lines 1897-1948)
#    - fork/exec + QMP connect + vCPU bind (lines 1664-1710)
#  Key changes:
#    - No etcd/Config singleton/batch/spice/docker/agent
#    - User-mode (SLIRP) networking replaces TAP
#    - Accepts plain Python dicts instead of vm objects
#  SPDX-License-Identifier: GPL-3.0-or-later

import atexit
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import yaml

from .error import HypervisorError, ImageError
from .image import VMImage
from .monitor import wait_for_monitor

# ---------------------------------------------------------------------------
# Block device cmdline helpers — adapted from pcocc Hypervisor.py:2363-2467
# ---------------------------------------------------------------------------

def _block_cache_opt(cache):
    """Map cache mode to blockdev cache flags."""
    modes = {
        'writeback':   'cache.direct=off,cache.no-flush=off',
        'none':        'cache.direct=on,cache.no-flush=off',
        'unsafe':      'cache.direct=off,cache.no-flush=on',
        'writethrough':'cache.direct=off,cache.no-flush=off',
        'directsync':  'cache.direct=on,cache.no-flush=off',
    }
    if cache not in modes:
        raise HypervisorError(f'unsupported cache mode: {cache}')
    return modes[cache]


def _dev_cache_opt(cache):
    """Map cache mode to device write-cache flag."""
    on_modes = {'writeback', 'none', 'unsafe'}
    off_modes = {'writethrough', 'directsync'}
    if cache in on_modes:
        return 'write-cache=on'
    if cache in off_modes:
        return 'write-cache=off'
    raise HypervisorError(f'unsupported cache mode: {cache}')


def _drive_cmdline(path, name, cache):
    """Common blockdev backend args (qcow2 or raw)."""
    fmt = VMImage.image_format(path)
    return ['-blockdev',
            f'driver={fmt},node-name={name},{_block_cache_opt(cache)},'
            f'file.driver=file,file.filename={path},discard=unmap']


def _iothread(name):
    return ['-object', f'iothread,id=ioth-{name}']


def _virtio_blk_cmdline(path, name, index, cache):
    """virtio-blk frontend + blockdev backend. pcocc: qemu_gen_vblk_cmdline."""
    dev_addr = 6 if index == 0 else (index - 1) // 3 + 7
    func = 0 if index == 0 else (index - 1) % 3
    cmd = _iothread(name)
    cmd += ['-device',
            f'virtio-blk-pci,id=vblk-{name},multifunction=on,'
            f'drive={name},addr={dev_addr:02d}.{func},{_dev_cache_opt(cache)}']
    return cmd + _drive_cmdline(path, name, cache)


def _scsi_cmdline(path, name, index, cache):
    """virtio-scsi frontend + blockdev backend. pcocc: qemu_gen_scsi_cmdline."""
    cmd = _iothread(name)
    cmd += ['-device',
            f'scsi-hd,id=scsi-hd-{name},bus=scsi0.0,'
            f'scsi-id={index},drive={name},{_dev_cache_opt(cache)}']
    return cmd + _drive_cmdline(path, name, cache)


def _nvme_cmdline(path, name, index, cache, serial=None):
    """NVMe frontend + blockdev backend."""
    serial = serial or f'VMOCS-NVME-{index}'
    cmd = ['-device', f'nvme,drive={name},serial={serial}']
    return cmd + _drive_cmdline(path, name, cache)


def _ide_cmdline(path, name, index, cache):
    """IDE frontend + blockdev backend. pcocc: qemu_gen_ide_cmdline."""
    cmd = []
    if index == 0:
        cmd += ['-device', 'ich9-ahci,id=ahci,addr=06.0']
    cmd += ['-device',
            f'ide-hd,drive={name},bus=ahci.{index},{_dev_cache_opt(cache)}']
    return cmd + _drive_cmdline(path, name, cache)


def block_cmdline(model, path, name, index, cache, serial=None):
    """Route to the correct block device generator. pcocc: qemu_gen_block_cmdline."""
    dispatch = {
        'virtio':       _virtio_blk_cmdline,
        'virtio-scsi':  _scsi_cmdline,
        'ide':          _ide_cmdline,
    }
    if model == 'nvme':
        return _nvme_cmdline(path, name, index, cache, serial)
    if model not in dispatch:
        raise HypervisorError(f'unknown disk model: {model}')
    return dispatch[model](path, name, index, cache)


# ---------------------------------------------------------------------------
# TPM 2.0 via swtpm — started as a sidecar before QEMU
# ---------------------------------------------------------------------------

_SWTPM = '/usr/bin/swtpm'


def _tpm_cmdline(runtime_dir):
    """Start swtpm as an orphaned daemon (double-fork) so it survives the
    vmocs process exiting in --detach mode, then return QEMU tpm args."""
    tpm_dir = os.path.join(runtime_dir, 'tpm')
    tpm_sock = os.path.join(runtime_dir, 'tpm.sock')
    os.makedirs(tpm_dir)
    # start_new_session=True puts swtpm in its own session so it survives the
    # vmocs process exiting in --detach mode (no atexit kill, no SIGHUP).
    subprocess.Popen(
        [_SWTPM, 'socket',
         '--tpmstate', f'dir={tpm_dir}',
         '--ctrl', f'type=unixio,path={tpm_sock}',
         '--tpm2'],
        close_fds=True, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not os.path.exists(tpm_sock):
        if time.monotonic() > deadline:
            raise HypervisorError('swtpm failed to start (socket timeout)')
        time.sleep(0.1)
    return [
        '-chardev', f'socket,id=chrtpm,path={tpm_sock}',
        '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm',
        '-device', 'tpm-tis,tpmdev=tpm0',
    ]


# ---------------------------------------------------------------------------
# Cloud-init ISO — adapted from pcocc Hypervisor.py:1579-1646
# ---------------------------------------------------------------------------

def _make_cloud_init_iso(runtime_dir, ssh_pubkey, hostname='vmocs', ssh_user='root'):
    """Write cloud-init user-data + meta-data, then call genisoimage."""
    if ssh_user == 'root':
        user_data = {
            'disable_root': False,
            'ssh_pwauth': False,
            'users': [{'name': 'root', 'ssh_authorized_keys': [ssh_pubkey]}],
        }
    else:
        # Standard cloud images (Ubuntu) use a non-root default user.
        # Inject the key for that user and keep root locked.
        user_data = {
            'ssh_pwauth': False,
            'users': [{
                'name': ssh_user,
                'sudo': 'ALL=(ALL) NOPASSWD:ALL',
                'ssh_authorized_keys': [ssh_pubkey],
            }],
        }
    ud_path = os.path.join(runtime_dir, 'user-data')
    md_path = os.path.join(runtime_dir, 'meta-data')
    iso_path = os.path.join(runtime_dir, 'cloud-init.iso')

    with open(ud_path, 'w') as f:
        f.write('#cloud-config\n')
        yaml.safe_dump(user_data, f)

    with open(md_path, 'w') as f:
        f.write(f'instance-id: {uuid.uuid4()}\n')
        f.write(f'local-hostname: {hostname}\n')

    try:
        subprocess.check_call(
            ['genisoimage', '-output', iso_path,
             '-volid', 'cidata', '-joliet', '-rock',
             ud_path, md_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HypervisorError(f'genisoimage failed: {e}')

    return iso_path


# ---------------------------------------------------------------------------
# Mount points — adapted from pcocc Hypervisor.py:1897-1948
# ---------------------------------------------------------------------------

_VIRTIOFSD_CANDIDATES = [
    '/usr/libexec/virtiofsd',
    '/usr/lib/qemu/virtiofsd',
]

def _find_virtiofsd():
    """Return path to virtiofsd binary, checking known locations then PATH."""
    for path in _VIRTIOFSD_CANDIDATES:
        if os.path.isfile(path):
            return path
    found = shutil.which('virtiofsd')
    if found:
        return found
    raise HypervisorError(
        f'virtiofsd not found; checked {_VIRTIOFSD_CANDIDATES}')


def _has_virtiofs(mount_points):
    """Return True if any mount point uses virtio-fs."""
    for opts in mount_points.values():
        if isinstance(opts, str):
            opts = {'path': opts}
        if opts.get('type') == 'virtio-fs':
            return True
    return False


def _mount_cmdline(mount_points, runtime_dir):
    """Build 9p/virtiofs args for each mount point."""
    cmd = []

    for i, (tag, opts) in enumerate(mount_points.items()):
        if isinstance(opts, str):
            opts = {'path': opts}
        host_path = opts['path']
        mount_type = opts.get('type', 'virtio-9p')
        readonly = opts.get('readonly', False)
        ro_str = ',readonly' if readonly else ''

        if not os.path.isdir(host_path):
            raise HypervisorError(f'mount point not found: {host_path}')

        if mount_type == 'virtio-9p':
            cmd += ['-fsdev',
                    f'local,id={tag},path={host_path},security_model=none{ro_str}']
            cmd += ['-device', f'virtio-9p-pci,fsdev={tag},mount_tag={tag}']
        elif mount_type == 'virtio-fs':
            if readonly:
                raise HypervisorError('read-only mounts not supported with virtio-fs')
            sock = os.path.join(runtime_dir, f'virtiofs_{i}.sock')
            p = subprocess.Popen(
                [_find_virtiofsd(), '--rlimit-nofile', '0',
                 '--socket-path', sock, '--sandbox', 'namespace',
                 '--shared-dir', host_path],
                close_fds=True)
            atexit.register(_try_kill, p)
            cmd += ['-chardev', f'socket,id=char_fs_{i},path={sock}']
            cmd += ['-device',
                    f'vhost-user-fs-pci,queue-size=1024,chardev=char_fs_{i},tag={tag}']
        else:
            raise HypervisorError(f'unknown mount type: {mount_type}')

    return cmd


def _try_kill(proc):
    try:
        proc.kill()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Free SSH port allocation for user-mode networking
# ---------------------------------------------------------------------------

def _find_free_port(port_range):
    lo, hi = port_range
    for port in range(lo, hi):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('', port))
                return port
            except OSError:
                continue
    raise HypervisorError(f'no free port in range {lo}-{hi}')


# ---------------------------------------------------------------------------
# Main QEMU cmdline builder
# ---------------------------------------------------------------------------

def build_qemu_cmdline(qemu_bin, template, cores, memory_mb,
                       disk_path, runtime_dir,
                       ssh_port, qmp_socket,
                       cloud_init_iso=None,
                       snapshot_mem=None,
                       firmware_vars=None,
                       pci_devices=()):
    """Build the full QEMU command line list.

    Args:
        qemu_bin:       path to qemu-system-x86_64
        template:       resolved Template object
        cores:          number of vCPUs
        memory_mb:      RAM in MB
        disk_path:      path to COW overlay (primary disk)
        runtime_dir:    per-job runtime directory
        ssh_port:       host port to forward to guest :22 (user-mode net)
        qmp_socket:     path for QMP Unix socket
        cloud_init_iso: path to cloud-init ISO, or None for vagrant boot mode
        snapshot_mem:   path to lzop-compressed memory snapshot, or None

    Adapted from pcocc Hypervisor.py:1328-1646.
    """
    cmd = [qemu_bin]

    # Machine type + KVM acceleration
    machine = template.machine_type
    smm_suffix = ',smm=on' if template.smm else ''
    cpu_flags = ',hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,kvm=off' if template.hyperv else ''
    try:
        open('/dev/kvm', 'r+').close()
        cmd += ['-machine', f'type={machine},accel=kvm{smm_suffix}',
                '-cpu', f'host{cpu_flags}']
    except OSError:
        logging.warning('KVM not available, running without acceleration')
        cmd += ['-machine', f'type={machine}{smm_suffix}']

    # UEFI firmware pflash pair (code read-only, vars is per-job writable copy)
    if template.firmware:
        if not firmware_vars:
            raise HypervisorError(
                f"template '{template.name}' sets 'firmware' but firmware_vars was not provided")
        cmd += ['-drive', f'if=pflash,format=raw,readonly=on,file={template.firmware}']
        cmd += ['-drive', f'if=pflash,format=raw,file={firmware_vars}']

    # TPM 2.0 sidecar (must start before QEMU opens the socket)
    if template.tpm:
        cmd += _tpm_cmdline(runtime_dir)

    # Snapshot restore: incoming migration (pcocc:1337-1341)
    if snapshot_mem:
        cmd += ['-incoming', f'exec: lzop -dc {snapshot_mem}']

    # Start paused — caller will send cont() after QMP handshake
    cmd += ['-S']

    # Clock: Windows expects localtime; Linux uses UTC
    if template.clock_offset == 'localtime':
        cmd += ['-rtc', 'base=localtime,clock=host,driftfix=slew']
    else:
        cmd += ['-rtc', 'base=utc']

    # Display
    if template.display == 'vnc':
        vnc_port = template.vnc_port or (5910 + (os.getpid() % 100))
        display_num = vnc_port - 5900
        logging.info('VNC on port %d (display :%d)', vnc_port, display_num)
        cmd += ['-display', f'vnc=0.0.0.0:{display_num}']
        cmd += ['-device', 'virtio-vga']
    else:
        cmd += ['-display', 'none']

    # SCSI controller (needed for cdrom and optional scsi disks)
    cmd += ['-device', 'virtio-scsi-pci,id=scsi0']

    # Primary disk (COW overlay)
    model = template.disk_model
    cache = template.disk_cache
    cmd += block_cmdline(model, disk_path, 'drive0', 0, cache)

    # Cloud-init ISO injected as SCSI cdrom (cloud-init boot mode only)
    if cloud_init_iso:
        cmd += ['-drive', f'id=cdrom0,if=none,format=raw,readonly=on,file={cloud_init_iso}']
        cmd += ['-device', 'scsi-cd,bus=scsi0.0,drive=cdrom0']

    # Direct kernel boot (optional); UEFI owns its boot order via NVRAM
    if template.kernel:
        cmd += ['-kernel', template.kernel]
        if '-append' not in (template.custom_args or []):
            cmd += ['-append', 'console=ttyS0 root=/dev/vda1']
    elif not template.firmware:
        cmd += ['-boot', 'order=cd']

    # Memory — virtiofs (vhost-user) requires shared memory backing (pcocc:1483-1486)
    mount_points = template.mount_points or {}
    if _has_virtiofs(mount_points):
        cmd += ['-m', str(memory_mb)]
        cmd += ['-object',
                f'memory-backend-file,id=mem,size={memory_mb}M,'
                f'mem-path=/dev/shm,share=on']
        cmd += ['-numa', 'node,memdev=mem']
    else:
        cmd += ['-m', str(memory_mb)]

    # CPU topology
    cmd += ['-smp', f'threads=1,cores=1,sockets={cores}']

    # User-mode networking with SSH port forward
    cmd += ['-netdev',
            f'user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22']
    cmd += ['-device', 'virtio-net-pci,netdev=net0']

    # Mount points
    if mount_points:
        cmd += _mount_cmdline(mount_points, runtime_dir)

    # QMP socket
    cmd += ['-qmp', f'unix:{qmp_socket},server=on,wait=off']

    # Serial console (for debugging)
    console_sock = os.path.join(runtime_dir, 'console.sock')
    cmd += ['-chardev',
            f'socket,id=charserial0,path={console_sock},server=on,wait=off']
    cmd += ['-device', 'isa-serial,chardev=charserial0,id=serial0']

    # Virtio RNG
    cmd += ['-object', 'rng-random,filename=/dev/urandom,id=rng0']
    cmd += ['-device', 'virtio-rng-pci,rng=rng0']

    # PCI passthrough — attach each VFIO device, optionally via its own PCIe
    # root port. pci-root-port=true is needed for AMD GPUs (and any device
    # sensitive to PCIe topology); false (default) attaches directly like pcocc
    # does for IB and generic PCI devices.
    # Chassis/slot numbering starts at 6/0x15 to avoid Q35's internal ports.
    use_root_port = template.pci_root_port
    for i, bdf in enumerate(pci_devices):
        if use_root_port:
            chassis = 6 + i
            slot = 0x15 + i
            port_id = f'pcie.{chassis}'
            cmd += ['-device',
                    f'pcie-root-port,id={port_id},bus=pcie.0,chassis={chassis},slot={slot:#x}']
            cmd += ['-device', f'vfio-pci,host={bdf},bus={port_id}']
        else:
            cmd += ['-device', f'vfio-pci,host={bdf}']

    # Custom args from template
    if template.custom_args:
        cmd += template.custom_args

    return cmd
