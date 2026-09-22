"""QMP disconnect behavior used by lifecycle cancellation."""

import os
import queue
import threading
from unittest.mock import MagicMock

import pytest

from vmocs.error import HypervisorError
from vmocs.monitor import QemuMonitor


def _disconnected_monitor():
    monitor = QemuMonitor.__new__(QemuMonitor)
    monitor._wlock = threading.Lock()
    monitor._closed = threading.Event()
    monitor._sync_cb = queue.Queue()
    monitor._async_cb = []
    monitor._sock = MagicMock()
    monitor._sock.sendall.side_effect = BrokenPipeError()
    monitor._stop_r, monitor._stop_w = os.pipe()
    return monitor


def test_qmp_send_failure_unblocks_synchronous_command():
    monitor = _disconnected_monitor()
    try:
        with pytest.raises(HypervisorError, match='disconnected'):
            monitor.powerdown()
    finally:
        os.close(monitor._stop_r)
        os.close(monitor._stop_w)


def test_qmp_command_after_disconnect_fails_immediately():
    monitor = _disconnected_monitor()
    try:
        monitor._closed.set()
        with pytest.raises(HypervisorError, match='disconnected'):
            monitor.quit()
    finally:
        os.close(monitor._stop_r)
        os.close(monitor._stop_w)
