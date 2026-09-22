#  Copyright (C) 2026 Naveenraj Muthuraj
#  SPDX-License-Identifier: GPL-3.0-or-later

"""Cold, thin VM checkpoint bundles.

Stage 1 deliberately captures only the stopped primary-disk overlay.  The
overlay remains backed by its immutable base image, which keeps checkpointing
proportional to the data written by the guest instead of the virtual disk size.
"""

import datetime
import errno
import json
import os
import shutil
import tempfile
import uuid

from .error import ImageError
from .image import VMImage


CHECKPOINT_SCHEMA = 'vmocs.checkpoint'
CHECKPOINT_VERSION = 1
CHECKPOINT_MODE = 'cold-disk'
DISK_NAME = 'disk.qcow2'
MANIFEST_NAME = 'manifest.json'
COMPLETE_NAME = 'COMPLETE'


def checkpoint_path(path):
    """Return the canonical host path used in persisted lifecycle metadata."""
    return os.path.abspath(os.path.expanduser(path))


def prepare_checkpoint_destination(path):
    """Validate a new checkpoint destination before the VM starts/stops."""
    destination = checkpoint_path(path)
    if os.path.lexists(destination):
        raise ImageError(f'checkpoint destination already exists: {destination}')
    parent = os.path.dirname(destination)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if not os.path.isdir(parent):
        raise ImageError(f'checkpoint parent is not a directory: {parent}')
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ImageError(f'checkpoint parent is not writable: {parent}')
    return destination


def _read_manifest(directory):
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    try:
        with open(manifest_path) as stream:
            manifest = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise ImageError(f'invalid checkpoint manifest {manifest_path}: {exc}')
    if not isinstance(manifest, dict):
        raise ImageError(f'checkpoint manifest is not an object: {manifest_path}')
    if manifest.get('schema') != CHECKPOINT_SCHEMA:
        raise ImageError(f'unsupported checkpoint schema in {manifest_path}')
    if manifest.get('schema_version') != CHECKPOINT_VERSION:
        raise ImageError(
            f'unsupported checkpoint version in {manifest_path}: '
            f'{manifest.get("schema_version")!r}')
    if manifest.get('mode') != CHECKPOINT_MODE:
        raise ImageError(
            f'unsupported checkpoint mode in {manifest_path}: '
            f'{manifest.get("mode")!r}')
    return manifest


def load_checkpoint(path, expected_template=None, check_image=True):
    """Validate a complete Stage-1 checkpoint and return manifest/disk paths."""
    directory = checkpoint_path(path)
    if not os.path.isdir(directory):
        raise ImageError(f'checkpoint is not a directory: {directory}')
    if not os.path.isfile(os.path.join(directory, COMPLETE_NAME)):
        raise ImageError(f'checkpoint is incomplete (missing COMPLETE): {directory}')
    manifest = _read_manifest(directory)
    try:
        with open(os.path.join(directory, COMPLETE_NAME)) as stream:
            complete_id = stream.read().strip()
    except OSError as exc:
        raise ImageError(f'cannot read checkpoint COMPLETE marker: {exc}')
    if not complete_id or complete_id != manifest.get('checkpoint_id'):
        raise ImageError(
            f'checkpoint COMPLETE marker does not match manifest: {directory}')
    template = manifest.get('source', {}).get('template')
    if expected_template and template != expected_template:
        raise ImageError(
            f'checkpoint template {template!r} does not match '
            f'{expected_template!r}')
    disk = os.path.join(directory, DISK_NAME)
    if check_image:
        if VMImage.image_format(disk) != 'qcow2':
            raise ImageError(f'checkpoint disk is not qcow2: {disk}')
        VMImage.check(disk)
    return manifest, disk


