"""Local page association overlaps safely without blocking ordinary dispatch."""

from threading import Event, Lock, Thread
from time import monotonic

import pytest
from vks_api import jobs
from vks_api.association_limits import GLOBAL_REQUEST_LIMIT, request_concurrency
from vks_api.models import Job
from vks_api.service import Service


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VKS_TASK_MODE", "local")
    service = Service(tmp_path)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "pipeline-pool", "name": "扫描关联流水线"}, "project")
    with service.db() as session:
        page_jobs = [
            service._new_job(session, actor, project["id"], "associate_all", {"scan_pipeline": True}).id
            for _ in range(GLOBAL_REQUEST_LIMIT + 1)
        ]
        ordinary = service._new_job(session, actor, project["id"], "index", {}).id
    started = {jid: Event() for jid in [*page_jobs, ordinary]}
    release = {jid: Event() for jid in page_jobs}
    lock, active, calls, peak = Lock(), set(), [], []

    def blocked(_service, jid):
        with lock:
            calls.append(jid)
            if jid in release:
                active.add(jid)
                peak.append(len(active))
        started[jid].set()
        if jid in release:
            assert release[jid].wait(5), "test must release the synthetic model"
        with service.db() as session:
            session.get(Job, jid).state = "succeeded"
        with lock:
            active.discard(jid)

    monkeypatch.setattr(jobs, "execute_job", blocked)
    yield service, page_jobs, ordinary, started, release, calls, peak
    for event in release.values():
        event.set()
    service.stop_worker()
    service.engine.dispose()


def test_model_capacity_overlaps_next_waits_and_regular_job_dispatches(setup):
    service, page_jobs, ordinary, started, release, calls, peak = setup
    before = monotonic()
    jobs.run_pending(service)
    assert monotonic() - before < 2
    assert started[ordinary].is_set()
    for jid in page_jobs[:GLOBAL_REQUEST_LIMIT]:
        assert started[jid].wait(2)
    assert not started[page_jobs[GLOBAL_REQUEST_LIMIT]].is_set() and max(peak) == GLOBAL_REQUEST_LIMIT
    # Repeated ticks cannot build an unbounded executor queue or resend a job.
    jobs.run_pending(service)
    assert len(calls) == GLOBAL_REQUEST_LIMIT + 1
    first = page_jobs[0]
    release[first].set()
    service.pipeline_futures[first].result(timeout=2)
    jobs.run_pending(service)
    assert started[page_jobs[GLOBAL_REQUEST_LIMIT]].wait(2)
    assert max(peak) == GLOBAL_REQUEST_LIMIT and all(calls.count(jid) == 1 for jid in [*page_jobs, ordinary])


def test_shutdown_drains_started_models_and_leaves_unstarted_jobs_durable(setup):
    service, page_jobs, _, started, release, calls, _ = setup
    jobs.run_pending(service)
    for jid in page_jobs[:GLOBAL_REQUEST_LIMIT]:
        assert started[jid].wait(2)
    stopped = Event()
    thread = Thread(target=lambda: (service.stop_worker(), stopped.set()))
    thread.start()
    try:
        assert service.stop.wait(2)
        assert not stopped.wait(0.05)
        jobs.run_pending(service)
        assert not started[page_jobs[GLOBAL_REQUEST_LIMIT]].is_set()
        for event in release.values():
            event.set()
        assert stopped.wait(2)
    finally:
        for event in release.values():
            event.set()
        thread.join(timeout=3)
    with service.db() as session:
        assert session.get(Job, page_jobs[GLOBAL_REQUEST_LIMIT]).state == "queued"
        assert all(session.get(Job, jid).state == "succeeded" for jid in page_jobs[:GLOBAL_REQUEST_LIMIT])
    assert service.pipeline_executor is None and not service.pipeline_futures
    service.stop.clear()
    jobs.run_pending(service)
    assert started[page_jobs[GLOBAL_REQUEST_LIMIT]].wait(2)
    service.pipeline_futures[page_jobs[GLOBAL_REQUEST_LIMIT]].result(timeout=2)
    assert all(calls.count(jid) == 1 for jid in page_jobs)


def test_external_and_broker_modes_do_not_start_local_models(setup, monkeypatch):
    service, page_jobs, _, started, _, _, _ = setup
    monkeypatch.setenv("VKS_TASK_MODE", "external")
    jobs.run_pending(service)
    assert service.pipeline_executor is None and not any(started[jid].is_set() for jid in page_jobs)
    published = []
    monkeypatch.setenv("VKS_TASK_MODE", "celery")
    monkeypatch.setattr(jobs, "dispatch_pending", lambda _service: published.append(True))
    jobs.run_pending(service)
    assert published == [True] and service.pipeline_executor is None


def test_preview_batch_uses_model_pool_and_does_not_block_regular_dispatch(setup):
    service, page_jobs, ordinary, started, _release, calls, _ = setup
    with service.db() as s:
        for jid in page_jobs:
            s.get(Job, jid).state = "cancelled"
        preview = s.get(Job, page_jobs[0])
        preview.kind = "reassociation_preview"
        preview.input_json = {"pipeline_concurrency": 1}
        preview.state = "queued"
    jobs.run_pending(service)
    assert started[page_jobs[0]].wait(2) and started[ordinary].is_set()
    assert set(calls) == {page_jobs[0], ordinary}


def test_live_dispatch_claims_preview_added_during_slow_migration(setup):
    service, page_jobs, ordinary, started, release, calls, _ = setup
    with service.db() as s:
        for jid in page_jobs:
            s.get(Job, jid).state = "cancelled"
        s.get(Job, ordinary).kind = "material_migrate"
    release[ordinary] = Event()
    service.start_worker()
    try:
        assert started[ordinary].wait(2)
        with service.db() as s:
            preview = s.get(Job, page_jobs[0])
            preview.kind = "reassociation_preview"
            preview.input_json = {"pipeline_concurrency": 1}
            preview.state = "queued"
        assert started[page_jobs[0]].wait(2)
        assert not release[ordinary].is_set()
        assert calls.count(page_jobs[0]) == 1
    finally:
        release[ordinary].set()
        release[page_jobs[0]].set()
        service.stop_worker()


@pytest.mark.parametrize("lanes", [1, 4])
def test_pipeline_pool_uses_the_configured_shared_lane_count(setup, lanes):
    service, page_jobs, ordinary, started, _, calls, peak = setup
    with service.db() as session:
        for jid in page_jobs:
            row = session.get(Job, jid)
            row.input_json = {**row.input_json, "pipeline_concurrency": lanes}
    jobs.run_pending(service)
    capacity = request_concurrency(lanes)
    for jid in page_jobs[:capacity]:
        assert started[jid].wait(2)
    assert not any(started[jid].is_set() for jid in page_jobs[capacity:])
    assert max(peak) == capacity and started[ordinary].is_set()
    jobs.run_pending(service)
    assert len(calls) == capacity + 1
