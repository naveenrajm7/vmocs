#  Copyright (C) 2014-2015 CEA/DAM/DIF
#  Copyright (C) 2026 Naveenraj Muthuraj
#
#  Based on pcocc QemuMonitor (Hypervisor.py:749-1086) by CEA/DAM/DIF
#  Key changes: accepts socket_path directly (not a vm object),
#  drops migration/checkpoint/agent/serial code, simplified threading.
#  SPDX-License-Identifier: GPL-3.0-or-later

import codecs
import json
import logging
import os
import queue
import select
import socket
import threading
import time

from .error import HypervisorError

_QMP_READ_SIZE = 32768


class QemuMonitor:
    """QMP protocol handler over a Unix socket.

    Adapted from pcocc QemuMonitor (Hypervisor.py:749-1086).
    """

    def __init__(self, socket_path):
        self._socket_path = socket_path

        self._wlock = threading.Lock()
        self._current_tag = 1

        # Pipe used to wake the reader thread on shutdown
        self._stop_r, self._stop_w = os.pipe()

        self._sync_cb = queue.Queue()
        self._async_cb = []
        self._databuff = ''

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(socket_path)
        except OSError as e:
            raise HypervisorError(f'cannot connect to QMP socket {socket_path}: {e}')

        # Prime the callback queue with the hello handler before the reader starts
        self._sync_cb.put(self._handle_hello)

        self._reader = threading.Thread(target=self._reader_thread, daemon=True)
        self._reader.start()

        self._negotiate_caps()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cont(self):
        self._validate('{"execute": "cont"}\n\n')

    def stop(self):
        self._validate('{"execute": "stop"}\n\n')

    def quit(self):
        self._validate('{"execute": "quit"}\n\n')

    def query_status(self):
        """Return the VM status string (e.g. 'running', 'paused')."""
        data = self._exec_sync('{"execute": "query-status"}\n\n')
        self._check_error(data)
        try:
            return data['return']['status']
        except (KeyError, TypeError) as e:
            raise HypervisorError(f'unexpected query-status reply: {data}')

    def query_cpus(self):
        """Return list of vCPU info dicts (cpu-index, thread-id, ...)."""
        data = self._exec_sync('{"execute": "query-cpus-fast"}\n\n')
        self._check_error(data)
        return data['return']

    def close(self):
        """Signal the reader thread to exit."""
        try:
            os.write(self._stop_w, b'x')
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_hello(self, data):
        if 'QMP' not in data:
            raise HypervisorError(f'unexpected first QMP message: {data}')
        logging.debug('QMP: connected to QEMU monitor')

    def _negotiate_caps(self):
        self._validate('{"execute": "qmp_capabilities", "arguments":{}}\n\n')

    def _validate(self, cmd):
        self._check_error(self._exec_sync(cmd))

    def _check_error(self, data):
        if data is None:
            raise HypervisorError('QMP socket disconnected')
        if 'error' in data:
            desc = data['error'].get('desc', str(data['error']))
            raise HypervisorError(f'QMP error: {desc}')
        return data

    def _exec_sync(self, json_cmd):
        """Send a QMP command and block until a reply arrives."""
        result_q = queue.Queue()
        self._exec_async(json_cmd, result_q.put)
        return result_q.get()

    def _exec_async(self, json_cmd, callback):
        with self._wlock:
            self._sync_cb.put(callback)
            try:
                self._sock.sendall(json_cmd.encode('utf-8'))
            except OSError as e:
                logging.warning('QMP send failed: %s', e)

    def _reader_thread(self):
        """Read QMP messages from the socket until stopped."""
        dec = codecs.getincrementaldecoder('utf8')()
        while True:
            try:
                rdr, _, _ = select.select([self._stop_r, self._sock], [], [])
            except OSError:
                break
            if self._stop_r in rdr:
                break
            if self._sock in rdr:
                try:
                    raw = self._sock.recv(_QMP_READ_SIZE)
                except OSError:
                    raw = None
                if not raw:
                    self._drain_callbacks(None)
                    break
                self._databuff += dec.decode(raw)
                self._dispatch_messages()

    def _dispatch_messages(self):
        while '\n' in self._databuff:
            line, _, self._databuff = self._databuff.partition('\n')
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                logging.error('QMP: cannot decode message: %s', e)
                continue
            if 'event' in msg:
                # Async events — fire registered callbacks
                self._async_cb = [cb for cb in self._async_cb if cb(msg)]
            else:
                try:
                    cb = self._sync_cb.get_nowait()
                except queue.Empty:
                    logging.error('QMP: unexpected reply: %s', msg)
                    continue
                cb(msg)

    def _drain_callbacks(self, value):
        for cb in self._async_cb:
            cb(value)
        self._async_cb = []
        while True:
            try:
                cb = self._sync_cb.get_nowait()
                cb(value)
            except queue.Empty:
                break


def wait_for_monitor(socket_path, timeout=60):
    """Poll until the QMP socket appears and QEMU responds, then return a QemuMonitor."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            try:
                return QemuMonitor(socket_path)
            except HypervisorError:
                pass
        time.sleep(0.5)
    raise HypervisorError(
        f'timed out waiting for QMP socket {socket_path} after {timeout}s')
