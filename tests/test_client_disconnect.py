"""Modern tool calls poll the client socket: it must work on fds above select()'s limit."""

import fcntl
import resource
import socket
import unittest

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

from lib.mcp_http_2026 import ModernHTTPMixin


class _Handler:
    _client_disconnected = ModernHTTPMixin._client_disconnected

    def __init__(self, connection):
        self.connection = connection


def _high_fd_socket(sock, floor=2000):
    fd = fcntl.fcntl(sock.fileno(), fcntl.F_DUPFD, floor)
    return socket.socket(fileno=fd)


class ClientDisconnectedTests(unittest.TestCase):
    def setUp(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
        self.server_side, self.client_side = socket.socketpair()
        self.addCleanup(self.server_side.close)
        self.addCleanup(self.client_side.close)

    def test_open_idle_connection_is_not_disconnected(self):
        self.assertFalse(_Handler(self.server_side)._client_disconnected())

    def test_closed_peer_is_disconnected(self):
        self.client_side.close()
        self.assertTrue(_Handler(self.server_side)._client_disconnected())

    def test_pending_data_is_not_a_disconnect(self):
        self.client_side.sendall(b"x")
        self.assertFalse(_Handler(self.server_side)._client_disconnected())

    def test_fd_above_select_limit_does_not_raise(self):
        high = _high_fd_socket(self.server_side)
        self.addCleanup(high.close)
        self.assertGreater(high.fileno(), 1024)
        self.assertFalse(_Handler(high)._client_disconnected())

    def test_closed_socket_counts_as_disconnected(self):
        self.server_side.close()
        self.assertTrue(_Handler(self.server_side)._client_disconnected())


if __name__ == "__main__":
    unittest.main()
