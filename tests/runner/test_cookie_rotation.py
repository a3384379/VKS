"""A08: real isolated Chromium against two synthetic loopback services."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from html import escape
from threading import Thread
import pytest

from test_browser_capture import browser_path
from services.browser_runner.auth_registry import AuthMaterial
from services.browser_runner.network import origin
from services.browser_runner.runner import run_capture
from services.browser_runner.network import NetworkPolicy, NetworkDenied
from services.browser_runner.auth_registry import rotated_cookies


@contextmanager
def sites():
    facts = {"refresh": [], "private": [], "cross": []}
    csrf1, csrf2 = "synthetic-csrf-first&value", "synthetic-csrf-second&value"
    session1, session2 = "synthetic-session-first&value", "synthetic-session-second&value"
    class Other(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            facts["cross"].append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"cross-ok")
    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    other_url = f"http://127.0.0.1:{other.server_port}"
    class Main(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == "/private":
                facts["private"].append({k.lower(): v for k, v in self.headers.items()})
                ok = session2 in self.headers.get("Cookie", "") and self.headers.get("X-CSRF-Token") == csrf2
                self.send_response(200 if ok else 403)
                self.end_headers()
                self.wfile.write(("private-ok " + session2 + " " + csrf2 if ok else "private-denied").encode())
                return
            body = ("""<html><body><p id='result'>ready</p>
            <script>function csrf(){return document.cookie.split('; ').find(x=>x.startsWith('csrftoken=')).split('=')[1]}
            async function run(){
              await new Promise(resolve=>setTimeout(resolve,250));
              await fetch('/refresh',{method:'POST',headers:{'X-CSRF-Token':csrf()}});
              const result=await fetch('/private',{headers:{'X-CSRF-Token':csrf()}}).then(r=>r.text());
              const cross=await fetch('""" + other_url + """/cross').then(r=>r.text()).catch(()=> 'cross-failed');
              document.querySelector('#result').textContent=result+' '+cross;
            }run();</script></body></html>""").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            facts["refresh"].append({k.lower(): v for k, v in self.headers.items()})
            ok = session1 in self.headers.get("Cookie", "") and self.headers.get("X-CSRF-Token") == csrf1
            self.send_response(200 if ok else 403)
            if ok:
                self.send_header("Set-Cookie", f"session={session2}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Set-Cookie", f"csrftoken={csrf2}; Path=/; SameSite=Lax")
                self.send_header("Set-Cookie", "escape=synthetic-escape; Domain=unrelated.invalid; Path=/")
                self.send_header("Set-Cookie", "secureOnly=synthetic-secure; Path=/; Secure")
            self.end_headers()
    main = ThreadingHTTPServer(("127.0.0.1", 0), Main)
    workers = [Thread(target=s.serve_forever, daemon=True) for s in (main, other)]
    for worker in workers: worker.start()
    try:
        yield f"http://127.0.0.1:{main.server_port}", other_url, facts, (csrf1, csrf2, session1, session2)
    finally:
        for server in (main, other): server.shutdown(); server.server_close()
        for worker in workers: worker.join(timeout=2)


def test_a08_csrf_cookie_rotation_multiple_cookies_and_cross_origin_isolation():
    with sites() as (base, other, facts, secrets):
        csrf1, csrf2, session1, session2 = secrets
        auth = AuthMaterial("synthetic", "1", origin(base), "binding", {"Authorization": "Bearer synthetic-auth"},
            {"cookies": [{"name": name, "value": value, "domain": "127.0.0.1", "path": "/",
                          "httpOnly": name == "session", "secure": False, "sameSite": "Lax"}
                         for name, value in (("session", session1), ("csrftoken", csrf1))], "origins": []},
            [csrf1, session1, "synthetic-auth"])
        dsl = {"schema_version": "1.0", "scenario_key": "rotation", "name": "Synthetic rotation",
            "environment_ref": "mock", "dataset_ref": "synthetic", "risk_level": "test_write",
            "approval_ref": "11111111-1111-4111-8111-111111111111", "timeout_ms": 15000,
            # The approved navigation owns the async refresh until networkidle;
            # subsequent assertions stay read-only, as required by the DSL.
            "steps": [{"step_key": "open", "kind": "goto", "path": "/", "wait_until": "networkidle", "risk": "test_write", "timeout_ms": 3000},
                {"step_key": "assert", "kind": "assert_text", "target": {"by": "css", "value": "#result"},
                 "expected": {"literal": "private-ok"}, "match": "contains", "risk": "read_only", "timeout_ms": 5000},
                {"step_key": "snapshot", "kind": "snapshot", "name": "Safe result", "risk": "read_only", "timeout_ms": 3000}]}
        output = run_capture(dsl, {"kind": "mock", "base_origin": base, "allowed_origins": [base, other],
            "allowed_cidrs": ["127.0.0.1/32"], "allow_loopback": True, "allowed_write_paths": ["/refresh"],
            "allowed_request_headers": {base: ["X-CSRF-Token"]}}, {"synthetic": True, "data": {}},
            auth=auth, executable_path=browser_path())
        assert facts["refresh"][0]["x-csrf-token"] == csrf1
        assert facts["private"][0]["x-csrf-token"] == csrf2
        assert session2 in facts["private"][0]["cookie"]
        assert "escape=" not in facts["private"][0]["cookie"] and "secureOnly=" not in facts["private"][0]["cookie"]
        assert facts["cross"] and all(not {"authorization", "cookie", "x-csrf-token"} & set(h) for h in facts["cross"])
        dom = output.snapshots[0]["dom"].decode()
        assert "cross-ok" in dom
        for value in secrets:
            assert value not in dom and value not in json.dumps(output.requests)
            assert escape(value) not in dom


@pytest.mark.parametrize("policy", [
    {"http://allowed.invalid": ["Host"]},
    {"http://allowed.invalid": ["Authorization"]},
    {"http://other.invalid": ["X-CSRF-Token"]},
])
def test_a08_request_header_policy_rejects_transport_auth_and_other_origin(policy):
    with pytest.raises(NetworkDenied):
        NetworkPolicy("http://allowed.invalid", ["http://allowed.invalid"], ["127.0.0.1/32"], request_headers=policy)


def test_a08_cookie_scope_path_expiration_and_secure_validation():
    cookies, secrets = rotated_cookies([
        "scoped=synthetic-path; Path=/private; HttpOnly; SameSite=Strict",
        "expired=synthetic-expired; Path=/; Max-Age=0",
        "outside=synthetic-outside; Domain=other.invalid; Path=/",
        "badpath=synthetic-invalid; Path=private",
        "httpsOnly=synthetic-secure; Secure; Path=/",
    ], "http://allowed.invalid/refresh")
    assert {c["name"] for c in cookies} == {"scoped", "expired"}
    assert cookies[0]["path"] == "/private" and cookies[0]["httpOnly"]
    assert cookies[1]["expires"] <= time.time()
    assert "synthetic-outside" in secrets
    secure, _ = rotated_cookies(["session=synthetic-secure; Secure; Path=/; SameSite=None"], "https://allowed.invalid/")
    assert secure[0]["secure"] and secure[0]["sameSite"] == "None"
