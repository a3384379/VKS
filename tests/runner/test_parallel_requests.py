from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from services.browser_runner.auth_registry import resolve_auth
from services.browser_runner.runner import run_capture
from test_auth_registry import registry
from test_browser_capture import browser_path


@contextmanager
def parallel_site():
    class Assets(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/":
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"""<html><body><p id="done">Loading</p><script>
                addEventListener('DOMContentLoaded',()=>Promise.all(Array.from({length:24},(_,i)=>fetch('/asset/'+i))).then(()=>document.querySelector('#done').textContent='Complete'));
                </script></body></html>""")
            else:
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"fixture")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Assets)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_actual_parallel_auth_asset_routes_keep_unique_ids_and_entry_step_snapshots(tmp_path, monkeypatch):
    with parallel_site() as base:
        registry(tmp_path, monkeypatch, base)
        authentication = resolve_auth("fixture-reader", base, "k" * 40)
        dsl = {
            "schema_version": "1.0",
            "scenario_key": "parallel.read",
            "name": "并发资源",
            "environment_ref": "mock",
            "dataset_ref": "synthetic",
            "risk_level": "read_only",
            "timeout_ms": 15000,
            "steps": [
                {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 5000, "risk": "read_only"},
                {
                    "step_key": "wait",
                    "kind": "assert_text",
                    "target": {"by": "css", "value": "#done"},
                    "expected": {"literal": "Complete"},
                    "timeout_ms": 5000,
                    "risk": "read_only",
                },
            ],
        }
        environment = {
            "base_origin": base,
            "allowed_origins": [base],
            "allowed_cidrs": ["127.0.0.1/32"],
            "allow_loopback": True,
            "allowed_write_paths": [],
            "kind": "mock",
            "auth_ref": "fixture-reader",
        }
        output = run_capture(
            dsl, environment, {"synthetic": True, "data": {}}, auth=authentication, executable_path=browser_path()
        )
        assets = [row for row in output.requests if row["path"].startswith("/asset/")]
        assert len(assets) == 24
        ids = [row["request_id"] for row in output.requests + output.blocked]
        assert len(ids) == len(set(ids))
        for request in output.requests:
            eligible = [
                step["step_key"]
                for step in output.steps
                if step["started_ms"] - 5 <= request["started_ms"] <= step["started_ms"] + step["duration_ms"] + 5
            ]
            assert request["observed_step_key"] in eligible
            assert request["step_key"] == request["observed_step_key"]
