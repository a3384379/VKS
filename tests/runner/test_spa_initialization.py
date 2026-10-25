from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
import pytest
from services.browser_runner.runner import CaptureError, run_capture
from services.browser_runner.auth_registry import resolve_auth
from test_auth_registry import registry
from test_browser_capture import browser_path


@contextmanager
def asynchronous_auth_site():
    counts = {"refresh": 0, "other_write": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<html><body><p id="status">Loading</p><script>
            document.addEventListener('DOMContentLoaded',()=>setTimeout(async()=>{
              try {const response=await fetch('/auth/refresh',{method:'POST'});
                if(response.ok)document.querySelector('#status').textContent='Authenticated';
              }catch(e){}
              fetch('/other-write',{method:'POST'}).catch(()=>{});
            },80));</script></body></html>""")

        def do_POST(self):
            if self.path == "/auth/refresh":
                counts["refresh"] += 1
            else:
                counts["other_write"] += 1
            time.sleep(0.05)
            if self.headers.get("Origin") != base or not self.headers.get("Cookie", "").startswith(
                "session=fixture-cookie-"
            ):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("wait_until", [None, "networkidle"])
def test_authentication_spa_keeps_only_explicit_initialization_window_authorized(wait_until, tmp_path, monkeypatch):
    with asynchronous_auth_site() as (base, counts):
        registry(tmp_path, monkeypatch, base)
        material = resolve_auth("fixture-reader", base, "k" * 40)
        goto = {"step_key": "initialize", "kind": "goto", "path": "/", "timeout_ms": 3000, "risk": "test_write"}
        if wait_until:
            goto["wait_until"] = wait_until
        dsl = {
            "schema_version": "1.0",
            "scenario_key": "spa.fixture",
            "name": "受控认证初始化",
            "environment_ref": "mock",
            "dataset_ref": "synthetic",
            "risk_level": "test_write",
            "approval_ref": "11111111-1111-4111-8111-111111111111",
            "timeout_ms": 8000,
            "steps": [
                goto,
                {
                    "step_key": "read_assert",
                    "kind": "assert_text",
                    "target": {"by": "css", "value": "#status"},
                    "expected": {"literal": "Authenticated"},
                    "timeout_ms": 1000,
                    "risk": "read_only",
                },
                {
                    "step_key": "snapshot",
                    "kind": "snapshot",
                    "name": "初始化完成",
                    "timeout_ms": 1000,
                    "risk": "read_only",
                },
            ],
        }
        environment = {
            "base_origin": base,
            "allowed_origins": [base],
            "allowed_cidrs": ["127.0.0.1/32"],
            "allow_loopback": True,
            "allowed_write_paths": ["/auth/refresh"],
            "kind": "mock",
        }
        if wait_until is None:
            with pytest.raises(CaptureError):
                run_capture(
                    dsl, environment, {"synthetic": True, "data": {}}, executable_path=browser_path(), auth=material
                )
            assert counts == {"refresh": 0, "other_write": 0}
        else:
            output = run_capture(
                dsl, environment, {"synthetic": True, "data": {}}, executable_path=browser_path(), auth=material
            )
            assert counts == {"refresh": 1, "other_write": 0}
            assert len(output.snapshots) == 1 and b"Authenticated" in output.snapshots[0]["dom"]
            assert output.blocked
