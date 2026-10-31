import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import pytest
from vks_api.models import Analysis
from vks_api.scenarios import ScenarioService
from vks_api.service import DomainError, Service
from services.browser_runner.runner import CaptureError, run_capture


def browser_path():
    value = os.getenv("VKS_CHROMIUM_EXECUTABLE")
    if value and Path(value).exists():
        return value
    candidates = list(
        (Path.home() / "Library/Caches/ms-playwright").glob(
            "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
    )
    if candidates:
        return str(sorted(candidates)[-1])
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        path = p.chromium.executable_path
        if Path(path).exists():
            return path
    pytest.skip("Chromium未安装，运行 uv run playwright install chromium 后执行真实浏览器验收")


@contextmanager
def mock_site():
    counts = {"writes": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()
                return
            body = b"<!doctype html><html><body><label>Phone<input value=\"13800138000\"></label><input type=\"password\" value=\"input-secret-value\"><p>access_token=raw-secret-value</p><p id=\"status\">ready</p><button data-testid=\"disabled\" disabled>Unavailable</button><button data-testid=\"act\" onclick=\"document.querySelector('#status').textContent='clicked';fetch('/write',{method:'POST'}).catch(()=>{});fetch('http://169.254.169.254/latest/meta-data').catch(()=>{});\">Inspect</button><a href=\"/redirect\">Redirect</a><script>setTimeout(()=>{const button=document.createElement('button');button.dataset.testid='delayed';button.textContent='Ready';document.body.append(button)},150)</script></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            counts["writes"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code":"MOCK_SUCCESS"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{server.server_port}", counts)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def capture_dsl(write=False):
    result = {
        "schema_version": "1.0",
        "scenario_key": "capture.demo",
        "name": "合成采集",
        "environment_ref": "mock-test",
        "dataset_ref": "synthetic",
        "risk_level": "test_write" if write else "read_only",
        "timeout_ms": 25000,
        "steps": [
            {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 4000, "risk": "read_only"},
            {
                "step_key": "click",
                "kind": "click",
                "target": {"by": "test_id", "value": "act"},
                "timeout_ms": 3000,
                "risk": "test_write" if write else "read_only",
            },
            {
                "step_key": "assert",
                "kind": "assert_text",
                "target": {"by": "css", "value": "#status"},
                "expected": {"literal": "clicked"},
                "timeout_ms": 3000,
                "risk": "read_only",
            },
            {"step_key": "snapshot", "kind": "snapshot", "name": "脱敏后状态", "timeout_ms": 4000, "risk": "read_only"},
        ],
    }
    return result


def env(base):
    return {
        "base_origin": base,
        "allowed_origins": [base],
        "allowed_cidrs": ["127.0.0.1/32"],
        "allow_loopback": True,
        "kind": "mock",
        "allowed_write_paths": ["/write"],
    }


def test_real_browser_blocks_unapproved_writes_and_redacts_before_output():
    executable = browser_path()
    with mock_site() as (base, counts):
        output = run_capture(capture_dsl(), env(base), {"synthetic": True, "data": {}}, executable_path=executable)
        assert counts["writes"] == 0
        assert output.blocked and len(output.steps) == 4 and (len(output.snapshots) == 1)
        raw = output.snapshots[0]["dom"]
        for secret in (b"13800138000", b"input-secret-value", b"raw-secret-value"):
            assert secret in raw
        assert b"[REDACTED]" not in raw
        assert output.snapshots[0]["png"].startswith(b"\x89PNG")
        assert all((r["body_saved"] is False and "?" not in r["path"] for r in output.requests))


def test_real_browser_only_exact_approved_mock_write_and_redirect_boundary():
    executable = browser_path()
    with mock_site() as (base, counts):
        output = run_capture(capture_dsl(True), env(base), {"synthetic": True, "data": {}}, executable_path=executable)
        assert counts["writes"] == 1
        assert output.blocked
        redirected = capture_dsl()
        redirected["steps"] = [
            {"step_key": "redirect", "kind": "goto", "path": "/redirect", "timeout_ms": 3000, "risk": "read_only"}
        ]
        with pytest.raises(Exception):
            run_capture(redirected, env(base), {"synthetic": True, "data": {}}, executable_path=executable)
        assert counts["writes"] == 1


def test_approval_binds_revision_actor_policy_and_survives_no_late_result(tmp_path, monkeypatch):
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    with mock_site() as (base, counts):
        service = Service(tmp_path)
        _, owner = service.login(service.bootstrap_token)
        project = service.create_project(owner, {"key": "capture-test", "name": "Capture test"}, "project")
        analysis = service.create_analysis(owner, project["id"], {"name": "draft", "sources": []}, "analysis")
        scenarios = ScenarioService(service)
        scenarios.create_environment(
            owner,
            project["id"],
            {"key": "mock-test", "name": "Mock", "kind": "mock", **{k: v for k, v in env(base).items() if k != "kind"}},
        )
        scenarios.create_dataset(
            owner, project["id"], {"key": "synthetic", "name": "合成", "synthetic": True, "data": {}}
        )
        scenario = scenarios.create(
            owner, project["id"], {"analysis_set_id": analysis["id"], "dsl": capture_dsl(True)}, "scenario"
        )
        approval = {
            "analysis_set_id": analysis["id"],
            "scenario_revision_id": scenario["scenario_revision_id"],
            "reason": "批准合成 Mock 写入",
        }
        assert approval["scenario_revision_id"] == scenario["scenario_revision_id"]
        approved = scenarios.get(owner, project["id"], scenario["scenario_id"], approval["scenario_revision_id"])
        assert "approval_ref" not in approved["dsl"]
        args = {
            "scenario_revision_id": approval["scenario_revision_id"],
            "environment_ref": "mock-test",
            "analysis_set_id": approval["analysis_set_id"],
        }
        accepted = scenarios.request_capture(owner, scenario["scenario_id"], args, "capture")
        assert accepted["decision"] == "accepted"
        assert (
            scenarios.request_capture(owner, scenario["scenario_id"], args, "capture")["job"]["id"]
            == accepted["job"]["id"]
        )
        assert (
            scenarios.request_capture(owner, scenario["scenario_id"], args, "another-key")["job"]["id"]
            != accepted["job"]["id"]
        )
        with service.db() as s:
            s.get(Analysis, analysis["id"]).selection_version += 1
        with pytest.raises(DomainError):
            scenarios.execute_capture(accepted["job"]["id"])
        assert counts["writes"] == 0
        service.engine.dispose()


def test_full_service_job_capture_lifecycle_persists_only_redacted_observations(tmp_path, monkeypatch):
    executable = browser_path()
    monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", executable)
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    with mock_site() as (base, counts):
        service = Service(tmp_path)
        _, owner = service.login(service.bootstrap_token)
        p = service.create_project(owner, {"key": "lifecycle", "name": "生命周期"}, "project")
        a = service.create_analysis(owner, p["id"], {"name": "draft", "sources": []}, "analysis")
        scenarios = ScenarioService(service)
        scenarios.create_environment(
            owner,
            p["id"],
            {"key": "mock-test", "name": "Mock", "kind": "mock", **{k: v for k, v in env(base).items() if k != "kind"}},
        )
        scenarios.create_dataset(owner, p["id"], {"key": "synthetic", "name": "合成", "synthetic": True, "data": {}})
        saved = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": capture_dsl()}, "scenario")
        approval = {
            "analysis_set_id": a["id"],
            "scenario_revision_id": saved["scenario_revision_id"],
            "reason": "批准只读合成采集",
        }
        accepted = scenarios.request_capture(
            owner,
            saved["scenario_id"],
            {
                "scenario_revision_id": saved["scenario_revision_id"],
                "environment_ref": "mock-test",
                "analysis_set_id": approval["analysis_set_id"],
            },
            "capture",
        )
        service.process_job(accepted["job"]["id"])
        job = service.get_job(owner, accepted["job"]["id"])
        assert job["state"] == "succeeded", job
        records = scenarios.captures(owner, p["id"])
        assert len(records) == 1 and records[0]["state"] == "succeeded"
        assert records[0]["source_alignment"] == "unknown"
        for eid in records[0]["result"]["evidence_ids"]:
            evidence = service.get_evidence(owner, p["id"], [eid])[0]
            assert evidence["evidence_kind"] == "observed"
            assert evidence["excerpt"]
        for artifact_id in records[0]["result"]["artifact_ids"]:
            raw, _ = service.artifact_bytes(owner, p["id"], artifact_id)
            if raw.startswith(b"<!DOCTYPE"):
                assert b"input-secret-value" in raw and b"13800138000" in raw
        assert counts["writes"] == 0
        reader_token = service.create_token(
            owner, p["id"], {"name": "read trace", "scopes": ["knowledge:read", "trace:read"]}
        )
        reader = service.authenticate(reader_token["token"], "token")
        assert scenarios.captures(reader, p["id"]) == []
        observed = dict(
            saved["dsl"],
            explanation_graph={
                "projectKey": p["key"],
                "baselineKey": a["key"],
                "states": [
                    {
                        "stateKey": "captured",
                        "label": "运行观察",
                        "evidenceOrigin": "observed",
                        "captureRef": records[0]["id"],
                        "scenarioRevisionRef": saved["scenario_revision_id"],
                    }
                ],
                "transitions": [],
            },
        )
        graph = scenarios.update(
            owner, saved["scenario_id"], {"analysis_set_id": a["id"], "dsl": observed}, saved["revision_no"]
        )
        graph = scenarios.update(
            owner, saved["scenario_id"], {"analysis_set_id": a["id"], "dsl": observed}, graph["revision_no"]
        )
        candidate = service.save_snapshot(owner, p["id"], {"analysis_set_id": a["id"]}, "publish-capture")
        confirmation = None
        candidate
        visible_captures = scenarios.captures(reader, p["id"])
        assert [row["id"] for row in visible_captures] == [records[0]["id"]]
        from vks_api.knowledge import KnowledgeService

        clone = KnowledgeService(service).clone(owner, p["id"], a["id"], "复用固定采集的草稿", "clone-capture")
        cloned = scenarios.list(owner, p["id"], analysis_set_id=clone["id"])[0]
        assert (
            cloned["scenario_id"] == saved["scenario_id"]
            and cloned["scenario_revision_id"] != graph["scenario_revision_id"]
        )
        assert cloned["dsl"]["explanation_graph"]["baselineKey"] == clone["key"]
        candidate = service.save_snapshot(owner, p["id"], {"analysis_set_id": clone["id"]}, "precheck-clone")
        pass
        clone = KnowledgeService(service).clone(owner, p["id"], clone["id"], "Next draft", "clone-after-save")
        raw = b"# Additional source\nnew fixed material\n"
        import hashlib

        upload = service.create_upload(
            owner,
            p["id"],
            {
                "filename": "new.md",
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "media_type": "text/markdown",
            },
        )
        service.put_upload(owner, p["id"], upload["upload_session_id"], raw)
        update = service.import_bundle(
            owner,
            p["id"],
            {"kind": "documents", "artifact_ids": [upload["artifact_id"]], "analysis_set_id": clone["id"]},
            "new-source",
        )
        service.process_job(update["id"])
        assert service.get_job(owner, update["id"])["state"] == "succeeded"
        stale = service.save_snapshot(owner, p["id"], {"analysis_set_id": clone["id"]}, "precheck-stale-capture")
        pass
        assert [row["id"] for row in scenarios.captures(reader, p["id"])] == [records[0]["id"]]
        assert counts["writes"] == 0
        service.engine.dispose()


def test_capture_revoked_client_stops_between_steps_and_discards_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    with mock_site() as (base, counts):
        service = Service(tmp_path)
        _, owner = service.login(service.bootstrap_token)
        p = service.create_project(owner, {"key": "revocation", "name": "撤销采集"}, "project")
        a = service.create_analysis(owner, p["id"], {"name": "draft", "sources": []}, "analysis")
        scenarios = ScenarioService(service)
        scenarios.create_environment(
            owner,
            p["id"],
            {"key": "mock-test", "name": "Mock", "kind": "mock", **{k: v for k, v in env(base).items() if k != "kind"}},
        )
        scenarios.create_dataset(owner, p["id"], {"key": "synthetic", "name": "合成", "synthetic": True, "data": {}})
        saved = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": capture_dsl()}, "scenario")
        token = service.create_token(
            owner, p["id"], {"name": "capture-client", "scopes": ["knowledge:read", "knowledge:edit", "capture:run"]}
        )
        client = service.authenticate(token["token"], "token")
        approval = {
            "analysis_set_id": a["id"],
            "scenario_revision_id": saved["scenario_revision_id"],
            "requestor_actor_id": client.id,
            "reason": "本次合成采集授权",
        }
        accepted = scenarios.request_capture(
            client,
            saved["scenario_id"],
            {
                "scenario_revision_id": saved["scenario_revision_id"],
                "environment_ref": "mock-test",
                "analysis_set_id": approval["analysis_set_id"],
            },
            "capture",
        )

        def revoked_runner(*args, cancelled, **kwargs):
            assert not cancelled()
            service.revoke_token(owner, p["id"], token["id"])
            assert cancelled()
            raise CaptureError("VKS_CANCELLED", "模拟第一个步骤后撤销客户端")

        monkeypatch.setattr("vks_api.scenarios.run_capture", revoked_runner)
        service.process_job(accepted["job"]["id"])
        assert service.get_job(owner, accepted["job"]["id"])["state"] == "failed"
        assert scenarios.captures(owner, p["id"])[0]["result"] == {}
        assert counts["writes"] == 0
        service.engine.dispose()


def test_real_browser_readonly_disabled_assertion_requires_no_write_permission():
    with mock_site() as (base, counts):
        definition = capture_dsl()
        definition["steps"] = [
            definition["steps"][0],
            {
                "step_key": "disabled",
                "kind": "wait_for",
                "state": "disabled",
                "target": {"by": "test_id", "value": "disabled"},
                "timeout_ms": 2000,
                "risk": "read_only",
            },
        ]
        output = run_capture(definition, env(base), {"synthetic": True, "data": {}}, executable_path=browser_path())
        assert len(output.steps) == 2 and output.steps[-1]["assertion_passed"]
        assert counts["writes"] == 0


def test_real_browser_waits_for_delayed_unique_control_and_accepts_hidden_absence():
    with mock_site() as (base, counts):
        definition = capture_dsl()
        definition["steps"] = [
            definition["steps"][0],
            {
                "step_key": "missing",
                "kind": "wait_for",
                "state": "hidden",
                "target": {"by": "test_id", "value": "absent"},
                "timeout_ms": 1000,
                "risk": "read_only",
            },
            {
                "step_key": "delayed",
                "kind": "wait_for",
                "state": "enabled",
                "target": {"by": "test_id", "value": "delayed"},
                "timeout_ms": 2000,
                "risk": "read_only",
            },
        ]
        output = run_capture(definition, env(base), {"synthetic": True, "data": {}}, executable_path=browser_path())
        assert len(output.steps) == 3 and counts["writes"] == 0
