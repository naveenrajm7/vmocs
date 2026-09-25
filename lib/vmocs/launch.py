#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  VM lifecycle: launch, SSH wait, teardown.
#  QEMU is fork/exec'd so it inherits the caller's cgroup automatically
#  (same principle as pcocc Hypervisor.py:1664-1674).

import json
import logging
import os
import socket
import subprocess
import tempfile
import time
import uuid

from .checkpoint import (
    checkpoint_path,
    create_checkpoint,
    load_checkpoint,
    prepare_checkpoint_destination,
)
from .error import HypervisorError
from .image import VMImage
from .hypervisor import build_qemu_cmdline, _make_cloud_init_iso, _find_free_port
from .keys import VAGRANT_KEY
from .monitor import wait_for_monitor
from .sidecars import (
    SidecarManager,
    plan_sidecars,
    preflight_qemu,
    read_boot_id,
    stop_persisted_sidecars,
)


def _write_metadata(runtime_dir, meta):
    """Atomically persist public VM state."""
    path = os.path.join(runtime_dir, 'vm.json')
    temp_path = path + '.tmp'
    public = {key: value for key, value in meta.items()
              if not key.startswith('_')}
    with open(temp_path, 'w') as stream:
        json.dump(public, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)
    descriptor = os.open(
        runtime_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def is_vm_stopping(runtime_dir):
    """Return whether another vmocs process has begun an explicit stop."""
    try:
        with open(os.path.join(runtime_dir, 'vm.json')) as stream:
            return json.load(stream).get('state') == 'stopping'
    except (OSError, ValueError, TypeError):
        return False


def stop_vm_sidecars(meta):
    """Stop live launch handles or recover sidecars from persisted metadata."""
    manager = meta.get('_sidecar_manager')
    if manager is not None:
        manager.stop_all()
    else:
        stop_persisted_sidecars(meta)


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
    """Upload ephemeral pubkey via scp, revoking the well-known Vagrant insecure key.

    Uses scp (no remote shell command) so it works on Linux and Windows guests.
    Polls until scp succeeds or timeout expires.
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.pub', delete=False) as f:
        f.write(pubkey + '\n')
        tmp_pub = f.name
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ret = subprocess.call(
                ['scp',
                 '-i', VAGRANT_KEY,
                 '-o', 'StrictHostKeyChecking=no',
                 '-o', 'UserKnownHostsFile=/dev/null',
                 '-o', 'ConnectTimeout=2',
                 '-P', str(port),
                 tmp_pub,
                 f'{ssh_user}@{host}:.ssh/authorized_keys'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ret == 0:
                return
            time.sleep(2)
        raise HypervisorError(f'vagrant key rotation timed out after {timeout}s')
    finally:
        os.unlink(tmp_pub)


def wait_for_ssh(host, port, key_path, timeout, ssh_user='root',
                 stop_requested=None):
    """Poll until the SSH daemon serves a banner. Works for Linux and Windows guests."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_requested and stop_requested():
            return False
        try:
            with socket.create_connection((host, port), timeout=2) as s:
                if s.recv(256).startswith(b'SSH-'):
                    return True
        except OSError:
            pass
        time.sleep(2)
    return False


def launch_vm(cfg, template, cores, memory_mb, job_id=None, pci_devices=(),
              supervised=False, save_path=None, resume_path=None):
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

    if save_path:
        save_path = prepare_checkpoint_destination(save_path)
    if resume_path:
        resume_path = checkpoint_path(resume_path)
        _, resume_disk = load_checkpoint(
            resume_path, expected_template=template.name)
    else:
        resume_disk = None

    runtime_dir = os.path.join(cfg.runtime_dir, str(job_id))
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    if os.path.exists(os.path.join(runtime_dir, 'vm.json')):
        raise HypervisorError(
            f'runtime already exists for job {job_id}: {runtime_dir}')

    # Detect snapshot: template.snapshot points to a snapshot dir
    snap_dir = template.snapshot
    using_snapshot = bool(not resume_disk and snap_dir and os.path.isfile(
        os.path.join(snap_dir, 'memory')))

    # Resolve base image: a cold checkpoint overrides the template's boot
    # snapshot but keeps the template's current device/sidecar configuration.
    if resume_disk:
        base_image = resume_disk
    elif using_snapshot:
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
    base_image = os.path.abspath(base_image)

    # 1. COW overlay
    logging.info('Creating disk overlay...')
    overlay = os.path.join(runtime_dir, 'disk.qcow2')
    VMImage.create_cow_overlay(base_image, overlay)

    # 2. SSH keypair — always ephemeral; snapshots reuse their pre-burned key
    boot_mode = template.boot_mode
    ssh_user = template.ssh_user
    snapshot_mem = os.path.join(snap_dir, 'memory') if using_snapshot else None

    # boot-mode + insert-key decide how (or whether) the key reaches the VM
    insert_key = template.insert_key
    pubkey = None

    if not insert_key:
        # Mirrors Vagrant's config.ssh.insert_key = false:
        # use the pre-installed key from the image, no ephemeral keypair needed.
        key_path = template.ssh_key  # None is fine — user relies on their own SSH config
        cloud_init_iso = None
    elif using_snapshot and boot_mode == 'cloud-init':
        key_path = os.path.join(snap_dir, 'id_ed25519')
        with open(key_path + '.pub') as f:
            pubkey = f.read().strip()
        cloud_init_iso = None  # set below
    else:
        key_path, pubkey = generate_ssh_keypair(runtime_dir)

    if insert_key:
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

    # UEFI NVRAM: copy vars template into runtime_dir so each job gets an isolated store
    firmware_vars = None
    if template.firmware:
        vars_template = template.firmware_vars_template
        if not vars_template:
            raise HypervisorError(
                f"template '{template.name}' sets 'firmware' but has no 'firmware-vars-template'")
        import shutil
        firmware_vars = os.path.join(runtime_dir, 'nvram.fd')
        shutil.copy2(vars_template, firmware_vars)

    # Resolve and validate every host sidecar before the first process starts.
    sidecar_plans = plan_sidecars(cfg, template, runtime_dir)
    preflight_qemu(qemu_bin, template, sidecar_plans,
                   using_snapshot=using_snapshot)

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
        firmware_vars=firmware_vars,
        pci_devices=pci_devices,
        extra_disks=template.extra_disks or [],
        sidecar_plans=sidecar_plans,
        supervised=supervised,
    )

    ssh_timeout = template.ssh_timeout
    meta = {
        'job_id': job_id,
        'pid': None,
        'boot_mode': boot_mode,
        'ssh_port': ssh_port,
        'ssh_user': ssh_user,
        'key_path': key_path,
        'overlay': overlay,
        'base_image': base_image,
        'qmp_socket': qmp_socket,
        'runtime_dir': runtime_dir,
        'template': template.name,
        'cores': cores,
        'memory_mb': memory_mb,
        'pci_devices': list(pci_devices),
        'save_path': save_path,
        'resume_path': resume_path,
        'checkpoint_id': str(uuid.uuid4()) if save_path else None,
        'ssh_timeout': ssh_timeout,
        'state': 'starting',
        'boot_id': read_boot_id(),
        'sidecars': [],
    }
    _write_metadata(runtime_dir, meta)

    manager = SidecarManager(sidecar_plans)
    qemu_pid = None
    try:
        manager.start_all()
        meta['sidecars'] = manager.metadata()
        _write_metadata(runtime_dir, meta)

        # 5. fork/exec QEMU — child inherits our cgroup (pcocc:1664-1674)
        logging.info('Starting QEMU...')
        qemu_pid = os.fork()
        if qemu_pid == 0:
            os.setpgid(0, 0)
            logfd = os.open(os.path.join(runtime_dir, 'qemu.log'),
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(logfd, 1)
            os.dup2(logfd, 2)
            try:
                os.execvp(cmd[0], cmd)
            except OSError as exc:
                os.write(2, f'QEMU exec failed: {exc}\n'.encode())
                os._exit(127)

        meta['pid'] = qemu_pid
        meta['state'] = 'qemu-starting'
        _write_metadata(runtime_dir, meta)

        # 6. Connect QMP and start VM (pcocc:1676-1723)
        logging.info('Waiting for QEMU monitor...')
        mon = wait_for_monitor(qmp_socket, timeout=30)

        # Snapshot restore: wait for incoming migration to finish, then cont
        if using_snapshot:
            while mon.query_status() == 'inmigrate':
                time.sleep(1)
        mon.cont()
        mon.close()  # Release QMP connection — QEMU serves one client at a time
        logging.info('VM booting, waiting for SSH...')

        # Vagrant: replace the well-known key with the ephemeral key.
        if boot_mode == 'vagrant' and insert_key:
            _rotate_vagrant_key(
                '127.0.0.1', ssh_port, pubkey, ssh_timeout, ssh_user)

        # 7. Wait for SSH (ephemeral key for both modes)
        if not wait_for_ssh(
                '127.0.0.1', ssh_port, key_path, ssh_timeout, ssh_user):
            raise HypervisorError(f'VM SSH not ready after {ssh_timeout}s')

        # 8. Write final metadata and retain live manager handles in memory.
        meta['state'] = 'running'
        _write_metadata(runtime_dir, meta)
        meta['_sidecar_manager'] = manager
        return meta
    except Exception:
        if qemu_pid is not None:
            _kill_qemu(qemu_pid, timeout=2)
            try:
                os.waitpid(qemu_pid, 0)
            except ChildProcessError:
                pass
        manager.stop_all()
        meta['state'] = 'failed'
        meta['sidecars'] = manager.metadata()
        _write_metadata(runtime_dir, meta)
        raise


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
        return False

    # Wait for QEMU to exit naturally (guest runs shutdown, syncs filesystems)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # QEMU exited cleanly

    # Guest didn't shut down in time — force kill
    _kill_qemu(pid)
    return False


def teardown_vm(job_id, runtime_dir=None, runtime_base='/var/run/vmocs',
                save_path=None):
    """Stop QEMU, optionally publish a cold checkpoint, then clean up.

    Args:
        save_path: Optional checkpoint directory.  Persisted launch-time save
                   intent is used when this is omitted, which lets the Slurm
                   exit hook recover an interrupted supervisor.
    """
    if runtime_dir is None:
        runtime_dir = os.path.join(runtime_base, str(job_id))

    vm_json = os.path.join(runtime_dir, 'vm.json')
    if not os.path.exists(vm_json):
        raise HypervisorError(f'no vm.json in {runtime_dir}')

    with open(vm_json) as f:
        meta = json.load(f)

    requested_save = save_path or meta.get('save_path')
    if save_path:
        requested_save = prepare_checkpoint_destination(save_path)
        meta['save_path'] = requested_save
        meta['checkpoint_id'] = str(uuid.uuid4())

    # Publish intent before QEMU closes its sidecar connections. A blocking
    # supervisor can then distinguish expected shutdown exits from failures.
    meta['state'] = 'stopping'
    _write_metadata(runtime_dir, meta)

    pid = meta.get('pid')

    remove_runtime = False
    try:
        if requested_save and pid is not None:
            # A clean shutdown is the Stage-1 consistency boundary.  The disk
            # is copied only after QEMU can no longer write to it.
            clean = _graceful_shutdown(meta['qmp_socket'], pid, timeout=60)
            meta['checkpoint_consistency'] = (
                'clean-shutdown' if clean else 'crash-consistent')
        elif pid is not None:
            _kill_qemu(pid)

        stop_vm_sidecars(meta)
        if requested_save:
            meta['state'] = 'checkpointing'
            _write_metadata(runtime_dir, meta)
            create_checkpoint(meta, requested_save)
            meta['state'] = 'checkpointed'
            _write_metadata(runtime_dir, meta)
        remove_runtime = True
    finally:
        # Sidecar cleanup is idempotent and must still happen after a failed
        # checkpoint; the stopped source overlay is intentionally retained.
        stop_vm_sidecars(meta)

    # Backward-compatible cleanup for runtimes created before sidecar metadata.
    import glob, signal, shutil
    for pid_file in glob.glob(os.path.join(runtime_dir, '*.pid')):
        try:
            with open(pid_file) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except OSError:
            pass
    if remove_runtime:
        shutil.rmtree(runtime_dir, ignore_errors=True)
    return meta
