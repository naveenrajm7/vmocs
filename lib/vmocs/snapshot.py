#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later
#
#  Snapshot create/restore — adapted from pcocc ckpt workflow
#  (Hypervisor.py:1021-1028, 1337-1341, 1713-1723; cmd.py:730-791)
#
#  Snapshot directory layout:
#    <snap_dir>/
#      memory        — lzop-compressed QEMU memory image
#      disk.qcow2    — flattened disk image (base for future COW overlays)
#      id_ed25519    — SSH private key (reused by every job launched from snapshot)
#      id_ed25519.pub

import json
import logging
import os
import shutil
import time

from .error import HypervisorError
from .image import VMImage
from .launch import generate_ssh_keypair, wait_for_ssh
from .hypervisor import build_qemu_cmdline, _find_free_port
from .monitor import wait_for_monitor


def create_snapshot(cfg, template, snap_dir, cores=2, memory_mb=2048):
    """Boot a VM from template, wait for SSH, save memory + disk to snap_dir.

    Adapted from pcocc ckpt workflow (cmd.py:751-791, Hypervisor.py:1021-1028).

    Args:
        cfg:        Config object
        template:   Template object (resolved)
        snap_dir:   Destination directory (must not exist)
        cores:      vCPUs for the snapshot VM
        memory_mb:  RAM in MB for the snapshot VM
    """
    if os.path.exists(snap_dir):
        raise HypervisorError(f'snapshot dir already exists: {snap_dir}')
    os.makedirs(snap_dir)

    import tempfile
    runtime_dir = tempfile.mkdtemp(prefix='vmocs-snap-')
    try:
        _do_create(cfg, template, snap_dir, runtime_dir, cores, memory_mb)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _do_create(cfg, template, snap_dir, runtime_dir, cores, memory_mb):
    import glob as _glob

    # Resolve base image
    if template.image_dir:
        candidates = _glob.glob(os.path.join(template.image_dir, '*.qcow2'))
        if not candidates:
            raise HypervisorError(f'no qcow2 found in {template.image_dir}')
        base_image = candidates[0]
    elif template.image:
        base_image = template.image
    else:
        raise HypervisorError('template has no image or image-dir')

    overlay = os.path.join(runtime_dir, 'disk.qcow2')
    VMImage.create_cow_overlay(base_image, overlay)

    key_path, pubkey = generate_ssh_keypair(runtime_dir)

    port_range = cfg.network.get('ssh-port-range', [60222, 60322])
    ssh_port = _find_free_port(port_range)
    qmp_socket = os.path.join(runtime_dir, 'qmp.sock')
    qemu_bin = template.qemu_bin or cfg.qemu_bin

    cmd = build_qemu_cmdline(
        qemu_bin=qemu_bin,
        template=template,
        cores=cores,
        memory_mb=memory_mb,
        disk_path=overlay,
        runtime_dir=runtime_dir,
        ssh_pubkey=pubkey,
        ssh_port=ssh_port,
        qmp_socket=qmp_socket,
        ssh_user=template.ssh_user,
    )

    import subprocess
    qemu_pid = os.fork()
    if qemu_pid == 0:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.execvp(cmd[0], cmd)

    try:
        mon = wait_for_monitor(qmp_socket, timeout=30)
    except HypervisorError:
        os.waitpid(qemu_pid, 0)
        raise HypervisorError('QEMU failed to start during snapshot creation')

    mon.cont()

    ssh_timeout = template.ssh_timeout
    if not wait_for_ssh('127.0.0.1', ssh_port, key_path, ssh_timeout,
                        template.ssh_user):
        mon.quit()
        os.waitpid(qemu_pid, 0)
        raise HypervisorError(f'VM SSH not ready after {ssh_timeout}s')

    logging.info('VM ready, saving memory snapshot...')

    # Stop the VM before migration for a consistent snapshot (pcocc:759-762)
    mon.stop()

    mem_path = os.path.join(snap_dir, 'memory')
    mon.migrate_to_file(mem_path)

    # Poll until migration completes (pcocc:967-977)
    while True:
        status = mon.query_migrate().get('status', 'active')
        if status == 'completed':
            break
        if status == 'failed':
            mon.quit()
            os.waitpid(qemu_pid, 0)
            raise HypervisorError('QEMU migration failed during snapshot')
        time.sleep(1)

    logging.info('Memory saved, flattening disk...')

    # Flatten COW overlay → standalone disk (no backing-file chain)
    disk_dest = os.path.join(snap_dir, 'disk.qcow2')
    VMImage.convert_standalone(overlay, disk_dest)

    # Kill snapshot VM — we don't need it anymore
    mon.quit()
    os.waitpid(qemu_pid, 0)

    # Copy SSH keypair into snap_dir so every job launched from this snapshot
    # can authenticate (same approach as pcocc pre-distributed cluster keys)
    for ext in ('', '.pub'):
        shutil.copy2(key_path + ext, os.path.join(snap_dir, 'id_ed25519' + ext))

    logging.info('Snapshot created at %s', snap_dir)
