"""Independent atomicity, replay and late-scope tests for incremental page AI."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import func, select
from vks_api import ai_gateway
from vks_api.bulk_association import InventoryPageInput, PrototypeInventoryPage
from vks_api.models import Analysis, Job, Outbox, uid
from vks_api.prototype_scan import PrototypeScanRun, ScanClient
from vks_api.scan_pipeline import PrototypeScanTarget, reconcile
from vks_api.service import DomainError, future

from tests.interactive import test_materials
from tests.interactive import test_scan_coordination_audit as audit

materials = test_materials.materials


def jobs(service, run_id):
    with service.db() as s:
        return [
            (row.id, deepcopy(row.input_json))
            for row in s.scalars(select(Job).where(Job.kind == "associate_all").order_by(Job.created_at, Job.id))
            if row.input_json.get("scan_run_id") == run_id
        ]


def test_page_ack_and_progress_cannot_commit_without_its_ai_jobs_outbox_and_unique_markers(materials, monkeypatch):
    service, actor, _, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    page = run["manifest"]["packages"][0]["pages"][0]
    original, created = service._new_job, []

    def fail_second_job(session, owner, project, kind, payload):
        if kind == "associate_all":
            created.append(payload)
            if len(created) == 2:
                raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "synthetic enqueue failure", 503)
        return original(session, owner, project, kind, payload)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_new_job", fail_second_job)
        with pytest.raises(DomainError, match="synthetic enqueue failure"):
            audit.capture(bulk, actor, pid, run, version, page)
    assert len(created) == 2 and not jobs(service, run["id"])
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(PrototypeInventoryPage)) == 0
        assert s.scalar(select(func.count()).select_from(PrototypeScanTarget)) == 0
        assert s.get(PrototypeScanRun, run["id"]).completed == {}
    assert scans.status(actor, pid, aid)["run"]["pages_completed"] == 0 and not calls
    captured = audit.capture(bulk, actor, pid, run, version, page)["scan_run"]
    assert captured["pipeline"] and captured["state"] == "running"
    produced = jobs(service, run["id"])
    assert len(produced) == captured["ai"]["targets"] == 2
    with service.db() as s:
        assert (
            s.scalar(select(func.count()).select_from(Outbox).where(Outbox.job_id.in_([j[0] for j in produced]))) == 2
        )


def test_concurrent_page_replays_queue_each_dynamic_target_once_and_ai_does_not_invalidate_remaining_pages(
    materials, monkeypatch
):
    service, actor, _, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    pages, barrier = run["manifest"]["packages"][0]["pages"], Barrier(3)

    def capture():
        barrier.wait(timeout=5)
        return audit.capture(bulk, actor, pid, run, version, pages[0])

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: capture(), range(3)))
    first = jobs(service, run["id"])
    assert len(first) == 2
    first_payloads = {jid: payload for jid, payload in first}
    for jid, payload in first:
        assert payload["scan_pipeline"] and payload["concurrency"] == 1
        assert {item["target"]["selector"] for item in payload["items"]} <= {"body", "#captured"}
        assert all(item["target"]["route"] == pages[0]["route"] for item in payload["items"])
        service.process_job(jid)
        assert service.get_job(actor, jid)["state"] == "succeeded"
    reconcile(service)
    current = scans.status(actor, pid, aid)["run"]
    assert current["pages_completed"] == 1 and current["ai"]["completed"] == 2
    with service.db() as s:
        assert s.get(Analysis, aid).selection_version > int(version)
    # Same source composition, original exact scan version; AI-created points
    # must not force the still-running browser workers to restart or resend.
    second = audit.capture(bulk, actor, pid, run, version, pages[1])["scan_run"]
    assert second["pages_completed"] == 2 and second["source_selection_version"] == int(version)
    assert len(jobs(service, run["id"])) == 4 and len(calls) == 2
    assert {jid: payload for jid, payload in jobs(service, run["id"]) if jid in first_payloads} == first_payloads


def test_pausing_the_browser_keeps_authorized_ai_running_and_resume_does_not_resend_completed_targets(
    materials, monkeypatch
):
    service, actor, _, pid, _aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    page = run["manifest"]["packages"][0]["pages"][0]
    audit.capture(bulk, actor, pid, run, version, page)
    paused = scans.pause(actor, pid, run["id"], run["client_id"])
    assert paused["state"] == "interrupted" and paused["ai"]["queued"] == 2
    with pytest.raises(DomainError):
        audit.capture(bulk, actor, pid, run, version, page)
    for jid, _ in jobs(service, run["id"]):
        service.process_job(jid)
        assert service.get_job(actor, jid)["state"] == "succeeded"
    reconcile(service)
    resumed = scans.resume(actor, pid, run["id"], ScanClient(client_id="new-pipeline-client"), version, uid())
    assert resumed["pages_completed"] == 1 and resumed["ai"]["completed"] == 2
    assert len(jobs(service, run["id"])) == len(calls) == 2


def test_cancelling_a_run_fences_the_first_late_model_response_and_all_other_children(materials, monkeypatch):
    service, actor, _, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    page = run["manifest"]["packages"][0]["pages"][0]
    audit.capture(bulk, actor, pid, run, version, page)
    original = ai_gateway.request_model

    def cancel_then_return(config, payload):
        scans.cancel(actor, pid, run["id"], run["client_id"])
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", cancel_then_return)
    for jid, _ in jobs(service, run["id"]):
        service.process_job(jid)
        assert service.get_job(actor, jid)["state"] == "cancelled"
    reconcile(service)
    state = scans.status(actor, pid, aid)["run"]
    assert state["state"] == "cancelled" and state["pages_completed"] == 1
    assert state["ai"]["cancelled"] == 2 and len(calls) == 1
    assert not bulk.links.bindings(actor, pid, aid)


def test_legacy_saved_pages_only_enter_pipeline_after_an_explicit_resume_and_never_twice(materials, monkeypatch):
    service, actor, _, pid, _aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=False)
    page = run["manifest"]["packages"][0]["pages"][0]
    audit.capture(bulk, actor, pid, run, version, page)
    scans.pause(actor, pid, run["id"], run["client_id"])
    assert not jobs(service, run["id"]) and not calls
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="legacy-upgrade-client", pipeline=True), version, uid()
    )
    assert resumed["pipeline"] and resumed["ai"]["targets"] == 2
    scans.pause(actor, pid, run["id"], resumed["client_id"])
    scans.resume(actor, pid, run["id"], ScanClient(client_id="legacy-again-client", pipeline=True), version, uid())
    assert len(jobs(service, run["id"])) == 2


def test_new_rows_in_an_active_operation_group_wait_for_immutable_work_and_reuse_its_checked_answer(
    materials, monkeypatch
):
    service, actor, _, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    package = run["manifest"]["packages"][0]
    page = package["pages"][0]
    body = {
        "route": page["route"],
        "frame_path": page["frame_path"],
        "selector": "body",
        "label": page["label"],
        "state": "",
        "open_steps": [],
    }

    def save_rows(count):
        targets = [body] + [
            {
                **body,
                "selector": f"table > tr:nth-of-type({index}) > td > button",
                "label": "编辑收费项",
            }
            for index in range(1, count + 1)
        ]
        bulk.save_inventory(
            actor,
            pid,
            InventoryPageInput(
                analysis_set_id=aid,
                package_id=package["package_id"],
                artifact_sha256=package["sha256"],
                composition=run["composition"],
                route=page["route"],
                frame_path=page["frame_path"],
                targets=targets,
                observed=len(targets),
                truncated=0,
                status="captured",
                scan_run_id=run["id"],
                client_id=run["client_id"],
            ),
            version,
            uid(),
        )

    save_rows(1)
    initial = jobs(service, run["id"])
    operation = next(
        (jid, payload) for jid, payload in initial if "tr:nth-of-type" in payload["items"][0]["target"]["selector"]
    )
    save_rows(2)
    assert jobs(service, run["id"]) == initial, "an active immutable job must not absorb the new target"
    with service.db() as s:
        assert (
            s.scalar(
                select(func.count()).select_from(PrototypeScanTarget).where(PrototypeScanTarget.state == "waiting")
            )
            == 1
        )
    service.process_job(operation[0])
    reconcile(service)
    expanded = jobs(service, run["id"])
    assert len(expanded) == 3
    alias = next((jid, payload) for jid, payload in expanded if jid not in {item[0] for item in initial})
    assert alias[1]["reuse_from_item"]["id"] == operation[1]["items"][0]["id"]
    assert len(alias[1]["items"]) == 1 and "(2)" in alias[1]["items"][0]["target"]["selector"]
    for jid, _ in expanded:
        service.process_job(jid)
    reconcile(service)
    assert len([request for request in calls if "tr:nth-of-type" in request["control"]["selector"]]) == 1
    state = scans.status(actor, pid, aid)["run"]
    assert state["ai"]["targets"] == state["ai"]["completed"] == 3 and state["controls"] == 2
    assert len(bulk.links.bindings(actor, pid, aid)) == 3


def test_cancelling_inflight_models_still_occupy_the_persistent_global_three_slot_cap(materials, monkeypatch):
    service, actor, _, pid, _aid, bulk, _scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    pages = run["manifest"]["packages"][0]["pages"]
    for page in pages[:2]:
        audit.capture(bulk, actor, pid, run, version, page)
    produced = jobs(service, run["id"])
    assert len(produced) == 4
    with service.db() as s:
        for index, (jid, _) in enumerate(produced[:3]):
            row = s.get(Job, jid)
            row.state = "cancelling" if index == 0 else "running"
            row.cancel_requested = index == 0
            row.lease_until = future(60)
    service.process_job(produced[3][0])
    with service.db() as s:
        waiting = s.get(Job, produced[3][0])
        assert waiting.state == "queued" and waiting.attempt == 0
        stopped = s.get(Job, produced[0][0])
        stopped.state, stopped.lease_until = "cancelled", None
    assert not calls
    service.process_job(produced[3][0])
    assert service.get_job(actor, produced[3][0])["state"] == "succeeded" and len(calls) == 1


def test_source_changes_reject_queued_ai_and_old_browser_captures_before_model_submission(materials, monkeypatch):
    service, actor, manager, pid, aid, bulk, scans, version, run, calls = audit.setup(
        materials, monkeypatch, pipeline=True
    )
    pages = run["manifest"]["packages"][0]["pages"]
    audit.capture(bulk, actor, pid, run, version, pages[0])
    changed = test_materials.stage(service, actor, manager, pid, aid, "documents", "# 收费项\n\n已变更的真实资料。")
    test_materials.activate(service, actor, manager, pid, aid, [changed])
    for jid, _ in jobs(service, run["id"]):
        service.process_job(jid)
        assert service.get_job(actor, jid)["state"] == "failed"
    with pytest.raises(DomainError):
        audit.capture(bulk, actor, pid, run, version, pages[1])
    reconcile(service)
    state = scans.status(actor, pid, aid)["run"]
    assert state["state"] == "invalidated" and state["ai"] is None
    assert state["pages_completed"] == state["controls"] == 0 and not state["manifest"]["packages"]
    assert not calls and not bulk.links.bindings(actor, pid, aid)


def test_explicit_ai_retry_only_requeues_the_failed_target_and_preserves_finished_work(materials, monkeypatch):
    service, actor, _, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch, pipeline=True)
    page = run["manifest"]["packages"][0]["pages"][0]
    original, attempted = ai_gateway.request_model, []

    def fail_one(config, payload):
        selector = json.loads(payload["input"][1]["content"])["control"]["selector"]
        attempted.append(selector)
        if selector == "#captured":
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "synthetic model unavailable", 503)
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", fail_one)
    audit.capture(bulk, actor, pid, run, version, page)
    initial = jobs(service, run["id"])
    for jid, _ in initial:
        service.process_job(jid)
    reconcile(service)
    state = scans.status(actor, pid, aid)["run"]
    assert state["ai"]["completed"] == state["ai"]["failed"] == 1
    assert state["pages_completed"] == 1 and len(attempted) == 2
    # Read/poll alone never resends a failed model request.
    reconcile(service)
    assert len(jobs(service, run["id"])) == 2
    monkeypatch.setattr(ai_gateway, "request_model", original)
    retried = scans.retry_ai(actor, pid, run["id"], version, uid())
    assert retried["ai"]["completed"] == 1 and retried["ai"]["queued"] == 1
    new = [entry for entry in jobs(service, run["id"]) if entry[0] not in {j[0] for j in initial}]
    assert len(new) == 1 and new[0][1]["items"][0]["target"]["selector"] == "#captured"
    service.process_job(new[0][0])
    reconcile(service)
    final = scans.status(actor, pid, aid)["run"]
    assert final["ai"]["completed"] == 2 and final["ai"]["failed"] == 0
    assert len(calls) == len(bulk.links.bindings(actor, pid, aid)) == 2
