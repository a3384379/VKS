"""Captured pages produce immutable model work before scanning finishes."""

import pytest
from sqlalchemy import select
from vks_api.bulk_association import BulkAssociationService
from vks_api.models import Job, Outbox, uid
from vks_api.prototype_scan import PrototypeScanService, ScanClient, ScanInput
from vks_api.scan_pipeline import PrototypeScanTarget, reconcile

from tests.interactive import test_materials, test_runtime_inventory_browser, test_scan_runs

materials = test_materials.materials


def setup(materials, monkeypatch):
    _, service, actor, pid, aid, calls = test_runtime_inventory_browser.setup(materials, monkeypatch)
    scans, bulk = PrototypeScanService(service), BulkAssociationService(service)
    expected = str(bulk.inventory_status(actor, pid, aid)["selection_version"])
    run = scans.start(
        actor, pid, ScanInput(analysis_set_id=aid, mode="all", client_id="pipeline-owner"), expected, uid()
    )
    return service, actor, pid, aid, calls, scans, bulk, run


def children(service, run):
    with service.db() as s:
        return [
            j.id
            for j in s.scalars(select(Job).where(Job.kind == "associate_all"))
            if j.input_json.get("scan_run_id") == run["id"]
        ]


def test_page_ack_queues_model_while_scan_continues_and_original_version_remains_valid(materials, monkeypatch):
    service, actor, pid, aid, calls, scans, bulk, run = setup(materials, monkeypatch)
    assert run["pipeline"] and run["ai"]["targets"] == 0
    first = test_scan_runs.save(bulk, actor, pid, run)
    assert first["scan_run"]["state"] == "running" and not calls
    jobs = children(service, run)
    assert len(jobs) == 2 and first["scan_run"]["ai"]["queued"] == 2
    for jid in jobs:
        service.process_job(jid)
    assert len(calls) == 2
    progressed = scans.status(actor, pid, aid)["run"]
    assert progressed["selection_version"] > run["source_selection_version"]
    assert progressed["ai"]["completed"] == 2 and progressed["ai"]["saved"] == 2
    assert progressed["manifest"]["selection_version"] == run["source_selection_version"]
    final = test_scan_runs.save(bulk, actor, pid, run, 1)
    assert final["scan_run"]["state"] == "completed"
    for jid in children(service, run)[2:]:
        service.process_job(jid)
    assert scans.status(actor, pid, aid)["run"]["ai"]["completed"] == 4
    assert len(calls) == 4


def test_inventory_job_outbox_and_unique_consumption_are_one_transaction(materials, monkeypatch):
    service, actor, pid, _aid, _calls, _scans, bulk, run = setup(materials, monkeypatch)
    original = service._new_job

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic queue admission failure")

    monkeypatch.setattr(service, "_new_job", fail)
    with pytest.raises(RuntimeError):
        test_scan_runs.save(bulk, actor, pid, run)
    monkeypatch.setattr(service, "_new_job", original)
    assert not children(service, run)
    assert PrototypeScanService(service).status(actor, pid, run["analysis_set_id"])["run"]["pages_completed"] == 0
    with service.db() as s:
        assert not list(s.scalars(select(PrototypeScanTarget)))
    test_scan_runs.save(bulk, actor, pid, run)
    test_scan_runs.save(bulk, actor, pid, run)
    ids = children(service, run)
    assert len(ids) == 2
    with service.db() as s:
        assert len(list(s.scalars(select(PrototypeScanTarget)))) == 2
        assert len(list(s.scalars(select(Outbox).where(Outbox.job_id.in_(ids))))) == 2


def test_pause_allows_queued_models_and_failed_page_has_no_phantom_work(materials, monkeypatch):
    service, actor, pid, aid, calls, scans, bulk, run = setup(materials, monkeypatch)
    test_scan_runs.save(bulk, actor, pid, run)
    paused = scans.pause(actor, pid, run["id"], run["client_id"])
    assert paused["state"] == "interrupted"
    for jid in children(service, run):
        service.process_job(jid)
    assert len(calls) == 2
    resumed = scans.resume(
        actor,
        pid,
        run["id"],
        ScanClient(client_id="pipeline-resumed", pipeline=True),
        str(run["source_selection_version"]),
        uid(),
    )
    test_scan_runs.save(bulk, actor, pid, resumed, status="failed")
    current = scans.status(actor, pid, aid)["run"]
    assert current["state"] == "partial" and current["pages_failed"] == 1
    assert current["ai"]["targets"] == 2 and len(children(service, run)) == 2
    reconcile(service)
    assert len(children(service, run)) == 2
