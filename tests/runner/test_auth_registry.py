from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from io import BytesIO
import json
import secrets
import hashlib
import pytest
from PIL import Image
from pydantic import ValidationError
from services.browser_runner.auth_registry import AuthRegistryError, resolve_auth
from services.browser_runner.runner import run_capture
from vks_api.scenarios import EnvironmentInput, ScenarioService
from vks_api.service import Service
from test_browser_capture import browser_path


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(384)


def registry(tmp_path, monkeypatch, base):
    token = "fixture-bearer-" + secrets.token_hex(16)
    cookie = "fixture-cookie-" + secrets.token_hex(16)
    storage = "fixture-storage-" + secrets.token_hex(16)
    monkeypatch.setenv("VKS_RUNNER_AUTH_FIXTURE", "Bearer " + token)
    state = tmp_path / "state.json"
    private_json(
        state,
        {
            "cookies": [
                {
                    "name": "session",
                    "value": cookie,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": False,
                    "sameSite": "Lax",
                }
            ],
            "origins": [{"origin": base, "localStorage": [{"name": "readerSession", "value": storage}]}],
        },
    )
    path = tmp_path / "registry.json"
    value = {
        "schema_version": "1",
        "entries": {
            "fixture-reader": {
                "origin": base,
                "revision": "v1",
                "test_only": True,
                "headers_env": {"Authorization": "VKS_RUNNER_AUTH_FIXTURE"},
                "storage_state_path": str(state),
            }
        },
    }
    private_json(path, value)
    monkeypatch.setenv("VKS_RUNNER_SECRET_REGISTRY", str(path))
    return (path, value, token, cookie, storage)


@contextmanager
def authenticated_site():
    facts = {"primary": [], "secondary": []}

    class Secondary(BaseHTTPRequestHandler):
        def do_GET(self):
            facts["secondary"].append((self.headers.get("Authorization"), self.headers.get("Cookie")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    other = ThreadingHTTPServer(("127.0.0.1", 0), Secondary)
    second = f"http://127.0.0.1:{other.server_port}"

    class Primary(BaseHTTPRequestHandler):
        def do_GET(self):
            facts["primary"].append((self.headers.get("Authorization"), self.headers.get("Cookie")))
            bearer = self.headers.get("Authorization", "")
            cookie = self.headers.get("Cookie", "")
            if not bearer.startswith("Bearer fixture-bearer-") or not cookie.startswith("session=fixture-cookie-"):
                self.send_response(401)
                self.end_headers()
                return
            html = f'''<html><body><h1 id="ok">Authenticated fixture</h1><p>{bearer[7:]}</p><p>{cookie.split("=", 1)[1]}</p><p id="stored"></p><img src="{second}/asset"><script>document.querySelector("#stored").textContent=localStorage.getItem("readerSession");fetch("{second}/ping")</script></body></html>'''.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Primary)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (server, other)]
    for thread in threads:
        thread.start()
    try:
        yield (f"http://127.0.0.1:{server.server_port}", second, facts)
    finally:
        for s in (server, other):
            s.shutdown()
            s.server_close()
        for thread in threads:
            thread.join(timeout=2)


def definition():
    return {
        "schema_version": "1.0",
        "scenario_key": "auth.read",
        "name": "受控认证只读采集",
        "role": "合成阅读者",
        "expected_results": ["只显示已授权测试内容"],
        "environment_ref": "authorized",
        "dataset_ref": "synthetic",
        "risk_level": "read_only",
        "timeout_ms": 10000,
        "steps": [
            {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 3000, "risk": "read_only"},
            {
                "step_key": "assert",
                "kind": "assert_text",
                "target": {"by": "css", "value": "#ok"},
                "expected": {"literal": "Authenticated fixture"},
                "timeout_ms": 2000,
                "risk": "read_only",
            },
            {
                "step_key": "snapshot",
                "kind": "snapshot",
                "name": "认证后状态",
                "full_page": False,
                "timeout_ms": 2000,
                "risk": "read_only",
            },
        ],
    }


def test_actual_browser_uses_origin_scoped_auth_isolated_state_viewport_and_exact_secret_redaction(
    tmp_path, monkeypatch
):
    with authenticated_site() as (base, secondary, facts):
        _, _, token, cookie, storage = registry(tmp_path, monkeypatch, base)
        material = resolve_auth("fixture-reader", base, secrets.token_urlsafe(40))
        environment = {
            "base_origin": base,
            "allowed_origins": [base, secondary],
            "allowed_cidrs": ["127.0.0.1/32"],
            "allow_loopback": True,
            "allowed_write_paths": [],
            "kind": "mock",
            "auth_ref": "fixture-reader",
            "viewport": {"width": 800, "height": 600},
        }
        result = run_capture(
            definition(), environment, {"synthetic": True, "data": {}}, auth=material, executable_path=browser_path()
        )
        assert facts["primary"] and facts["secondary"]
        assert all((header == "Bearer " + token and "session=" + cookie in value for header, value in facts["primary"]))
        assert all((header is None and cookie_header is None for header, cookie_header in facts["secondary"]))
        serialized = json.dumps(result.requests) + json.dumps(result.steps)
        for secret in (token, cookie, storage):
            assert secret.encode() in result.snapshots[0]["dom"] and secret not in serialized
        image = Image.open(BytesIO(result.snapshots[0]["png"]))
        assert image.size == (800, 600)
        assert sum((pixel[:3] == (203, 213, 225) for pixel in image.get_flattened_data())) == 0
        assert result.snapshots[0]["viewport"] == {"width": 800, "height": 600}


