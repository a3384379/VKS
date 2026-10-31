"""G02 uses actual sandboxed Chromium and a synthetic loopback page."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import copy
import pytest
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from vks_api.documents import DocumentService
from vks_api.models import Relation
from vks_api.scenarios import ScenarioService
from vks_api.service import DomainError


@pytest.fixture
def captured(audit_store, monkeypatch):
    c = audit_store
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    if not Path(executable).is_file():
        installed = sorted(
            (Path.home() / "Library/Caches/ms-playwright").glob(
                "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        if not installed:
            pytest.skip("Independent Chromium is not installed")
        executable = str(installed[-1])
    monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", executable)
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<main><h1>Synthetic observed state</h1><p>No business request</p></main>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    svc = ScenarioService(c.s)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        svc.create_environment(
            c.owner,
            c.p,
            dict(
                key="audit",
                name="Audit mock",
                kind="mock",
                base_origin=base,
                allowed_origins=[base],
                allowed_cidrs=["127.0.0.1/32"],
                allow_loopback=True,
            ),
        )
        svc.create_dataset(c.owner, c.p, dict(key="synthetic", name="Synthetic", synthetic=True, data={}))
        saved = svc.create(
            c.owner,
            c.p,
            {
                "analysis_set_id": c.aid,
                "dsl": {
                    "schema_version": "1.0",
                    "scenario_key": "capture-bind",
                    "name": "Capture binding",
                    "environment_ref": "audit",
                    "dataset_ref": "synthetic",
                    "risk_level": "read_only",
                    "timeout_ms": 8000,
                    "steps": [
                        {"step_key": "open", "kind": "goto", "path": "/", "risk": "read_only", "timeout_ms": 3000},
                        {
                            "step_key": "snapshot",
                            "kind": "snapshot",
                            "name": "Observed synthetic",
                            "risk": "read_only",
                            "timeout_ms": 3000,
                        },
                    ],
                    "explanation_graph": {
                        "projectKey": "charge-demo",
                        "baselineKey": c.w["selected_analysis_set"]["key"],
                        "states": [
                            {"stateKey": "declared", "label": "Declared", "evidenceOrigin": "declared"},
                            {"stateKey": "simulated", "label": "Simulated", "evidenceOrigin": "simulated"},
                        ],
                        "transitions": [],
                    },
                },
            },
            "scenario",
        )
        approval = dict(
            analysis_set_id=c.aid, scenario_revision_id=saved["scenario_revision_id"], reason="Synthetic only"
        )
        request = svc.request_capture(
            c.owner,
            saved["scenario_id"],
            dict(
                scenario_revision_id=saved["scenario_revision_id"],
                environment_ref="audit",
                analysis_set_id=approval["analysis_set_id"],
            ),
            "capture",
        )
        c.s.process_job(request["job"]["id"])
        record = svc.captures(c.owner, c.p)[0]
        assert record["state"] == "succeeded", c.s.get_job(c.owner, record["job_id"])
        yield (c, svc, saved, record)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def binding_payload(c, scenario, capture):
    return dict(
        analysis_set_id=c.aid,
        scenario_revision_id=scenario["scenario_revision_id"],
        capture_id=capture["id"],
        snapshot_index=0,
        state_key="observed-new",
        label="Observed synthetic snapshot",
        action_id=c.w["actions"][0]["entity_id"],
    )


@pytest.mark.parametrize(
    "graph", [None, {"projectKey": "charge-demo", "baselineKey": "unused", "states": [None], "transitions": []}]
)
def test_g01_malformed_state_graph_is_a_safe_input_error(audit_store, graph):
    c = audit_store
    with pytest.raises(DomainError) as error:
        ScenarioService(c.s).create(
            c.owner,
            c.p,
            {
                "analysis_set_id": c.aid,
                "dsl": {
                    "schema_version": "1.0",
                    "scenario_key": "invalid",
                    "name": "Invalid synthetic state",
                    "environment_ref": "none",
                    "dataset_ref": "none",
                    "risk_level": "read_only",
                    "timeout_ms": 2000,
                    "steps": [
                        {"step_key": "open", "kind": "goto", "path": "/", "risk": "read_only", "timeout_ms": 1000}
                    ],
                    "explanation_graph": graph,
                },
            },
            "invalid",
        )
    assert error.value.code == "VKS_INVALID_INPUT"


def test_g02_binding_creates_immutable_revision_and_preserves_origins_without_causal_upgrade(captured):
    c, svc, scenario, capture = captured
    before = copy.deepcopy(scenario["dsl"])
    result = svc.bind_capture(
        c.owner, scenario["scenario_id"], binding_payload(c, scenario, capture), scenario["revision_no"], "bind"
    )
    assert result["scenario_revision_id"] != scenario["scenario_revision_id"]
    assert svc.get(c.owner, c.p, scenario["scenario_id"], scenario["scenario_revision_id"])["dsl"] == before
    states = {s["stateKey"]: s for s in result["dsl"]["explanation_graph"]["states"]}
    assert states["declared"]["evidenceOrigin"] == "declared"
    assert states["simulated"]["evidenceOrigin"] == "simulated"
    assert states["observed-new"]["evidenceOrigin"] == "observed"
    assert states["observed-new"]["captureRef"] == capture["id"]
    assert states["observed-new"]["actionBindingOrigin"] == "declared"
    repeated = svc.bind_capture(
        c.owner, scenario["scenario_id"], binding_payload(c, scenario, capture), scenario["revision_no"], "bind"
    )
    assert repeated["scenario_revision_id"] == result["scenario_revision_id"]
    with c.s.db() as db:
        assert not any((r.causal_basis == "correlated_runtime" for r in db.scalars(select(Relation))))


@pytest.mark.parametrize(
    "case", ["other_project", "changed_baseline", "old_revision", "no_trace", "simulated_state", "bad_snapshot"]
)
def test_g02_binding_rejects_invalid_context_and_keeps_current_revision(captured, case):
    c, svc, scenario, capture = captured
    payload, actor = (binding_payload(c, scenario, capture), c.owner)
    if case == "other_project":
        other = c.s.create_project(c.owner, {"key": "other", "name": "Other"}, "other")
        payload["analysis_set_id"] = c.s.create_analysis(
            c.owner, other["id"], {"name": "Other", "sources": []}, "other"
        )["id"]
    elif case == "changed_baseline":
        doc = c.w["documents"][0]
        DocumentService(c.s).save_draft(
            c.owner,
            doc["entity_id"],
            dict(
                analysis_set_id=c.aid,
                base_revision_id=doc["entity_revision_id"],
                markdown=doc["content"] + "\nChanged baseline\n",
                locked_section_ids=[],
            ),
            1,
        )
    elif case == "old_revision":
        payload["scenario_revision_id"] = "unknown"
    elif case == "no_trace":
        token = c.s.create_token(c.owner, c.p, {"name": "No trace", "scopes": ["knowledge:read", "knowledge:edit"]})
        actor = c.s.authenticate(token["token"], "token")
    elif case == "simulated_state":
        payload["state_key"] = "simulated"
    elif case == "bad_snapshot":
        payload["snapshot_index"] = 99
    with pytest.raises(DomainError):
        svc.bind_capture(actor, scenario["scenario_id"], payload, scenario["revision_no"], "bad")
    current = svc.list(c.owner, c.p, c.aid)[0]
    assert current["scenario_revision_id"] == scenario["scenario_revision_id"]
