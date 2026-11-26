"""Checks that non-Python package data survives an install."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vmocs.error import HypervisorError
from vmocs.keys import VAGRANT_KEY, VAGRANT_PUBKEY, vagrant_key_path


ROOT = Path(__file__).resolve().parents[1]


def test_bundled_vagrant_keys_are_installed(tmp_path):
    """find_packages() collects modules only; the keys need package_data.

    Built output is checked rather than the source tree, where the files are
    always present and a packaging regression is invisible.
    """
    build_lib = tmp_path / 'build-lib'
    subprocess.run(
        [sys.executable, 'setup.py', '--quiet',
         'build_py', '--build-lib', str(build_lib)],
        cwd=ROOT, check=True, capture_output=True, text=True)

    keys_dir = build_lib / 'vmocs' / 'keys'
    assert (keys_dir / os.path.basename(VAGRANT_KEY)).is_file()
    assert (keys_dir / os.path.basename(VAGRANT_PUBKEY)).is_file()


def test_vagrant_key_is_staged_with_a_mode_openssh_accepts():
    """A checkout or user-owned venv leaves the bundled key world-readable."""
    staged = vagrant_key_path()

    assert os.stat(staged).st_mode & 0o777 == 0o600
    assert Path(staged).read_bytes() == Path(VAGRANT_KEY).read_bytes()
    assert vagrant_key_path() == staged


def test_missing_bundled_key_reports_the_packaging_fault(monkeypatch):
    """Without this the vagrant boot mode just times out (issue #31)."""
    monkeypatch.setattr('vmocs.keys._staged_vagrant_key', None)
    monkeypatch.setattr('vmocs.keys.VAGRANT_KEY', '/nonexistent/vagrant_key')

    with pytest.raises(HypervisorError, match='missing from the installed package'):
        vagrant_key_path()
