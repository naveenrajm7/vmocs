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

import logging
import os
import socket
import subprocess
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
# PCI sysfs helpers
# ---------------------------------------------------------------------------

def _pci_sysfs_hex(bdf, attr):
    """Read a hex sysfs attribute for a PCI device, return int or None."""
    try:
        with open(f'/sys/bus/pci/devices/{bdf}/{attr}') as f:
            return int(f.read().strip(), 16)
    except (IOError, ValueError):
        return None

def _pci_vendor_device(bdf):
    """Return 'vendor:device' string (e.g. '1002:1586') for a BDF, or None."""
    vendor = _pci_sysfs_hex(bdf, 'vendor')
    device = _pci_sysfs_hex(bdf, 'device')
    if vendor is None or device is None:
        return None
    return f'{vendor:04x}:{device:04x}'

# ---------------------------------------------------------------------------
# Mount points — adapted from pcocc Hypervisor.py:1897-1948
# ---------------------------------------------------------------------------

def _has_virtiofs(mount_points):
    """Return True if any mount point uses virtio-fs."""
    for opts in mount_points.values():
        if isinstance(opts, str):
            opts = {'path': opts}
        if opts.get('type') == 'virtio-fs':
            return True
    return False


def _mount_cmdline(mount_points):
    """Build in-QEMU 9p args; virtio-fs comes from sidecar plans."""
    cmd = []

    for tag, opts in mount_points.items():
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
            # A typed sidecar plan owns the daemon and contributes its QEMU
            # chardev/device arguments.
            continue
        else:
            raise HypervisorError(f'unknown mount type: {mount_type}')

    return cmd
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


def _validated_network_mapping(network):
    if network is None:
        network = {}
    if not isinstance(network, dict):
        raise HypervisorError("template 'network' must be a mapping")
    return network


def _network_string_list(network, name, allow_commas=False):
    values = network.get(name, [])
    if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values):
        raise HypervisorError(f'network.{name} must be a list of strings')
    if not allow_commas and any(',' in value for value in values):
        raise HypervisorError(f'network.{name} entries cannot contain commas')
    return values


def _network_bool_options(network, names):
    options = []
    for name in names:
        if name not in network:
            continue
        value = network[name]
        if not isinstance(value, bool):
            raise HypervisorError(f"network.{name} must be true or false")
        options.append(f'{name}={"on" if value else "off"}')
    return options


def _raw_network_options(network, managed_options):
    values = _network_string_list(network, 'options', allow_commas=True)
    for value in values:
        if any(part.startswith(managed_options) for part in value.split(',')):
            raise HypervisorError(
                'network.options cannot override ' +
                ', '.join(name.rstrip('=') for name in managed_options))
    return values


def _user_network_cmdline(network, ssh_port, extra_hostfwd=()):
    """Build the managed QEMU SLIRP backend and guest NIC arguments."""
    network = _validated_network_mapping(network)

    supported = {
        'mode', 'restrict', 'ipv4', 'ipv6', 'options', 'hostfwd', 'guestfwd',
    }
    unknown = sorted(set(network) - supported)
    if unknown:
        raise HypervisorError(
            'unknown network setting(s): ' + ', '.join(unknown))

    options = ['user', 'id=net0']
    options.extend(_network_bool_options(
        network, ('restrict', 'ipv4', 'ipv6')))
    managed_options = (
        'id=', 'restrict=', 'ipv4=', 'ipv6=', 'hostfwd=', 'guestfwd=',
    )
    options.extend(_raw_network_options(network, managed_options))

    # vmocs always needs a private management path into the guest. QEMU's
    # restrict=on explicitly preserves configured forwarding rules.
    options.append(f'hostfwd=tcp:127.0.0.1:{ssh_port}-:22')

    hostfwds = _network_string_list(network, 'hostfwd')
    guestfwds = _network_string_list(network, 'guestfwd')
    for name, values in (('hostfwd', hostfwds),
                         ('guestfwd', guestfwds),
                         ('extra-hostfwd', extra_hostfwd or [])):
        if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values):
            raise HypervisorError(f'network.{name} must be a list of strings')
        if any(',' in value for value in values):
            raise HypervisorError(f'network.{name} entries cannot contain commas')
        if name == 'guestfwd' and any(
                '-tcp:' not in value and '-cmd:' not in value
                for value in values):
            raise HypervisorError(
                'network.guestfwd entries must forward to tcp or cmd')
        key = 'hostfwd' if name == 'extra-hostfwd' else name
        options.extend(f'{key}={value}' for value in values)

    return [
        '-netdev', ','.join(options),
        '-device', 'virtio-net-pci,netdev=net0',
    ]


