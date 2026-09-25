#  Copyright (C) 2014-2015 CEA/DAM/DIF
#  Copyright (C) 2026 Naveenraj Muthuraj
#
#  Based on pcocc Image.py by CEA/DAM/DIF (VMImage class, lines 117-330)
#  SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import subprocess

from .error import ImageError


class VMImage:
    """qemu-img wrappers. Adapted from pcocc VMImage (Image.py:117-330)."""

    KNOWN_FORMATS = {'raw', 'qcow2', 'qed', 'vdi', 'vpc', 'vmdk'}

    @staticmethod
    def image_format(path):
        """Return the image format string via qemu-img info, or raise ImageError."""
        if not os.path.isfile(path):
            raise ImageError(f'{path} is not a file')
        if not os.access(path, os.R_OK):
            raise ImageError(f'{path} is not readable')
        try:
            out = subprocess.check_output(
                ['qemu-img', 'info', '-U', '--output=json', path],
                stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            raise ImageError(f'qemu-img info failed for {path}: {e}')
        try:
            return json.loads(out).get('format')
        except json.JSONDecodeError as e:
            raise ImageError(f'could not parse qemu-img output: {e}')

    @staticmethod
    def backing_file(path):
        """Return the backing file path for a COW image, or None."""
        if not os.path.isfile(path):
            raise ImageError(f'{path} is not a file')
        try:
            out = subprocess.check_output(
                ['qemu-img', 'info', '-U', '--output=json', path],
                stderr=subprocess.DEVNULL).decode()
        except subprocess.CalledProcessError:
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        return data.get('full-backing-filename') or data.get('backing-filename')

    @staticmethod
    def check(path):
        """Validate an image and its backing chain with qemu-img check."""
        if not os.path.isfile(path):
            raise ImageError(f'image not found: {path}')
        try:
            subprocess.check_call(
                ['qemu-img', 'check', '-q', path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            raise ImageError(f'qemu-img check failed for {path}: {e}')

    @staticmethod
    def convert_standalone(overlay_path, dest_path):
        """Flatten a COW overlay (with all user changes) into a new standalone qcow2.

        The result has no backing-file dependency — safe to use as a new base image.
        Adapted from pcocc VMImage.convert (Image.py:269-303).
        """
        if not os.path.isfile(overlay_path):
            raise ImageError(f'overlay not found: {overlay_path}')
        try:
            subprocess.check_call(
                ['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2',
                 overlay_path, dest_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            raise ImageError(f'qemu-img convert failed: {e}')

    @staticmethod
    def create_cow_overlay(base_path, overlay_path):
        """Create a qcow2 COW overlay backed by base_path.

        Near-instant: writes a ~200KB qcow2 header, copies no data.
        Adapted from pcocc Hypervisor.py:1374-1386.
        """
        if not os.path.isfile(base_path):
            raise ImageError(f'base image not found: {base_path}')
        try:
            subprocess.check_call(
                ['qemu-img', 'create', '-f', 'qcow2',
                 '-F', 'qcow2', '-b', base_path, overlay_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            raise ImageError(f'failed to create COW overlay: {e}')
