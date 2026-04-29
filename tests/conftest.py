"""Shared fixtures for all tests."""

import builtins
import pytest
from unittest.mock import MagicMock

_real_open = builtins.open


def _open_stub(path, mode='r', **kwargs):
    if path == '/dev/kvm':
        return MagicMock()
    return _real_open(path, mode, **kwargs)


@pytest.fixture(autouse=True)
def _stub_kvm(monkeypatch):
    """Make KVM appear available so -cpu host is always emitted."""
    monkeypatch.setattr('builtins.open', _open_stub)
