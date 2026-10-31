"""Actual ten-step isolated Mock capture; timings exclude real-business acceptance."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json
import os
from pathlib import Path
import time
from playwright.sync_api import Page, sync_playwright
from vks_api.service import Service
from vks_api.scenarios import ScenarioService


def test_ten_step_mock_capture_reports_capture_and_storage_phases(tmp_path, monkeypatch):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b'<main><input value="13800138000"><input type="password" value="synthetic-private"><p>access_token=synthetic-private</p><p data-testid="status">ready</p></main>'
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as pw:
        executable = os.getenv("VKS_CHROMIUM_EXECUTABLE") or pw.chromium.executable_path
    if not Path(executable).exists():
        installed = sorted(
            (Path.home() / "Library/Caches/ms-playwright").glob(
                "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        assert installed, "Install isolated Playwright Chromium before capture acceptance"
        executable = str(installed[-1])
    monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", executable)
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    base = f"http://127.0.0.1:{server.server_port}"
    service = Service(tmp_path / "store")
    try:
        _, actor = service.login(service.bootstrap_token)
        project = service.create_project(actor, dict(key="capture-profile", name="Synthetic capture profile"), "p")
        aid = service.create_analysis(actor, project["id"], dict(name="draft", sources=[]), "draft")["id"]
        scenes = ScenarioService(service)
        scenes.create_environment(
            actor,
            project["id"],
            dict(
                key="mock",
                name="Mock",
                kind="mock",
                base_origin=base,
                allowed_origins=[base],
                allowed_cidrs=["127.0.0.1/32"],
                allow_loopback=True,
                allowed_write_paths=[],
            ),
        )
        scenes.create_dataset(actor, project["id"], dict(key="synthetic", name="Synthetic", synthetic=True, data={}))
        steps = [dict(step_key="open", kind="goto", path="/", risk="read_only", timeout_ms=4000)]
        for i in range(9):
            steps.append(
                dict(step_key=f"step-{i}", kind="snapshot", name=f"Snapshot {i}", risk="read_only", timeout_ms=4000)
                if i % 2 == 0
                else dict(
                    step_key=f"step-{i}",
                    kind="assert_text",
                    target=dict(by="test_id", value="status"),
                    expected=dict(literal="ready"),
                    risk="read_only",
                    timeout_ms=2000,
                )
            )
        dsl = dict(
            schema_version="1.0",
            scenario_key="profile",
            name="Ten-step Mock",
            environment_ref="mock",
            dataset_ref="synthetic",
            risk_level="read_only",
            timeout_ms=40000,
            steps=steps,
        )
        saved = scenes.create(actor, project["id"], dict(analysis_set_id=aid, dsl=dsl), "scene")
        approval = dict(
            analysis_set_id=aid,
            scenario_revision_id=saved["scenario_revision_id"],
            reason="Synthetic private Mock profile",
        )
        accepted = scenes.request_capture(
            actor,
            saved["scenario_id"],
            dict(
                scenario_revision_id=saved["scenario_revision_id"],
                environment_ref="mock",
                analysis_set_id=approval["analysis_set_id"],
            ),
            "capture",
        )
        phases = dict(dom_capture_seconds=0.0, screenshot_seconds=0.0, object_storage_seconds=0.0)
        for obj, name, key in (
            (Page, "content", "dom_capture_seconds"),
            (Page, "screenshot", "screenshot_seconds"),
            (service, "_put_object", "object_storage_seconds"),
        ):
            original = getattr(obj, name)

            def measured(*args, _call=original, _key=key, **kwargs):
                started = time.monotonic()
                try:
                    return _call(*args, **kwargs)
                finally:
                    phases[_key] += time.monotonic() - started

            monkeypatch.setattr(obj, name, measured)
        started = time.monotonic()
        service.process_job(accepted["job"]["id"])
        elapsed = time.monotonic() - started
        job = service.get_job(actor, accepted["job"]["id"])
        assert job["state"] == "succeeded", job.get("error")
        record = scenes.captures(actor, project["id"])[0]
        assert len(record["result"]["steps"]) == 10 and len(record["result"]["snapshots"]) == 5
        for item in record["result"]["snapshots"]:
            raw, _ = service.artifact_bytes(actor, project["id"], item["dom_artifact_id"])
            assert b"13800138000" in raw and b"synthetic-private" in raw
        report = dict(
            profile="synthetic ten-step read-only Mock; five saved snapshots; not business branch acceptance",
            steps=10,
            snapshots=5,
            elapsed_seconds=elapsed,
            phases=phases,
            external_model_calls=0,
            phase_scope="Original DOM capture, screenshot call, object writes separately; DB/network/queue overhead remains in total",
        )
        if destination := os.getenv("VKS_CAPTURE_PROFILE_OUTPUT"):
            Path(destination).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        service.engine.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
