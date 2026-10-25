import socket

import pytest

from services.browser_runner.network import NetworkDenied, NetworkPolicy
from services.browser_runner.runner import redact_dom


def test_network_boundary_rejects_origin_metadata_control_ports_and_rebinding(monkeypatch):
    policy = NetworkPolicy("http://safe.test:8081", ["http://safe.test:8081"], ["10.0.0.0/8"])
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 8081))]
    )
    assert policy.resolve("http://safe.test:8081/path")[1] == "10.1.2.3"
    for url in (
        "http://outside.test:8081",
        "http://safe.test:8000",
        "file:///etc/passwd",
        "http://user:pass@safe.test:8081",
        "http://169.254.169.254/latest",
    ):
        with pytest.raises(NetworkDenied):
            policy.resolve(url)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 8081))]
    )
    with pytest.raises(NetworkDenied):
        policy.resolve("http://safe.test:8081/path")


def test_dom_redaction_removes_secret_inputs_scripts_and_pii_before_storage():
    html = """<html><body><script>password=source-secret</script><input value="input-secret"><textarea>raw-secret</textarea><p>客户电话13800138000</p><p>access_token=raw-token-value</p><img src="http://outside.test"><div data-sensitive>account-secret</div><button data-vks-action="submit">确认</button></body></html>"""
    result = redact_dom(html)
    for secret in (
        "source-secret",
        "input-secret",
        "raw-secret",
        "13800138000",
        "raw-token-value",
        "outside.test",
        "account-secret",
    ):
        assert secret not in result
    assert 'data-vks-action="submit"' in result and "[REDACTED]" in result


def test_fetch_enforces_request_body_and_total_slow_response_deadline():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import time

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    policy = NetworkPolicy(base, [base], ["127.0.0.1/32"], allow_loopback=True)
    try:
        with pytest.raises(NetworkDenied):
            policy.fetch(base, body=b"x" * (1024 * 1024 + 1))
        started = time.monotonic()
        with pytest.raises((NetworkDenied, OSError)):
            policy.fetch(base, timeout=0.15)
        assert time.monotonic() - started < 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_only_native_registered_origin_is_forwarded_for_cookie_csrf_checks():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    observed = []

    class CookieCsrf(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append(self.headers.get("Origin"))
            valid = self.headers.get("Origin") == base and self.headers.get("Cookie") == "session=fixture-session"
            self.send_response(200 if valid else 403)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CookieCsrf)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = NetworkPolicy(base, [base], ["127.0.0.1/32"], allow_loopback=True, write_paths=["/auth/refresh"])
    try:
        for provided, expected in (
            (base, 200),
            (None, 403),
            ("http://unregistered.invalid", 403),
            (base + "/path", 403),
            ("null", 403),
        ):
            status, _, _ = policy.fetch(
                base + "/auth/refresh",
                "POST",
                allow_write=True,
                headers={"Origin": provided} if provided else {},
                trusted_headers={"Cookie": "session=fixture-session"},
            )
            assert status == expected
        assert observed == [base, None, None, None, None]
        with pytest.raises(NetworkDenied):
            policy.fetch(base + "/other", "POST", allow_write=True, headers={"Origin": base})
        assert len(observed) == 5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
