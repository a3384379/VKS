"""Full-batch and page workers share a persistent global request reservation."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from sqlalchemy import select

from vks_api.association_limits import (
    GLOBAL_REQUEST_LIMIT,
    active_request_slots,
    claim_full_association,
    request_concurrency,
)
from vks_api.jobs import Checkpoint, DispatchState
from vks_api.models import Job, Outbox
from vks_api.scan_pipeline import claim
from vks_api.service import Service, future

from tests.interactive import test_materials, test_pipeline_batching

materials = test_materials.materials


def full_job(service, s, actor, pid, template, lanes):
    payload = {
        k: v
        for k, v in deepcopy(template).items()
        if k
        not in {
            "scan_pipeline",
            "scan_run_id",
            "pipeline_concurrency",
            "pipeline_group_key",
            "batched_page",
        }
    }
    payload["concurrency"] = lanes
    return service._new_job(s, actor, pid, "associate_all", payload)


def test_full_execute_defers_without_consuming_attempt_or_progress_until_entire_pool_is_free(materials, monkeypatch):
    service, actor, pid, _aid, _bulk, _scans, _run, jid, planned, _calls, _provider = test_pipeline_batching.setup(
        materials, monkeypatch
    )
    with service.db() as s:
        occupied = s.get(Job, jid)
        occupied.state, occupied.lease_until = "running", future(60)
        full = full_job(service, s, actor, pid, planned, lanes=3)
        full_id = full.id
        immutable = deepcopy(full.input_json)
        full.attempt, full.stage, full.progress = 4, "RECOVERED", 37
        progress = {"processing_epoch": 4, "processed": 1, "pending": 1, "saved": 0}
        full.result_json = deepcopy(progress)
        prior_target = next(group[0] for group in planned["groups"] if len(group) == 1)
        checkpoint = {
            "id": prior_target,
            "label": "prior insufficient item",
            "status": "insufficient",
            "saved": False,
            "message": "preserved evidence gap",
            "attempt": 4,
        }
        s.add(
            Checkpoint(
                job_id=full_id,
                stage="ASSOCIATE",
                input_hash=prior_target,
                processor_version="source-controls-v2",
                result_json=deepcopy(checkpoint),
            )
        )
        out = s.scalar(select(Outbox).where(Outbox.job_id == full_id))
        out.dispatched = True
        s.add(DispatchState(job_id=full_id, state="published", next_attempt_at=future(60)))
    service.process_job(full_id)
    with service.db() as s:
        full = s.get(Job, full_id)
        assert full.state == "queued" and full.attempt == 4
        assert full.stage == "RECOVERED" and full.progress == 37
        assert full.input_json == immutable and full.result_json == progress
        assert s.scalar(select(Checkpoint).where(Checkpoint.job_id == full_id)).result_json == checkpoint
        assert not s.scalar(select(Outbox).where(Outbox.job_id == full_id)).dispatched
        assert s.scalar(select(DispatchState).where(DispatchState.job_id == full_id)).state == "pending"
        occupied = s.get(Job, jid)
        occupied.state, occupied.lease_until = "succeeded", None
    service.process_job(full_id)
    result = service.get_job(actor, full_id)
    assert result["state"] == "succeeded" and result["result"]["saved"] == 6
    assert result["result"]["pending"] == 1 and result["result"]["processed"] == 7
    with service.db() as s:
        assert s.get(Job, full_id).input_json == immutable
        assert s.get(Job, full_id).attempt == 5
        assert (
            s.scalar(
                select(Checkpoint).where(Checkpoint.job_id == full_id, Checkpoint.input_hash == prior_target)
            ).result_json
            == checkpoint
        )


def test_mixed_full_and_pipeline_reservations_never_exceed_global_cap(materials, monkeypatch):
    service, actor, pid, _aid, _bulk, _scans, _run, jid, planned, _calls, _provider = test_pipeline_batching.setup(
        materials, monkeypatch
    )
    with service.db() as s:
        full = full_job(service, s, actor, pid, planned, lanes=1)
        assert claim_full_association(service, s, full)
        full.state, full.lease_until = "running", future(60)
        s.flush()
        assert active_request_slots(s) == request_concurrency(1)
        pages = []
        for index in range(GLOBAL_REQUEST_LIMIT - request_concurrency(1)):
            page = service._new_job(s, actor, pid, "associate_all", deepcopy(planned))
            assert claim(service, s, page)
            page.state, page.lease_until = "running", future(60)
            pages.append(page)
            s.flush()
        assert active_request_slots(s) == GLOBAL_REQUEST_LIMIT
        serial = full_job(service, s, actor, pid, planned, lanes=1)
        assert not claim_full_association(service, s, serial)
        extra = service._new_job(s, actor, pid, "associate_all", deepcopy(planned))
        assert not claim(service, s, extra)
        pages[0].state, pages[0].lease_until = "cancelled", None
        s.flush()
        assert claim_full_association(service, s, serial)
        serial.state, serial.lease_until = "running", future(60)
        s.flush()
        assert active_request_slots(s) == GLOBAL_REQUEST_LIMIT
        assert not claim(service, s, extra)


def test_two_sqlite_service_instances_serialize_full_job_claims(materials, monkeypatch):
    service, actor, pid, _aid, _bulk, _scans, _run, _jid, planned, _calls, _provider = test_pipeline_batching.setup(
        materials, monkeypatch
    )
    with service.db() as s:
        ids = [full_job(service, s, actor, pid, planned, lanes=1).id for _ in range(GLOBAL_REQUEST_LIMIT + 2)]
    other = Service(service.data_dir, database_url=str(service.engine.url))
    barrier = Barrier(len(ids))

    def reserve(index):
        current = service if index % 2 else other
        barrier.wait(timeout=10)
        with current.db() as s:
            job = s.get(Job, ids[index])
            admitted = claim_full_association(current, s, job)
            if admitted:
                job.state, job.lease_until = "running", future(60)
            return admitted

    try:
        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            admitted = list(pool.map(reserve, range(len(ids))))
        assert sum(admitted) == GLOBAL_REQUEST_LIMIT
        with service.db() as s:
            assert active_request_slots(s) == GLOBAL_REQUEST_LIMIT
            pending = list(s.scalars(select(Job).where(Job.id.in_(ids), Job.state == "queued")))
            assert len(pending) == 2 and all(job.attempt == 0 for job in pending)
    finally:
        other.engine.dispose()


@pytest.mark.parametrize(
    "lanes,expected", [(1, 1), (2, GLOBAL_REQUEST_LIMIT), (3, GLOBAL_REQUEST_LIMIT), (4, GLOBAL_REQUEST_LIMIT)]
)
def test_scan_setting_has_explicit_bounded_model_request_capacity(lanes, expected):
    assert 1 <= GLOBAL_REQUEST_LIMIT <= 128
    assert request_concurrency(lanes) == expected
