"""Real HTTP keep-alive, current pin checks and uncertain failure accounting."""

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from vks_api import ai_gateway, model_credentials
from vks_api.model_transport import MODEL_CONNECTIONS, ConnectionPool
from vks_api.service import DomainError

from services.browser_runner.network import NetworkDenied


@pytest.fixture
def provider(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.client_address, self.headers.get("Authorization"), body))
            if body.get("drop"):
                self.close_connection = True
                return
            response = b'{"ok":true}'
            self.send_response(429 if body.get("limit") else 200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    monkeypatch.setattr(model_credentials, "key_for_ref", lambda *args: "test-only-key-a")
    MODEL_CONNECTIONS.close()
    try:
        yield {"endpoint": f"http://127.0.0.1:{server.server_port}/v1", "protocol": "responses"}, seen
    finally:
        MODEL_CONNECTIONS.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reuses_connection_but_rotation_and_current_network_policy_take_effect(provider, monkeypatch):
    config, seen = provider
    assert ai_gateway.request_model(config, {"id": 1}) == {"ok": True}
    assert ai_gateway.request_model(config, {"id": 2}) == {"ok": True}
    assert seen[0][0] == seen[1][0]  # actual TCP peer, no fake transport
    monkeypatch.setattr(model_credentials, "key_for_ref", lambda *args: "test-only-key-b")
    ai_gateway.request_model(config, {"id": 3})
    assert seen[2][0] != seen[1][0]
    assert seen[2][1] == "Bearer test-only-key-b"
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "0")
    with pytest.raises(ai_gateway.ModelNotSent):
        ai_gateway.request_model(config, {"id": 4})
    assert len(seen) == 3


def test_revalidates_dns_even_with_idle_connection_and_never_replays_uncertain_post(provider, monkeypatch):
    from services.browser_runner.network import NetworkPolicy

    config, seen = provider
    ai_gateway.request_model(config, {"id": 1})
    with monkeypatch.context() as patch:

        def denied(*args):
            raise NetworkDenied("changed DNS")

        patch.setattr(NetworkPolicy, "resolve", denied)
        with pytest.raises(ai_gateway.ModelNotSent):
            ai_gateway.request_model(config, {"id": 2})
    with pytest.raises(DomainError) as error:
        ai_gateway.request_model(config, {"drop": True})
    assert not isinstance(error.value, ai_gateway.ModelNotSent)
    assert len(seen) == 2  # no automatic retry after uncertain delivery
    ai_gateway.request_model(config, {"id": 3})
    assert len(seen) == 3
    assert seen[2][0] != seen[0][0]


def test_error_response_discards_connection(provider):
    config, seen = provider
    with pytest.raises(DomainError):
        ai_gateway.request_model(config, {"limit": True})
    ai_gateway.request_model(config, {"id": 2})
    assert seen[0][0] != seen[1][0]


def test_idle_pool_evicts_and_expires(monkeypatch):
    pool = ConnectionPool(limit=1, idle_seconds=1)
    closed = []

    class Connection:
        sock = None

        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    pool.put("a", Connection("a"))
    pool.put("b", Connection("b"))
    assert closed == ["a"]
    monkeypatch.setattr("vks_api.model_transport.time.monotonic", lambda: float("inf"))
    assert pool.take("b", lambda: "fresh", 30) == "fresh"
    assert closed == ["a", "b"]


@pytest.mark.parametrize("kind", ["timeout", "invalid_json", "connection_closed"])
def test_transport_failures_are_distinct_and_never_auto_replayed(provider, monkeypatch, kind):
    config, seen = provider
    requests, closed = [], []

    class Response:
        status = 200
        will_close = True

        def read(self, limit):
            if kind == "timeout":
                raise TimeoutError("synthetic timeout")
            return b"invalid json"

    class Connection:
        sock = None

        def request(self, *args, **kwargs):
            requests.append(True)

        def getresponse(self):
            if kind == "connection_closed":
                raise http.client.RemoteDisconnected("synthetic closed connection")
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(MODEL_CONNECTIONS, "take", lambda *args: Connection())
    with pytest.raises(DomainError) as error:
        ai_gateway.request_model(config, {})
    assert error.value.details["failure_class"] == kind
    assert error.value.retryable and not isinstance(error.value, ai_gateway.ModelNotSent)
    assert len(requests) == len(closed) == 1 and not seen
