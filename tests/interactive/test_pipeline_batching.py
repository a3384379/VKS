"""Merged page jobs retain immutable operation groups and per-target recovery."""

import json

from sqlalchemy import event, select, update

from vks_api import ai_gateway
from vks_api.association_limits import GLOBAL_REQUEST_LIMIT
from vks_api.bulk_association import BulkAssociationService, InventoryPageInput
from vks_api.jobs import Checkpoint
from vks_api.interactive_ai import SYSTEM
from vks_api.material_parser import inventory_report
from vks_api.models import Job, uid
from vks_api.service import future
from vks_api.prototype_scan import PrototypeScanRun, PrototypeScanService, ScanInput
from vks_api.scan_pipeline import PrototypeScanTarget, claim, reconcile

from tests.interactive import test_bulk_association, test_materials

materials = test_materials.materials


def setup(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, calls, provider = test_bulk_association.setup(materials, monkeypatch)
    bulk, scans = BulkAssociationService(service), PrototypeScanService(service)
    run = scans.start(
        actor,
        pid,
        ScanInput(analysis_set_id=aid, mode="all", client_id="batched-page-owner"),
        str(bulk.inventory_status(actor, pid, aid)["selection_version"]),
        uid(),
    )
    package = run["manifest"]["packages"][0]
    page = package["pages"][0]
    targets = inventory_report(test_bulk_association.SOURCE)["targets"]
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
            unverified_frames=0,
            status="captured",
            scan_run_id=run["id"],
            client_id=run["client_id"],
        ),
        str(run["source_selection_version"]),
        uid(),
    )
    with service.db() as s:
        jobs = list(s.scalars(select(Job).where(Job.input_json["scan_run_id"].as_string() == run["id"])))
        assert len(jobs) == 1 and jobs[0].input_json["batched_page"]
        original = json.loads(json.dumps(jobs[0].input_json))
        jid = jobs[0].id
    return service, actor, pid, aid, bulk, scans, run, jid, original, calls, provider


def test_page_cohort_keeps_group_identity_and_individual_persistent_results(materials, monkeypatch):
    service, actor, pid, aid, bulk, scans, run, jid, planned, calls, provider = setup(materials, monkeypatch)
    assert len(planned["groups"]) == 6 and sum(map(len, planned["groups"])) == 7
    network = []

    def send(config, body):
        network.append(body)
        return provider(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", send)
    service.process_job(jid)
    job = service.get_job(actor, jid)
    assert job["state"] == "succeeded" and job["result"]["saved"] == 7
    assert len(network) == len(calls) == 6
    assert all(body["input"][0]["content"] == SYSTEM for body in network)
    assert all("targets" not in json.loads(body["input"][1]["content"]) for body in network)
    with service.db() as s:
        assert s.get(Job, jid).input_json == planned
        markers = list(s.scalars(select(PrototypeScanTarget).where(PrototypeScanTarget.run_id == run["id"])))
        assert len(markers) == 7 and all(row.state == "complete" for row in markers)
        assert {row.target_id for row in markers} == {key for group in planned["groups"] for key in group}
        assert {row.target_id: row.group_key for row in markers} == {
            item["id"]: item["operation_group"] for item in planned["items"]
        }
        assert len(list(s.scalars(select(Checkpoint).where(Checkpoint.job_id == jid)))) == 7
    before = len(network)
    reconcile(service)
    assert len(network) == before
    current = scans.status(actor, pid, aid)["run"]
    assert current["ai"]["processed"] == current["ai"]["saved"] == 7
    assert current["ai"]["queued"] == current["ai"]["running"] == 0


def test_cancel_after_first_checkpoint_then_retry_retains_saved_target_and_old_input(materials, monkeypatch):
    # Make the cancellation boundary deterministic: one durable checkpoint.
    # Production may acknowledge several independent items in one transaction.
    monkeypatch.setenv("VKS_AI_SAVE_BATCH_SIZE", "1")
    service, actor, pid, aid, bulk, scans, run, jid, planned, calls, provider = setup(materials, monkeypatch)
    stopped = []

    def stop_after_first(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO job_checkpoint") and not stopped:
            stopped.append(True)
            connection.execute(update(Job).where(Job.id == jid).values(cancel_requested=True))

    # Bulk checkpoint writes use SQL DML rather than mapper insert events.
    event.listen(service.engine, "after_cursor_execute", stop_after_first)
    try:
        service.process_job(jid)
    finally:
        event.remove(service.engine, "after_cursor_execute", stop_after_first)
    assert service.get_job(actor, jid)["state"] == "cancelled"
    with service.db() as s:
        preserved = {
            row.input_hash: dict(row.result_json)
            for row in s.scalars(select(Checkpoint).where(Checkpoint.job_id == jid))
        }
        assert preserved and len(preserved) < 7
        assert s.get(Job, jid).input_json == planned
    calls.clear()
    retry = scans.retry_ai(actor, pid, run["id"], str(run["source_selection_version"]), uid())
    assert retry["ai"]["completed"] >= len(preserved)
    with service.db() as s:
        jobs = list(s.scalars(select(Job).where(Job.input_json["scan_run_id"].as_string() == run["id"], Job.id != jid)))
        assert jobs
        assert not ({item["id"] for job in jobs for item in job.input_json["items"]} & preserved.keys())
        ids = [job.id for job in jobs]
    for child in ids:
        service.process_job(child)
    reconcile(service)
    done = scans.status(actor, pid, aid)["run"]
    assert done["ai"]["saved"] == 7 and done["ai"]["processed"] == 7
    with service.db() as s:
        assert s.get(Job, jid).input_json == planned
        assert {
            row.input_hash: dict(row.result_json)
            for row in s.scalars(select(Checkpoint).where(Checkpoint.job_id == jid))
        } == preserved
        assert all(
            row.state == "complete"
            for row in s.scalars(select(PrototypeScanTarget).where(PrototypeScanTarget.run_id == run["id"]))
        )


def test_global_sixteen_request_claim_cap_applies_across_scan_runs(materials, monkeypatch):
    service, actor, pid, _aid, _bulk, _scans, run, jid, _planned, _calls, _provider = setup(materials, monkeypatch)
    with service.db() as s:
        first = s.get(PrototypeScanRun, run["id"])
        other = PrototypeScanRun(
            id=uid(),
            project_id=pid,
            analysis_set_id=first.analysis_set_id,
            created_by=first.created_by,
            mode=first.mode,
            composition=first.composition,
            state="completed",
            client_id="other-run",
            plan=json.loads(json.dumps(first.plan)),
            completed=json.loads(json.dumps(first.completed)),
            current=None,
            batch_job_id=None,
            lease_until=future(60),
        )
        s.add(other)
        s.flush()
        template = json.loads(json.dumps(s.get(Job, jid).input_json))
        for index in range(GLOBAL_REQUEST_LIMIT):
            active = service._new_job(s, actor, pid, "associate_all", template)
            assert claim(service, s, active)
            active.state, active.lease_until = "running", future(60)
            s.flush()
        candidate = service._new_job(s, actor, pid, "associate_all", {**template, "scan_run_id": other.id})
        assert not claim(service, s, candidate), (
            "other run has free per-run capacity but global request slots are occupied"
        )
        active.state, active.lease_until = "succeeded", None
        s.flush()
        assert claim(service, s, candidate)