def _passt_network_cmdline(network, ssh_port, extra_hostfwd=()):
    """Build QEMU's native passt backend (available since QEMU 10.1)."""
    network = _validated_network_mapping(network)
    supported = {
        'mode', 'ipv4', 'ipv6', 'options', 'bind', 'tcp-ports', 'udp-ports',
    }
    unknown = sorted(set(network) - supported)
    if unknown:
        raise HypervisorError(
            "network mode 'passt' does not support setting(s): " +
            ', '.join(unknown))
    if extra_hostfwd:
        raise HypervisorError(
            "network mode 'passt' cannot use legacy extra-hostfwd; use "
            'network.tcp-ports')

    bind = network.get('bind', '127.0.0.1')
    if not isinstance(bind, str) or not bind or ',' in bind or '/' in bind:
        raise HypervisorError(
            'network.bind must be a non-empty address/interface without '
            "',' or '/'")

    tcp_ports = _network_string_list(network, 'tcp-ports')
    udp_ports = _network_string_list(network, 'udp-ports')
    options = ['passt', 'id=net0']
    options.extend(_network_bool_options(network, ('ipv4', 'ipv6')))
    options.extend(_raw_network_options(
        network, ('id=', 'ipv4=', 'ipv6=', 'tcp-ports=', 'udp-ports=')))

    # All passt forwards share one bind address. Keep the vmocs management
    # port private by default while allowing templates to opt into 0.0.0.0.
    options.append(f'tcp-ports={bind}/{ssh_port}:22')
    options.extend(f'tcp-ports={value}' for value in tcp_ports)

    # passt otherwise mirrors TCP forward port numbers to UDP implicitly.
    # Be explicit so the management TCP port never creates an unwanted UDP
    # listener. A template can opt into UDP forwards independently.
    if udp_ports:
        options.append(f'udp-ports={bind}/{udp_ports[0]}')
        options.extend(
            f'udp-ports={value}' for value in udp_ports[1:])
    else:
        options.append('udp-ports=none')

    return [
        '-netdev', ','.join(options),
        '-device', 'virtio-net-pci,netdev=net0',
    ]


def _network_cmdline(network, ssh_port, extra_hostfwd=()):
    network = _validated_network_mapping(network)
    mode = network.get('mode', 'user')
    if mode == 'user':
        return _user_network_cmdline(network, ssh_port, extra_hostfwd)
    if mode == 'passt':
        return _passt_network_cmdline(network, ssh_port, extra_hostfwd)
    raise HypervisorError(f'unsupported network mode: {mode!r}')


# ---------------------------------------------------------------------------
# Main QEMU cmdline builder
# ---------------------------------------------------------------------------

