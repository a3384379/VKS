"""Small idle connection pool; every checkout follows fresh network validation."""

import atexit
import http.client
import os
import select
import ssl
import threading
import time
from functools import lru_cache


@lru_cache(maxsize=1)
def tls_context():
    return ssl.create_default_context()


class PinnedTLSConnection(http.client.HTTPConnection):
    def __init__(self, ip, port, hostname, timeout):
        super().__init__(ip, port, timeout=timeout)
        self.server_hostname = hostname

    def connect(self):
        # HTTPConnection may reconnect after a closed socket. Every such
        # connection must still use TLS with the original validated SNI.
        super().connect()
        try:
            self.sock = tls_context().wrap_socket(self.sock, server_hostname=self.server_hostname)
        except BaseException:
            self.close()
            raise


class ConnectionPool:
    def __init__(self, limit=128, idle_seconds=20):
        self.limit, self.idle_seconds = limit, idle_seconds
        self.lock = threading.Lock()
        self.idle = []
        self.pid = os.getpid()

    def close(self):
        with self.lock:
            idle, self.idle = self.idle, []
        for _, _, connection in idle:
            connection.close()

    def take(self, key, create, timeout):
        expired, found = [], None
        with self.lock:
            now = time.monotonic()
            current_pid = os.getpid()
            kept = []
            for stamp, identity, connection in self.idle:
                if self.pid != current_pid or now - stamp >= self.idle_seconds:
                    expired.append(connection)
                elif identity == key and found is None:
                    found = connection
                else:
                    kept.append((stamp, identity, connection))
            self.pid, self.idle = current_pid, kept
        for connection in expired:
            connection.close()
        if found is not None:
            try:
                # EOF/unsolicited bytes indicate an unusable idle connection.
                # Drop it BEFORE submitting; never replay a possibly sent POST.
                if found.sock is None or select.select([found.sock], [], [], 0)[0]:
                    found.close()
                else:
                    found.timeout = timeout
                    found.sock.settimeout(timeout)
                    return found
            except (OSError, ValueError):
                found.close()
        return create()

    def put(self, key, connection):
        evicted = None
        with self.lock:
            if len(self.idle) >= self.limit:
                evicted = self.idle.pop(0)[2]
            self.idle.append((time.monotonic(), key, connection))
        if evicted:
            evicted.close()


MODEL_CONNECTIONS = ConnectionPool()
atexit.register(MODEL_CONNECTIONS.close)
