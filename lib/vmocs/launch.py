#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  VM lifecycle: launch, SSH wait, teardown.
#  QEMU is fork/exec'd so it inherits the caller's cgroup automatically
#  (same principle as pcocc Hypervisor.py:1664-1674).

import json
import logging
import os
import subprocess
import tempfile
import time

from .error import HypervisorError, ImageError
from .image import VMImage
from .hypervisor import build_qemu_cmdline, _make_cloud_init_iso, _find_free_port
from .keys import VAGRANT_KEY
from .monitor import wait_for_monitor


def generate_ssh_keypair(runtime_dir):
    """Generate an ED25519 keypair in runtime_dir. Return (privkey_path, pubkey_str)."""
    key_path = os.path.join(runtime_dir, 'id_ed25519')
    subprocess.check_call(
        ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', key_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(key_path + '.pub') as f:
        pubkey = f.read().strip()
    return key_path, pubkey


def _rotate_vagrant_key(host, port, pubkey, timeout, ssh_user='vagrant'):
    """SSH in with the Vagrant insecure key and replace it with an ephemeral pubkey.

    Polls until SSH accepts connections, then overwrites authorized_keys so the
    well-known insecure key is immediately revoked.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ret = subprocess.call(
            ['ssh',
             '-i', VAGRANT_KEY,
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=/dev/null',
             '-o', 'ConnectTimeout=2',
             '-p', str(port),
             f'{ssh_user}@{host}',
             f"printf '%s\\n' '{pubkey}' > ~/.ssh/authorized_keys"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ret == 0:
            return
        time.sleep(2)
    raise HypervisorError(f'vagrant key rotation timed out after {timeout}s')


def wait_for_ssh(host, port, key_path, timeout, ssh_user='root'):
    """Poll SSH until the VM accepts connections. Return True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ret = subprocess.call(
            ['ssh',
             '-i', key_path,
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=/dev/null',
             '-o', f'ConnectTimeout=2',
             '-p', str(port),
             f'{ssh_user}@{host}',
             'true'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ret == 0:
            return True
        time.sleep(2)
    return False


def launch_vm(cfg, template, cores, memory_mb, job_id=None):
    """
    Launch a VM and wait for SSH. Returns a dict with runtime metadata.

    Steps (mirrors pcocc Hypervisor.py:1328-1710):
      1. Create per-job runtime dir
      2. Create COW overlay over base image
      3. Generate SSH keypair
      4. Allocate SSH port
      5. Build QEMU cmdline
      6. fork/exec QEMU (inherits caller's cgroup)
      7. Connect QMP, bind vCPUs, cont()
      8. Poll SSH until ready
      9. Write vm.json metadata
    """
    if job_id is None:
        job_id = os.getpid()

    runtime_dir = os.path.join(cfg.runtime_dir, str(job_id))
    os.makedirs(runtime_dir, exist_ok=True)

    # Detect snapshot: template.snapshot points to a snapshot dir
    snap_dir = template.snapshot
    using_snapshot = bool(snap_dir and os.path.isfile(
        os.path.join(snap_dir, 'memory')))

    # Resolve base image: prefer snapshot disk, then template image/image-dir
    if using_snapshot:
        base_image = os.path.join(snap_dir, 'disk.qcow2')
    elif template.image_dir:
        import glob
        candidates = glob.glob(os.path.join(template.image_dir, '*.qcow2'))
        if not candidates:
            raise HypervisorError(f'no qcow2 image found in {template.image_dir}')
        base_image = candidates[0]
    elif template.image:
        base_image = template.image
    else:
        raise HypervisorError('template has no image or image-dir')

    # 1. COW overlay
    overlay = os.path.join(runtime_dir, 'disk.qcow2')
    VMImage.create_cow_overlay(base_image, overlay)

    # 2. SSH keypair — always ephemeral; snapshots reuse their pre-burned key
    boot_mode = template.boot_mode
    ssh_user = template.ssh_user
    snapshot_mem = os.path.join(snap_dir, 'memory') if using_snapshot else None

    if using_snapshot and boot_mode == 'cloud-init':
        key_path = os.path.join(snap_dir, 'id_ed25519')
        with open(key_path + '.pub') as f:
            pubkey = f.read().strip()
    else:
        key_path, pubkey = generate_ssh_keypair(runtime_dir)

    # boot-mode only decides how the key reaches the VM
    if boot_mode == 'cloud-init':
        cloud_init_iso = _make_cloud_init_iso(runtime_dir, pubkey, ssh_user=ssh_user)
    elif boot_mode == 'vagrant':
        cloud_init_iso = None   # key is rotated post-boot via _rotate_vagrant_key
    else:
        raise HypervisorError(f'unknown boot-mode: {boot_mode!r}')

    # 3. SSH port
    port_range = cfg.network.get('ssh-port-range', [60222, 60322])
    ssh_port = _find_free_port(port_range)

    qmp_socket = os.path.join(runtime_dir, 'qmp.sock')
    qemu_bin = template.qemu_bin or cfg.qemu_bin

    # 4. Build QEMU cmdline
    cmd = build_qemu_cmdline(
        qemu_bin=qemu_bin,
        template=template,
        cores=cores,
        memory_mb=memory_mb,
        disk_path=overlay,
        runtime_dir=runtime_dir,
        ssh_port=ssh_port,
        qmp_socket=qmp_socket,
        cloud_init_iso=cloud_init_iso,
        snapshot_mem=snapshot_mem,
    )

    # 5. fork/exec QEMU — child inherits our cgroup (pcocc:1664-1674)
    qemu_pid = os.fork()
    if qemu_pid == 0:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.execvp(cmd[0], cmd)

    # 6. Connect QMP and start VM (pcocc:1676-1723)
    try:
        mon = wait_for_monitor(qmp_socket, timeout=30)
    except HypervisorError:
        os.waitpid(qemu_pid, 0)
        raise HypervisorError('QEMU failed to start (QMP timeout)')

    # Snapshot restore: wait for incoming migration to finish, then cont (pcocc:1713-1723)
    if using_snapshot:
        while mon.query_status() == 'inmigrate':
            time.sleep(1)
    mon.cont()
    mon.close()  # Release QMP connection — QEMU serves one client at a time

    # Vagrant: SSH in with the well-known insecure key, replace it with our ephemeral key
    ssh_timeout = template.ssh_timeout
    if boot_mode == 'vagrant':
        _rotate_vagrant_key('127.0.0.1', ssh_port, pubkey, ssh_timeout, ssh_user)

    # 7. Wait for SSH (ephemeral key for both modes)
    if not wait_for_ssh('127.0.0.1', ssh_port, key_path, ssh_timeout, ssh_user):
        mon = wait_for_monitor(qmp_socket, timeout=10)
        mon.quit()
        mon.close()
        os.waitpid(qemu_pid, 0)
        raise HypervisorError(f'VM SSH not ready after {ssh_timeout}s')

    # 8. Write metadata
    meta = {
        'job_id': job_id,
        'pid': qemu_pid,
        'boot_mode': boot_mode,
        'ssh_port': ssh_port,
        'ssh_user': ssh_user,
        'key_path': key_path,
        'overlay': overlay,
        'qmp_socket': qmp_socket,
        'runtime_dir': runtime_dir,
        'template': template.name,
    }
    with open(os.path.join(runtime_dir, 'vm.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    return meta


def _kill_qemu(pid, timeout=10):
    """SIGTERM → wait → SIGKILL."""
    try:
        os.kill(pid, 15)
        for _ in range(timeout):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _graceful_shutdown(qmp_socket, pid, timeout=60):
    """Send ACPI powerdown via QMP so the guest flushes filesystems cleanly.

    Falls back to SIGTERM if the QMP socket is gone or the guest doesn't
    shut down within timeout seconds.
    """
    try:
        from .monitor import QemuMonitor
        mon = QemuMonitor(qmp_socket)
        mon._validate('{"execute": "system_powerdown"}\n\n')
        mon.close()
    except Exception:
        _kill_qemu(pid)
        return

    # Wait for QEMU to exit naturally (guest runs shutdown, syncs filesystems)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # QEMU exited cleanly

    # Guest didn't shut down in time — force kill
    _kill_qemu(pid)


def teardown_vm(job_id, runtime_dir=None, runtime_base='/var/run/vmocs',
                save_path=None):
    """Kill QEMU, optionally save disk state, then remove runtime dir.

    Args:
        save_path: If given, flatten the COW overlay into a new standalone
                   qcow2 at this path before cleanup (--vm-save equivalent).
                   Uses a graceful guest shutdown so the guest OS syncs
                   its filesystems before we convert the overlay.
    """
    if runtime_dir is None:
        runtime_dir = os.path.join(runtime_base, str(job_id))

    vm_json = os.path.join(runtime_dir, 'vm.json')
    if not os.path.exists(vm_json):
        raise HypervisorError(f'no vm.json in {runtime_dir}')

    with open(vm_json) as f:
        meta = json.load(f)

    pid = meta['pid']

    if save_path:
        # Graceful ACPI shutdown so guest syncs filesystems before we convert
        _graceful_shutdown(meta['qmp_socket'], pid, timeout=60)
        VMImage.convert_standalone(meta['overlay'], save_path)
    else:
        _kill_qemu(pid)

    import shutil
    shutil.rmtree(runtime_dir, ignore_errors=True)
    return meta
