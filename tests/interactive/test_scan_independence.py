"""Scan continuation is independent of fixed, already running AI work."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select
from vks_api.bulk_association import BatchAssociationInput
from vks_api.jobs import Checkpoint
from vks_api.models import Analysis, Job, uid
from vks_api.prototype_scan import PrototypeScanRun, ScanClient, ScanInput, ScanResume
from vks_api.scan_pipeline import claim, reconcile
from vks_api.service import DomainError, future

from tests.interactive import test_bulk_association, test_materials
from tests.interactive import test_scan_coordination_audit as audit

materials = test_materials.materials


def partial_with_batch(materials, monkeypatch):
    service, actor, manager, pid, aid, bulk, scans, version, run, calls = audit.setup(materials, monkeypatch)
    pages = run["manifest"]["packages"][0]["pages"]
    for i, page in enumerate(pages):
        audit.capture(bulk, actor, pid, run, version, page, failed=i != 0)
    job = bulk.enqueue(
        actor,
        pid,
        BatchAssociationInput(analysis_set_id=aid, mode=run["mode"], scan_run_id=run["id"], allow_partial=True),
        version,
        uid(),
    )
    with service.db() as s:
        row = s.get(Job, job["id"])
        row.state, row.lease_until = "running", future(60)
        payload = deepcopy(row.input_json)
    return service, actor, manager, pid, aid, bulk, scans, version, run, calls, job, payload


def test_resume_partial_while_legacy_ai_runs_adopts_old_work_and_only_enqueues_new_targets(materials, monkeypatch):
    service, actor, _, pid, aid, bulk, scans, version, run, calls, job, payload = partial_with_batch(
        materials, monkeypatch
    )
    item = payload["items"][0]
    with service.db() as s:
        s.add(
            Checkpoint(
                job_id=job["id"],
                stage="ASSOCIATE",
                input_hash=item["id"],
                processor_version=payload["processor"],
                result_json={
                    "id": item["id"],
                    "label": item["target"]["label"],
                    "status": "linked",
                    "saved": True,
                    "message": "已保存",
                },
            )
        )
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="resume-during-ai", pipeline=True), version, uid()
    )
    assert resumed["state"] == "running" and resumed["batch_job_id"] == job["id"]
    assert resumed["pages_completed"] == 1 and resumed["ai"]["targets"] == 2
    reconcile(service)
    assert scans.status(actor, pid, aid)["run"]["ai"]["completed"] == 1
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "associate_all")) == 1
    page = run["manifest"]["packages"][0]["pages"][1]
    audit.capture(bulk, actor, pid, run, version, page, client="resume-during-ai")
    with service.db() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "associate_all")))
        assert len(jobs) == 3 and s.get(Job, job["id"]).input_json == payload
        for child in jobs:
            if child.id != job["id"]:
                assert {i["target"]["route"] for i in child.input_json["items"]} == {page["route"]}
                assert not claim(service, s, child), "legacy's three active lanes already occupy this run's AI budget"
    assert scans.pause(actor, pid, run["id"], resumed["client_id"])["state"] == "interrupted"
    assert service.get_job(actor, job["id"])["state"] == "running" and not calls


def test_scan_only_resume_keeps_association_job_running_without_starting_more_ai(materials, monkeypatch):
    service, actor, _, pid, _, bulk, scans, version, run, calls, job, _ = partial_with_batch(materials, monkeypatch)
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="scan-only-resume", pipeline=False), version, uid()
    )
    assert not resumed["pipeline"]
    for page in resumed["manifest"]["packages"][0]["pages"]:
        audit.capture(bulk, actor, pid, run, version, page, client="scan-only-resume")
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "associate_all")) == 1
        assert s.get(Job, job["id"]).state == "running"
        assert s.get(PrototypeScanRun, run["id"]).state == "completed"
    assert not calls


def test_analysis_source_token_ignores_binding_counter_but_rejects_changed_documents(materials, monkeypatch):
    service, actor, manager, pid, aid, bulk, scans, version, run, _ = audit.setup(materials, monkeypatch)
    for page in run["manifest"]["packages"][0]["pages"]:
        audit.capture(bulk, actor, pid, run, version, page)
    digest = bulk.status(actor, pid, aid)["composition"]
    with service.db() as s:
        s.get(Analysis, aid).selection_version += 1
    form = ScanInput(
        analysis_set_id=aid, mode="changed", client_id="independent-analysis", pipeline=False, composition=digest
    )
    assert scans.start(actor, pid, form, version, uid())["state"] == "completed"
    update = test_materials.stage(
        service, actor, manager, pid, aid, "documents", "# Different document\n\nReplaced source."
    )
    test_materials.activate(service, actor, manager, pid, aid, [update])
    with pytest.raises(DomainError, match="资料版本"):
        scans.start(actor, pid, form, version, uid())


@pytest.mark.parametrize("lanes", [1, 4])
def test_pipeline_claim_obeys_configured_request_budget(materials, monkeypatch, lanes):
    service, actor, _, pid, _, bulk, _, version, run, _ = audit.setup(materials, monkeypatch, pipeline=True)
    with service.db() as s:
        row = s.get(PrototypeScanRun, run["id"])
        row.plan = {**row.plan, "concurrency": lanes}
    for page in run["manifest"]["packages"][0]["pages"]:
        audit.capture(bulk, actor, pid, run, version, page)
    with service.db() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "associate_all").order_by(Job.created_at)))
        from vks_api.association_limits import request_concurrency

        capacity = request_concurrency(lanes)
        for index, job in enumerate(jobs[: capacity + 1]):
            assert job.input_json["pipeline_concurrency"] == lanes
            assert claim(service, s, job) == (index < capacity)
            if index < capacity:
                job.state, job.lease_until = "running", future(60)
            s.flush()


def test_worker_recovery_preserves_already_processed_insufficient_outcomes(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    from vks_api.bulk_association import BulkAssociationService

    bulk = BulkAssociationService(service)
    job = test_bulk_association.enqueue(bulk, actor, pid, aid)
    with service.db() as s:
        row = s.get(Job, job["id"])
        item = row.input_json["items"][0]
        row.stage = "RECOVERED"
        s.add(
            Checkpoint(
                job_id=row.id,
                stage="ASSOCIATE",
                input_hash=item["id"],
                processor_version=row.input_json["processor"],
                result_json={
                    "id": item["id"],
                    "label": item["target"]["label"],
                    "status": "insufficient",
                    "saved": False,
                    "message": "先前已处理的依据不足",
                },
            )
        )
    service.process_job(job["id"])
    done = service.get_job(actor, job["id"])
    assert done["state"] == "succeeded"
    assert done["result"]["completed"][item["id"]]["message"] == "先前已处理的依据不足"
    assert not any(
        c["control"]["selector"] == item["target"]["selector"] and c["control"]["route"] == item["target"]["route"]
        for c in calls
    )


@pytest.mark.parametrize("via_retry", [False, True])
def test_explicit_continuation_changes_scan_and_queued_association_lanes_without_changing_targets(
    materials, monkeypatch, via_retry
):
    service, actor, _, pid, _, bulk, scans, version, run, _ = audit.setup(materials, monkeypatch, pipeline=True)
    with service.db() as s:
        row = s.get(PrototypeScanRun, run["id"])
        row.plan = {**row.plan, "concurrency": 1}
    for page in run["manifest"]["packages"][0]["pages"]:
        audit.capture(bulk, actor, pid, run, version, page)
    with service.db() as s:
        payloads = {j.id: deepcopy(j.input_json) for j in s.scalars(select(Job).where(Job.kind == "associate_all"))}
    if via_retry:
        resumed = scans.retry_ai(actor, pid, run["id"], version, uid(), concurrency=3)
    else:
        resumed = scans.resume(
            actor, pid, run["id"], ScanResume(client_id="changed-lanes", pipeline=True, concurrency=3), version, uid()
        )
    assert resumed["concurrency"] == 3 and resumed["pages_completed"] == 3
    with service.db() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "associate_all").order_by(Job.created_at)))
        assert {j.id: j.input_json for j in jobs} == payloads
        for index, job in enumerate(jobs[:4]):
            assert claim(service, s, job)
            job.state, job.lease_until = "running", future(60)
            s.flush()


def test_continuation_cannot_reconfigure_live_legacy_requests(materials, monkeypatch):
    service, actor, _, pid, _, _, scans, version, run, _, job, _ = partial_with_batch(materials, monkeypatch)
    with service.db() as s:
        row = s.get(Job, job["id"])
        row.input_json = {**row.input_json, "concurrency": 1}
    with pytest.raises(DomainError, match="在途"):
        scans.resume(actor, pid, run["id"], ScanResume(client_id="changed-lanes", concurrency=3), version, uid())
    old_client = scans.resume(actor, pid, run["id"], ScanClient(client_id="old-client"), version, uid())
    assert old_client["concurrency"] == 1


def test_recovery_after_explicit_retry_keeps_retrying_old_insufficient_targets(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    from vks_api.bulk_association import BulkAssociationService

    bulk = BulkAssociationService(service)
    job = test_bulk_association.enqueue(bulk, actor, pid, aid)
    with service.db() as s:
        row = s.get(Job, job["id"])
        item = deepcopy(row.input_json["items"][0])
        row.state, row.attempt = "cancelled", 1
        s.add(
            Checkpoint(
                job_id=row.id,
                stage="ASSOCIATE",
                input_hash=item["id"],
                processor_version=row.input_json["processor"],
                result_json={
                    "id": item["id"],
                    "label": item["target"]["label"],
                    "status": "insufficient",
                    "saved": False,
                    "message": "旧轮次依据不足",
                    "attempt": 1,
                },
            )
        )
    test_bulk_association.enqueue(bulk, actor, pid, aid, resume=job["id"], concurrency=3)
    with service.db() as s:
        row = s.get(Job, job["id"])
        assert row.result_json["processing_epoch"] == 2
        row.stage, row.attempt = "RECOVERED", 2
    service.process_job(job["id"])
    assert any(c["control"]["selector"] == item["target"]["selector"] for c in calls)
    assert service.get_job(actor, job["id"])["result"]["completed"][item["id"]]["message"] != "旧轮次依据不足"


def test_adopted_legacy_progress_updates_without_waiting_for_dispatch_reconciliation(materials, monkeypatch):
    service, actor, _, pid, aid, _, scans, version, run, _, job, payload = partial_with_batch(materials, monkeypatch)
    with service.db() as s:
        s.get(Job, job["id"]).result_json = {"processed": 1, "saved": 1, "pending": 0, "total": len(payload["items"])}
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="progress-client", pipeline=True), version, uid()
    )
    assert resumed["ai"]["processed"] == 1 and resumed["ai"]["saved"] == 1
    assert resumed["ai"]["running"] <= 3
    with service.db() as s:
        s.get(Job, job["id"]).result_json = {"processed": 2, "saved": 1, "pending": 1, "total": len(payload["items"])}
    latest = scans.status(actor, pid, aid)["run"]["ai"]
    assert latest["processed"] == 2 and latest["completed"] == 1 and latest["failed"] == 1


def test_stopped_legacy_targets_can_retry_in_same_pipeline_without_duplicate_old_work(materials, monkeypatch):
    service, actor, _, pid, _, _, scans, version, run, _, job, payload = partial_with_batch(materials, monkeypatch)
    scans.resume(actor, pid, run["id"], ScanClient(client_id="retry-legacy", pipeline=True), version, uid())
    with service.db() as s:
        s.get(Job, job["id"]).state = "cancelled"
    resumed = scans.retry_ai(actor, pid, run["id"], version, uid(), concurrency=3)
    assert resumed["ai"]["queued"] == len(payload["items"]) and resumed["ai"]["cancelled"] == 0
    with service.db() as s:
        assert s.get(Job, job["id"]).input_json == payload
        assert s.get(Job, job["id"]).state == "cancelled"