def build_qemu_cmdline(qemu_bin, template, cores, memory_mb,
                       disk_path, runtime_dir,
                       ssh_port, qmp_socket,
                       cloud_init_iso=None,
                       snapshot_mem=None,
                       firmware_vars=None,
                       pci_devices=(),
                       extra_disks=(),
                       sidecar_plans=(),
                       network=None,
                       supervised=False):
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
        network:        merged global/template QEMU network policy

    Adapted from pcocc Hypervisor.py:1328-1646.
    """
    cmd = [qemu_bin]
    shared_guest_memory = any(
        plan.requires_shared_guest_memory for plan in sidecar_plans)

    if shared_guest_memory:
        cmd += ['-object',
                f'memory-backend-memfd,id=vmocs.ram,size={memory_mb}M,share=on']

    # Machine type + KVM acceleration
    machine = template.machine_type
    machine_props = [f'type={machine}']
    if template.smm:
        machine_props.append('smm=on')
    if shared_guest_memory:
        machine_props.append('memory-backend=vmocs.ram')
    cpu_flags = ',hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,kvm=off' if template.hyperv else ''
    cpu_model = getattr(template, 'cpu_model', None) or 'host'
    try:
        open('/dev/kvm', 'r+').close()
        machine_props.append('accel=kvm')
        cmd += ['-machine', ','.join(machine_props),
                '-cpu', f'{cpu_model}{cpu_flags}']
    except OSError:
        logging.warning('KVM not available, running without acceleration')
        cmd += ['-machine', ','.join(machine_props)]

    # UEFI firmware pflash pair (code read-only, vars is per-job writable copy)
    if template.firmware:
        if not firmware_vars:
            raise HypervisorError(
                f"template '{template.name}' sets 'firmware' but firmware_vars was not provided")
        cmd += ['-drive', f'if=pflash,format=raw,readonly=on,file={template.firmware}']
        cmd += ['-drive', f'if=pflash,format=raw,file={firmware_vars}']

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

    # Extra disks (persistent, no COW overlay — user owns these files)
    for i, disk in enumerate(extra_disks):
        idx = i + 1
        name = f'drive{idx}'
        cmd += block_cmdline(
            disk.get('device', model),
            disk['file'],
            name, idx,
            disk.get('cache', cache),
            serial=disk.get('serial'),
        )

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

    # Sidecar planning decides whether external processes need shared guest
    # memory. The single memfd is bound directly to -machine above so
    # virtio-fs and vfio-user compose correctly.
    mount_points = template.mount_points or {}
    cmd += ['-m', str(memory_mb)]

    # CPU topology
    cmd += ['-smp', f'threads=1,cores=1,sockets={cores}']

    # Unprivileged networking with a managed SSH path. The global defaults and
    # per-template policy are merged by the launcher.
    if network is None:
        network = getattr(template, 'network', {})
    cmd += _network_cmdline(
        network, ssh_port, getattr(template, 'extra_hostfwd', []))

    # Mount points
    if mount_points:
        cmd += _mount_cmdline(mount_points)

    # Host sidecars are already running by QEMU exec time. Their adapters
    # contribute only pure QEMU arguments here.
    for plan in sidecar_plans:
        cmd += list(plan.qemu_args)

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
    pci_roms = template.pci_roms or {}
    for i, bdf in enumerate(pci_devices):
        romfile = ''
        if pci_roms:
            vid_did = _pci_vendor_device(bdf)
            if vid_did and vid_did in pci_roms:
                romfile = f',romfile={pci_roms[vid_did]}'
        if use_root_port:
            chassis = 6 + i
            slot = 0x15 + i
            port_id = f'pcie.{chassis}'
            cmd += ['-device',
                    f'pcie-root-port,id={port_id},bus=pcie.0,chassis={chassis},slot={slot:#x}']
            cmd += ['-device', f'vfio-pci,host={bdf},bus={port_id}{romfile}']
        else:
            cmd += ['-device', f'vfio-pci,host={bdf}{romfile}']

    # Custom args from template
    if template.custom_args:
        cmd += template.custom_args

    # A supervised session consumes QMP RESET/SHUTDOWN events. Keep QEMU alive
    # long enough for the supervisor to distinguish reboot from poweroff.
    if supervised and '-no-shutdown' not in cmd:
        cmd += ['-no-shutdown']

    return cmd
