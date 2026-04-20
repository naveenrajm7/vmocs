"""Unit tests for image.py."""

import os
import subprocess
import tempfile
import json
import pytest

from vmocs.image import VMImage
from vmocs.error import ImageError


@pytest.fixture
def base_qcow2(tmp_path):
    img = str(tmp_path / 'base.qcow2')
    subprocess.check_call(
        ['qemu-img', 'create', '-f', 'qcow2', img, '64M'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return img


def test_image_format(base_qcow2):
    assert VMImage.image_format(base_qcow2) == 'qcow2'


def test_image_format_missing():
    with pytest.raises(ImageError):
        VMImage.image_format('/nonexistent/image.qcow2')


def test_create_cow_overlay(base_qcow2, tmp_path):
    overlay = str(tmp_path / 'overlay.qcow2')
    VMImage.create_cow_overlay(base_qcow2, overlay)

    assert os.path.isfile(overlay)
    backing = VMImage.backing_file(overlay)
    assert backing is not None
    assert os.path.basename(backing) == 'base.qcow2'


def test_create_cow_overlay_missing_base(tmp_path):
    with pytest.raises(ImageError):
        VMImage.create_cow_overlay('/no/such/base.qcow2', str(tmp_path / 'out.qcow2'))