@pytest.mark.parametrize("rotate", [False, "before", "during"])
def test_auth_refs_are_private_version_bound_and_rotation_never_runs_old_approval(tmp_path, monkeypatch, rotate):
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    with authenticated_site() as (base, _, facts):
        registry_path, configured, token, cookie, storage = registry(tmp_path, monkeypatch, base)
        service = Service(tmp_path / "instance")
        _, owner = service.login(service.bootstrap_token)
        p = service.create_project(owner, {"key": "authorized", "name": "认证版本"}, "project")
        a = service.create_analysis(owner, p["id"], {"name": "draft", "sources": []}, "analysis")
        scenarios = ScenarioService(service)
        environment = scenarios.create_environment(
            owner,
            p["id"],
            {
                "key": "authorized",
                "name": "已登记测试环境",
                "kind": "test",
                "base_origin": base,
                "allowed_origins": [base],
                "allowed_cidrs": ["127.0.0.1/32"],
                "allow_loopback": True,
                "auth_ref": "fixture-reader",
                "viewport": {"width": 800, "height": 600},
            },
        )
        safe = json.dumps(environment)
        assert environment["policy"]["auth_revision"] == "v1" and "auth_binding_hmac" not in safe
        for secret in (token, cookie, storage):
            assert secret not in safe
        scenarios.create_dataset(owner, p["id"], {"key": "synthetic", "name": "合成", "synthetic": True, "data": {}})
        scenario = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": definition()}, "scenario")
        approval = {
            "analysis_set_id": a["id"],
            "scenario_revision_id": scenario["scenario_revision_id"],
            "reason": "授权独立合成会话",
        }
        job = scenarios.request_capture(
            owner,
            scenario["scenario_id"],
            {
                "scenario_revision_id": scenario["scenario_revision_id"],
                "environment_ref": "authorized",
                "analysis_set_id": approval["analysis_set_id"],
            },
            "capture",
        )["job"]
        if rotate == "before":
            configured["entries"]["fixture-reader"]["revision"] = "v2"
            private_json(registry_path, configured)
        if rotate == "during":
            import vks_api.scenarios as scenario_module

            runner = scenario_module.run_capture

            def rotating_runner(*args, **kwargs):
                callback = kwargs["cancelled"]

                def changed():
                    if facts["primary"]:
                        configured["entries"]["fixture-reader"]["revision"] = "v2"
                        private_json(registry_path, configured)
                    return callback()

                kwargs["cancelled"] = changed
                return runner(*args, **kwargs)

            monkeypatch.setattr(scenario_module, "run_capture", rotating_runner)
        monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", browser_path())
        service.process_job(job["id"])
        result = service.get_job(owner, job["id"])
        if rotate:
            assert result["state"] == "failed" and result["error"]["code"] == "VKS_STATE_CONFLICT"
            assert bool(facts["primary"]) is (rotate == "during") and (not result.get("result"))
        else:
            assert result["state"] == "succeeded", result
            capture = scenarios.captures(owner, p["id"])[0]
            assert capture["result"]["environment"]["auth_revision"] == "v1"
            assert capture["result"]["snapshots"][0]["viewport"] == {"width": 800, "height": 600}
            for artifact_id in capture["result"]["artifact_ids"]:
                raw, _ = service.artifact_bytes(owner, p["id"], artifact_id)
                if raw.startswith(b"<html"):
                    assert all(secret.encode() in raw for secret in (token, cookie, storage))
        one = resolve_auth("fixture-reader", base, "a" * 40)
        two = resolve_auth("fixture-reader", base, "b" * 40)
        assert one.binding_hmac != two.binding_hmac and one.binding_hmac != hashlib.sha256(token.encode()).hexdigest()
        service.engine.dispose()


def test_auth_registry_rejects_origin_escape_public_file_and_client_paths(tmp_path, monkeypatch):
    path, configured, _, _, _ = registry(tmp_path, monkeypatch, "http://127.0.0.1:19991")
    with pytest.raises(AuthRegistryError):
        resolve_auth("fixture-reader", "http://127.0.0.1:19992", "k" * 40)
    path.chmod(420)
    with pytest.raises(AuthRegistryError):
        resolve_auth("fixture-reader", "http://127.0.0.1:19991", "k" * 40)
    with pytest.raises(ValidationError):
        EnvironmentInput(
            key="test",
            name="x",
            kind="test",
            base_origin="http://example.invalid",
            allowed_origins=["http://example.invalid"],
            allowed_cidrs=["127.0.0.1/32"],
            storage_state_path="/private/user-cookie.json",
        )


def test_instance_auth_project_scope_is_checked_and_bound_to_rotation(tmp_path, monkeypatch):
    base = "http://127.0.0.1:19991"
    path, configured, _, _, _ = registry(tmp_path, monkeypatch, base)
    configured["entries"]["fixture-reader"]["project_keys"] = ["project-a"]
    private_json(path, configured)
    allowed = resolve_auth("fixture-reader", base, "k" * 40, project_key="project-a")
    assert allowed.allowed_project_keys == ["project-a"]
    with pytest.raises(AuthRegistryError):
        resolve_auth("fixture-reader", base, "k" * 40, project_key="project-b")
    configured["entries"]["fixture-reader"]["project_keys"].append("project-b")
    private_json(path, configured)
    changed = resolve_auth("fixture-reader", base, "k" * 40, project_key="project-a")
    assert changed.binding_hmac != allowed.binding_hmac