def _fsync_file(path):
    with open(path, 'rb') as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_disk(source, destination):
    """Copy a thin qcow2 while preserving sparse regions where supported."""
    def _write_all(descriptor, data):
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise ImageError(f'short write while copying {source}')
            view = view[written:]

    source_fd = os.open(source, os.O_RDONLY)
    try:
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            size = os.fstat(source_fd).st_size
            has_sparse_seek = hasattr(os, 'SEEK_DATA') and hasattr(os, 'SEEK_HOLE')
            if has_sparse_seek:
                while offset < size:
                    try:
                        data_offset = os.lseek(source_fd, offset, os.SEEK_DATA)
                    except OSError as exc:
                        if exc.errno == errno.ENXIO:
                            break
                        if exc.errno in (errno.EINVAL, errno.ENOTSUP):
                            has_sparse_seek = False
                            break
                        raise
                    hole_offset = os.lseek(source_fd, data_offset, os.SEEK_HOLE)
                    os.lseek(source_fd, data_offset, os.SEEK_SET)
                    os.lseek(destination_fd, data_offset, os.SEEK_SET)
                    remaining = hole_offset - data_offset
                    while remaining:
                        data = os.read(source_fd, min(1024 * 1024, remaining))
                        if not data:
                            raise ImageError(f'unexpected EOF while copying {source}')
                        _write_all(destination_fd, data)
                        remaining -= len(data)
                    offset = hole_offset
            if not has_sparse_seek:
                os.lseek(source_fd, 0, os.SEEK_SET)
                os.lseek(destination_fd, 0, os.SEEK_SET)
                while True:
                    data = os.read(source_fd, 1024 * 1024)
                    if not data:
                        break
                    _write_all(destination_fd, data)
            os.ftruncate(destination_fd, size)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    shutil.copystat(source, destination, follow_symlinks=False)


def create_checkpoint(meta, path=None):
    """Publish a stopped primary-disk overlay as an atomic checkpoint bundle.

    The source overlay is never moved or modified.  A failed save therefore
    leaves the runtime intact for retry.  Publication is an atomic directory
    rename, and only a fully validated bundle receives a COMPLETE marker.
    """
    requested = path or meta.get('save_path')
    if not requested:
        raise ImageError('no checkpoint destination was requested')
    destination = checkpoint_path(requested)
    checkpoint_id = meta.get('checkpoint_id')

    if os.path.isdir(destination):
        existing, _ = load_checkpoint(destination)
        if (checkpoint_id and
                existing.get('checkpoint_id') == checkpoint_id):
            return existing
        raise ImageError(f'checkpoint destination already exists: {destination}')
    if os.path.lexists(destination):
        raise ImageError(f'checkpoint destination already exists: {destination}')

    source = meta.get('overlay')
    if not source or not os.path.isfile(source):
        raise ImageError(f'VM overlay not found: {source}')
    if VMImage.image_format(source) != 'qcow2':
        raise ImageError(f'VM overlay is not qcow2: {source}')
    VMImage.check(source)

    parent = os.path.dirname(destination)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = tempfile.mkdtemp(
        prefix=f'.{os.path.basename(destination)}.partial-', dir=parent)
    os.chmod(temporary, 0o700)
    published = False
    try:
        disk = os.path.join(temporary, DISK_NAME)
        _copy_disk(source, disk)
        if VMImage.image_format(disk) != 'qcow2':
            raise ImageError(f'copied checkpoint disk is not qcow2: {disk}')
        VMImage.check(disk)

        backing_file = VMImage.backing_file(disk)
        manifest = {
            'schema': CHECKPOINT_SCHEMA,
            'schema_version': CHECKPOINT_VERSION,
            'checkpoint_id': checkpoint_id or str(uuid.uuid4()),
            'mode': CHECKPOINT_MODE,
            'consistency': meta.get(
                'checkpoint_consistency', 'stopped-primary-disk'),
            'created_at': datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            'source': {
                'job_id': meta.get('job_id'),
                'template': meta.get('template'),
                'base_image': meta.get('base_image'),
                'resume_checkpoint': meta.get('resume_path'),
            },
            'resources': {
                'cores': meta.get('cores'),
                'memory_mb': meta.get('memory_mb'),
                'pci_devices': meta.get('pci_devices', []),
            },
            'disk': {
                'path': DISK_NAME,
                'format': 'qcow2',
                'backing_file': backing_file,
            },
            'captures': ['primary-disk-writes'],
            'not_captured': [
                'guest-memory',
                'cpu-state',
                'gpu-and-pci-device-state',
                'sidecar-state',
                'uefi-nvram',
                'tpm-state',
                'extra-disks',
                'host-mounts-and-virtiofs-content',
            ],
        }
        manifest_path = os.path.join(temporary, MANIFEST_NAME)
        with open(manifest_path, 'x') as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        complete_path = os.path.join(temporary, COMPLETE_NAME)
        with open(complete_path, 'x') as stream:
            stream.write(f'{manifest["checkpoint_id"]}\n')
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_file(disk)
        _fsync_directory(temporary)

        if os.path.lexists(destination):
            raise ImageError(
                f'checkpoint destination appeared while saving: {destination}')
        os.rename(temporary, destination)
        published = True
        _fsync_directory(parent)
        return manifest
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)
