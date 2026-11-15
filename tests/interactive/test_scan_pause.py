"""A deliberate pause keeps the scan checkpoint and fences late browser work."""

import pytest
from sqlalchemy import select
from vks_api.bulk_association import PrototypeInventoryPage
from vks_api.models import uid
from vks_api.prototype_scan import PrototypeScanRun, ScanClient, ScanHeartbeat
from vks_api.service import DomainError

from tests.interactive import test_materials, test_scan_runs

materials = test_materials.materials


def test_pause_keeps_progress_and_resumes_same_run_without_model_replay(materials, monkeypatch):
    web, _, actor, pid, aid, calls, scans, bulk, run = test_scan_runs.start(materials, monkeypatch)
    test_scan_runs.save(bulk, actor, pid, run)
    auth = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).json()["data"]
    endpoint = f"/api/v3/projects/{pid}/associations/scans/{run['id']}/pause"
    paused = web.post(endpoint, json={"client_id": run["client_id"]}, headers={"X-CSRF-Token": auth["csrf_token"]})
    assert paused.status_code == 200, paused.text
    saved = paused.json()["data"]
    assert saved["state"] == "interrupted" and saved["pages_completed"] == 1 and saved["controls"] == 1
    assert scans.status(actor, pid, aid)["run"]["pages_completed"] == 1
    assert scans.pause(actor, pid, run["id"], run["client_id"])["pages_completed"] == 1
    with pytest.raises(DomainError):
        test_scan_runs.save(bulk, actor, pid, run, 1)
    with pytest.raises(DomainError):
        scans.heartbeat(actor, pid, run["id"], ScanHeartbeat(client_id=run["client_id"]))
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="client-after-pause"), str(run["selection_version"]), uid()
    )
    assert resumed["id"] == run["id"] and resumed["manifest"]["pages_total"] == 1
    with pytest.raises(DomainError):
        scans.pause(actor, pid, run["id"], run["client_id"])
    assert test_scan_runs.save(bulk, actor, pid, resumed)["scan_run"]["state"] == "completed"
    assert not calls


@pytest.mark.parametrize(
    "mismatch",
    [None, "observed_by", "artifact_sha256", "composition", "updated_at", "route", "status"],
)
def test_pause_restores_only_exact_failed_legacy_history(materials, monkeypatch, mismatch):
    _, service, actor, pid, aid, _, scans, bulk, run = test_scan_runs.start(materials, monkeypatch)
    test_scan_runs.save(bulk, actor, pid, run, status="failed")
    with service.db() as s:
        row = s.get(PrototypeScanRun, run["id"])
        row.completed = {}
        row.plan = {key: value for key, value in row.plan.items() if key != "_round_pending"}
        page = s.scalar(
            select(PrototypeInventoryPage).where(
                PrototypeInventoryPage.project_id == pid,
                PrototypeInventoryPage.analysis_set_id == aid,
            )
        )
        if mismatch == "status":
            page.report = {**page.report, "status": "captured"}
        elif mismatch:
            setattr(
                page,
                mismatch,
                "2000-01-01T00:00:00Z" if mismatch == "updated_at" else "unrelated-value",
            )
        # The API was stopped, so even an expired old holder can pause its run.
        row.lease_until = "2000-01-01T00:00:00Z"
    paused = scans.pause(actor, pid, run["id"], run["client_id"])
    expected = 0 if mismatch else 1
    assert paused["state"] == "interrupted"
    assert paused["pages_completed"] == 0
    assert paused["pages_attempted"] == expected and paused["pages_failed"] == expected
    assert paused["controls"] == 0 and paused["manifest"]["pages_total"] == 2
    # An idempotent pause must never invent a success or count the record twice.
    assert scans.pause(actor, pid, run["id"], run["client_id"])["pages_attempted"] == expected
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="legacy-history-resume"), str(run["selection_version"]), uid()
    )
    assert resumed["id"] == run["id"] and resumed["pages_failed"] == expected
