"""Prescans persist across browser lifetimes, fence stale clients and enqueue once."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from vks_api.bulk_association import BatchAssociationInput, BulkAssociationService, InventoryPageInput
from vks_api.models import Artifact, Job, uid
from vks_api.prototype_scan import PrototypeScanRun, PrototypeScanService, ScanClient, ScanHeartbeat, ScanInput
from vks_api.service import DomainError

from tests.interactive import test_materials, test_runtime_inventory_browser

materials = test_materials.materials


def start(materials, monkeypatch, mode="all", client="client-original"):
    web, service, actor, pid, aid, calls = test_runtime_inventory_browser.setup(materials, monkeypatch)
    scans, bulk = PrototypeScanService(service), BulkAssociationService(service)
    version = bulk.inventory_status(actor, pid, aid)["selection_version"]
    run = scans.start(actor, pid, ScanInput(analysis_set_id=aid, mode=mode, client_id=client, pipeline=False), str(version), uid())
    return web, service, actor, pid, aid, calls, scans, bulk, run


def save(bulk, actor, pid, run, index=0, status="captured", client=None, key=None):
    package = run["manifest"]["packages"][0]
    page = package["pages"][index]
    base = {k: page[k] for k in ("route", "frame_path", "label")}
    targets = [
        {**base, "selector": "body", "state": "", "open_steps": []},
        {**base, "selector": "#mounted", "label": "新增收费项", "state": "", "open_steps": []},
    ]
    if status == "failed":
        targets = []
    form = InventoryPageInput(
        analysis_set_id=run["analysis_set_id"],
        package_id=package["package_id"],
        artifact_sha256=package["sha256"],
        composition=run["composition"],
        route=page["route"],
        frame_path=page["frame_path"],
        targets=targets,
        observed=len(targets),
        truncated=0,
        unverified_frames=0,
        status=status,
        scan_run_id=run["id"],
        client_id=client or run["client_id"],
    )
    return bulk.save_inventory(actor, pid, form, str(run.get("source_selection_version", run["selection_version"])), key or uid())


def enqueue(bulk, actor, pid, run, **kwargs):
    return bulk.enqueue(
        actor,
        pid,
        BatchAssociationInput(
            analysis_set_id=run["analysis_set_id"], mode=run["mode"], scan_run_id=run["id"], **kwargs
        ),
        str(run["selection_version"]),
        uid(),
    )


def test_saved_pages_resume_after_refresh_and_batch_admission_is_once(materials, monkeypatch):
    _, service, actor, pid, aid, calls, scans, bulk, run = start(materials, monkeypatch)
    assert run["pages_total"] == 2 and run["pages_completed"] == 0
    first = save(bulk, actor, pid, run)
    assert first["scan_run"]["pages_completed"] == 1
    # A fresh coordinator sees server progress without beginning a new model request.
    refreshed = PrototypeScanService(service).status(actor, pid, aid)["run"]
    assert refreshed["id"] == run["id"] and refreshed["pages_completed"] == 1
    assert not calls and refreshed["manifest"]["pages_total"] == 1
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="client-after-refresh"), str(run["selection_version"]), uid()
    )
    assert resumed["manifest"]["pages_total"] == 1
    with pytest.raises(DomainError):
        save(bulk, actor, pid, run, 1)
    complete = save(bulk, actor, pid, resumed)["scan_run"]
    assert complete["state"] == "completed" and complete["pages_completed"] == 2
    assert scans.heartbeat(actor, pid, run["id"], ScanHeartbeat(client_id=resumed["client_id"]))["state"] == "completed"
    first_job = enqueue(bulk, actor, pid, complete)
    second_job = enqueue(bulk, actor, pid, complete)
    assert first_job["id"] == second_job["id"]
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "associate_all")) == 1
    assert not calls


def test_cancel_and_client_takeover_fence_late_save_heartbeat_and_cancel(materials, monkeypatch):
    _, _service, actor, pid, aid, _, scans, bulk, run = start(materials, monkeypatch)
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="client-new-owner"), str(run["selection_version"]), uid()
    )
    with pytest.raises(DomainError):
        scans.cancel(actor, pid, run["id"], run["client_id"])
    with pytest.raises(DomainError):
        scans.heartbeat(actor, pid, run["id"], ScanHeartbeat(client_id=run["client_id"]))
    with pytest.raises(DomainError):
        save(bulk, actor, pid, run)
    scans.cancel(actor, pid, run["id"], resumed["client_id"])
    assert scans.status(actor, pid, aid)["run"]["state"] == "cancelled"
    with pytest.raises(DomainError):
        save(bulk, actor, pid, resumed)
    with pytest.raises(DomainError):
        scans.resume(
            actor, pid, run["id"], ScanClient(client_id="another-client"), str(run["selection_version"]), uid()
        )
    with pytest.raises(DomainError):
        enqueue(bulk, actor, pid, resumed, allow_partial=True)


def test_partial_failed_pages_require_explicit_subset_and_never_include_failed_modules(materials, monkeypatch):
    _, service, actor, pid, _, _, scans, bulk, run = start(materials, monkeypatch)
    save(bulk, actor, pid, run)
    partial = save(bulk, actor, pid, run, 1, status="failed")["scan_run"]
    assert partial["state"] == "partial" and partial["pages_completed"] == 1 and partial["pages_attempted"] == 2
    assert partial["pages_failed"] == 1 and partial["manifest"]["pages_total"] == 1
    with service.db() as s:
        s.get(PrototypeScanRun, run["id"]).state = "completed"  # Prior process's attempted-only completion.
    assert scans.status(actor, pid, partial["analysis_set_id"])["run"]["state"] == "partial"
    with pytest.raises(DomainError):
        enqueue(bulk, actor, pid, partial)
    job = enqueue(bulk, actor, pid, partial, allow_partial=True)
    with service.db() as s:
        saved = s.get(Job, job["id"])
        assert saved.input_json["partial_scope"]["pages_captured"] == 1
        assert len(saved.input_json["items"]) == 2
        assert {item["target"]["route"] for item in saved.input_json["items"]} == {
            run["manifest"]["packages"][0]["pages"][0]["route"]
        }
    assert scans.status(actor, pid, partial["analysis_set_id"])["run"]["pages_failed"] == 1


def test_lease_expiry_and_changed_scope_cannot_keep_writing(materials, monkeypatch):
    _, service, actor, pid, aid, _, scans, bulk, run = start(materials, monkeypatch)
    save(bulk, actor, pid, run)
    with service.db() as s:
        s.get(PrototypeScanRun, run["id"]).lease_until = "2000-01-01T00:00:00Z"
    assert scans.status(actor, pid, aid)["run"]["state"] == "interrupted"
    with pytest.raises(DomainError):
        save(bulk, actor, pid, run, 1)
    current = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="new-lease-client"), str(run["selection_version"]), uid()
    )
    _, _, _, manager, _ = materials
    document = test_materials.stage(service, actor, manager, pid, aid, "documents", "# 新资料\n\n已变更。")
    test_materials.activate(service, actor, manager, pid, aid, [document])
    status = scans.status(actor, pid, aid)["run"]
    assert status["state"] == "invalidated" and status["pages_total"] == 0
    with pytest.raises(DomainError):
        save(bulk, actor, pid, current)


def test_resume_keeps_attempted_history_and_does_not_finish_before_failed_retry(materials, monkeypatch):
    _, _service, actor, pid, _aid, _, scans, bulk, run = start(materials, monkeypatch)
    failed = save(bulk, actor, pid, run, 0, status="failed")["scan_run"]
    assert failed["pages_attempted"] == 1 and failed["pages_failed"] == 1
    paused = scans.pause(actor, pid, run["id"], run["client_id"])
    assert paused["state"] == "interrupted" and paused["pages_attempted"] == 1
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="history-resume-client"), str(run["selection_version"]), uid()
    )
    assert resumed["pages_attempted"] == 1 and resumed["pages_failed"] == 1
    # Completing the unattempted page must not terminate before retrying the
    # already recorded failure later in this round's work plan.
    middle = save(bulk, actor, pid, resumed, 1)["scan_run"]
    assert middle["state"] == "running" and middle["pages_attempted"] == 2 and middle["pages_failed"] == 1
    complete = save(bulk, actor, pid, resumed, 0)["scan_run"]
    assert complete["state"] == "completed" and complete["pages_attempted"] == 2 and complete["pages_failed"] == 0


def test_source_restriction_and_read_only_token_do_not_expose_or_cancel_scan(materials, monkeypatch):
    _, service, actor, pid, aid, _, scans, _bulk, run = start(materials, monkeypatch)
    limited = replace(actor, scopes=frozenset({"knowledge:read", "trace:read"}))
    with pytest.raises(DomainError):
        scans.cancel(limited, pid, run["id"], run["client_id"])
    package = run["manifest"]["packages"][0]
    with service.db() as s:
        s.get(Artifact, package["package_id"]).sensitivity = "confidential"
    dto = scans.status(limited, pid, aid)["run"]
    assert dto["diagnostics_restricted"] and dto["pages_total"] == 0 and dto["manifest"]["packages"] == []


def test_active_model_job_without_new_controls_is_not_silently_bound_to_new_scan(materials, monkeypatch):
    _, service, actor, pid, aid, _, _scans, bulk, run = start(materials, monkeypatch)
    old = bulk.enqueue(
        actor, pid, BatchAssociationInput(analysis_set_id=aid, mode="all"), str(run["selection_version"]), uid()
    )
    save(bulk, actor, pid, run)
    complete = save(bulk, actor, pid, run, 1)["scan_run"]
    with pytest.raises(DomainError, match="未包含新扫描目标"):
        enqueue(bulk, actor, pid, complete)
    with service.db() as s:
        assert s.get(PrototypeScanRun, run["id"]).batch_job_id is None
    service.cancel_job(actor, old["id"])
    new = enqueue(bulk, actor, pid, complete)
    with service.db() as s:
        assert len(s.get(Job, new["id"]).input_json["items"]) == 4


def test_http_scan_protocol_auth_progress_and_cancel_binding(materials, monkeypatch):
    web, _service, _actor, pid, aid, _, _scans, _bulk, run = start(materials, monkeypatch)
    endpoint = f"/api/v3/projects/{pid}/associations/scans"
    assert web.get(endpoint + f"?analysis_set_id={aid}").status_code == 401
    auth = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).json()["data"]
    response = web.get(endpoint + f"?analysis_set_id={aid}")
    assert response.status_code == 200 and response.json()["data"]["run"]["id"] == run["id"]
    stopped = web.post(
        endpoint + f"/{run['id']}/cancel",
        json={"client_id": run["client_id"]},
        headers={"X-CSRF-Token": auth["csrf_token"]},
    )
    assert stopped.status_code == 200 and stopped.json()["data"]["state"] == "cancelled"


def test_different_modes_do_not_silently_reuse_an_active_scan_and_legacy_progress_is_visible(materials, monkeypatch):
    _, service, actor, pid, aid, _, scans, bulk, run = start(materials, monkeypatch, mode="missing")
    with pytest.raises(DomainError):
        scans.start(
            actor,
            pid,
            ScanInput(analysis_set_id=aid, mode="all", client_id="different-mode"),
            str(run["selection_version"]),
            uid(),
        )
    # No task is invented for pre-upgrade persisted page records.
    save(bulk, actor, pid, run)
    with service.db() as s:
        s.delete(s.get(PrototypeScanRun, run["id"]))
    legacy = scans.status(actor, pid, aid)
    assert legacy["run"] is None
    assert legacy["stored_progress"] == {"pages_total": 2, "pages_captured": 1, "pages_failed": 0, "controls": 1}
