"""Tests for VM launch readiness helpers."""

from vmocs.launch import wait_for_ssh


class _BannerSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, _size):
        return b'SSH-2.0-OpenSSH_9.6\r\n'


def test_authenticated_ssh_readiness_retries_login(monkeypatch):
    statuses = iter([255, 0])
    calls = []
    monkeypatch.setattr(
        'vmocs.launch.socket.create_connection',
        lambda *args, **kwargs: _BannerSocket())
    monkeypatch.setattr(
        'vmocs.launch.subprocess.call',
        lambda argv, **kwargs: calls.append(argv) or next(statuses))
    monkeypatch.setattr('vmocs.launch.time.sleep', lambda _seconds: None)

    assert wait_for_ssh(
        '127.0.0.1', 60222, '/tmp/key', 1,
        ssh_user='cluster-user', authenticate=True)
    assert len(calls) == 2
    assert '-i' in calls[0]
    assert 'cluster-user@127.0.0.1' in calls[0]
