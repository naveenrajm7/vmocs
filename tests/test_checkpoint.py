"""Tests for Stage-1 cold primary-disk checkpoint bundles."""

import json

import pytest

from vmocs.checkpoint import create_checkpoint, load_checkpoint
from vmocs.error import ImageError


def _meta(tmp_path):
    overlay = tmp_path / 'runtime' / 'disk.qcow2'
    overlay.parent.mkdir()
    overlay.write_bytes(b'qcow2-delta')
    return {
        'job_id': 42,
        'template': 'agent-vm',
        'overlay': str(overlay),
        'base_image': '/images/base.qcow2',
        'cores': 4,
        'memory_mb': 8192,
        'pci_devices': ['0000:03:00.0'],
        'checkpoint_id': 'checkpoint-42',
    }


def _mock_qemu_img(monkeypatch):
    monkeypatch.setattr(
        'vmocs.checkpoint.VMImage.image_format', lambda _path: 'qcow2')
    monkeypatch.setattr(
        'vmocs.checkpoint.VMImage.check', lambda _path: None)
    monkeypatch.setattr(
        'vmocs.checkpoint.VMImage.backing_file',
        lambda _path: '/images/base.qcow2')


def test_create_checkpoint_atomically_publishes_thin_disk(monkeypatch, tmp_path):
    _mock_qemu_img(monkeypatch)
    meta = _meta(tmp_path)
    destination = tmp_path / 'checkpoints' / 'step-1'

    manifest = create_checkpoint(meta, str(destination))

    assert (destination / 'COMPLETE').read_text().strip() == 'checkpoint-42'
    assert (destination / 'disk.qcow2').read_bytes() == b'qcow2-delta'
    assert (tmp_path / 'runtime' / 'disk.qcow2').exists()
    assert manifest['mode'] == 'cold-disk'
    assert manifest['captures'] == ['primary-disk-writes']
    assert 'guest-memory' in manifest['not_captured']
    assert not list((tmp_path / 'checkpoints').glob('.step-1.partial-*'))


def test_load_checkpoint_rejects_partial_bundle(tmp_path):
    destination = tmp_path / 'partial'
    destination.mkdir()
    (destination / 'manifest.json').write_text(json.dumps({
        'schema': 'vmocs.checkpoint',
        'schema_version': 1,
        'mode': 'cold-disk',
    }))

    with pytest.raises(ImageError, match='missing COMPLETE'):
        load_checkpoint(str(destination), check_image=False)


def test_load_checkpoint_rejects_template_mismatch(monkeypatch, tmp_path):
    _mock_qemu_img(monkeypatch)
    destination = tmp_path / 'checkpoint'
    create_checkpoint(_meta(tmp_path), str(destination))

    with pytest.raises(ImageError, match='does not match'):
        load_checkpoint(
            str(destination), expected_template='different-template',
            check_image=False)


def test_load_checkpoint_rejects_mismatched_complete_marker(
        monkeypatch, tmp_path):
    _mock_qemu_img(monkeypatch)
    destination = tmp_path / 'checkpoint'
    create_checkpoint(_meta(tmp_path), str(destination))
    (destination / 'COMPLETE').write_text('different-checkpoint\n')

    with pytest.raises(ImageError, match='does not match manifest'):
        load_checkpoint(str(destination), check_image=False)


def test_checkpoint_retry_is_idempotent_for_same_checkpoint_id(
        monkeypatch, tmp_path):
    _mock_qemu_img(monkeypatch)
    meta = _meta(tmp_path)
    destination = tmp_path / 'checkpoint'
    first = create_checkpoint(meta, str(destination))

    second = create_checkpoint(meta, str(destination))

    assert second == first
