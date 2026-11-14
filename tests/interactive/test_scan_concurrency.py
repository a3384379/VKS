"""Parallel browser workers preserve scan counts and holder/cancellation fences."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.bulk_association import BulkAssociationService, PrototypeInventoryPage
from vks_api.models import Job, uid
from vks_api.prototype_scan import PrototypeScanRun, PrototypeScanService, ScanClient, ScanInput
from vks_api.service import DomainError

from tests.interactive import test_materials, test_scan_runs
from tests.interactive.test_document_navigation import zip_docs

materials = test_materials.materials


def setup(materials, concurrency=3):
    _, service, actor, manager, projects = materials
    pid, aid = projects[0]
    source = zip_docs([(f"page-{i}.html", f"<title>模块{i}</title><button>新增收费项</button>") for i in range(3)])
    prototype = test_materials.stage(service, actor, manager, pid, aid, "prototype", source, filename="pages.zip")
    document = test_materials.stage(service, actor, manager, pid, aid, "documents", "# 收费项\n\n新增收费项依据。")
    test_materials.activate(service, actor, manager, pid, aid, [prototype, document])
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    scans, bulk = PrototypeScanService(service), BulkAssociationService(service)
    expected = str(bulk.inventory_status(actor, pid, aid)["selection_version"])
    run = scans.start(
        actor,
        pid,
        ScanInput(analysis_set_id=aid, mode="all", client_id="parallel-client", concurrency=concurrency, pipeline=False),
        expected,
        uid(),
    )
    return service, actor, pid, aid, scans, bulk, run


def test_three_simultaneous_page_saves_keep_all_progress_and_replays_do_not_double_count(materials):
    service, actor, pid, aid, scans, bulk, run = setup(materials)
    assert run["concurrency"] == 3 and run["manifest"]["concurrency"] == 3
    barrier = Barrier(3)
    keys = [uid() for _ in range(3)]

    def worker(index):
        barrier.wait(timeout=10)
        return test_scan_runs.save(bulk, actor, pid, run, index, key=keys[index])

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(worker, range(3)))
    current = scans.status(actor, pid, aid)["run"]
    assert current["state"] == "completed" and current["pages_attempted"] == 3
    assert current["pages_completed"] == 3 and current["controls"] == 3
    assert len({r["scan_run"]["row_version"] for r in responses}) == 3
    # Concurrent retries of a prior HTTP request are readbacks of its one
    # transaction, even when its response reached the browser after completion.
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: test_scan_runs.save(bulk, actor, pid, run, 0, key=keys[0]), range(3)))
    assert scans.status(actor, pid, aid)["run"]["pages_completed"] == 3
    with service.db() as s:
        assert len(list(s.scalars(select(PrototypeInventoryPage)))) == 3
        assert len(s.get(PrototypeScanRun, run["id"]).completed) == 3


def test_parallel_late_workers_cannot_save_after_cancel_or_client_takeover(materials):
    _, actor, pid, aid, scans, bulk, run = setup(materials)
    newer = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="new-parallel-owner"), str(run["selection_version"]), uid()
    )

    def rejected(index, plan):
        try:
            test_scan_runs.save(bulk, actor, pid, plan, index)
        except DomainError as error:
            return error.code
        raise AssertionError("A stale browser worker must not write")

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda i: rejected(i, run), range(3))) == ["VKS_STATE_CONFLICT"] * 3
    assert scans.status(actor, pid, aid)["run"]["pages_completed"] == 0
    scans.cancel(actor, pid, run["id"], newer["client_id"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda i: rejected(i, newer), range(3))) == ["VKS_STATE_CONFLICT"] * 3
    assert scans.status(actor, pid, aid)["run"]["pages_completed"] == 0


def test_scan_concurrency_persists_on_resume_and_is_independent_of_ai_concurrency(materials):
    service, actor, pid, _aid, scans, bulk, run = setup(materials, concurrency=4)
    assert run["concurrency"] == 4
    scans.pause(actor, pid, run["id"], run["client_id"])
    resumed = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="resume-four-workers"), str(run["selection_version"]), uid()
    )
    assert resumed["concurrency"] == resumed["manifest"]["concurrency"] == 4
    for i in range(3):
        completed = test_scan_runs.save(bulk, actor, pid, resumed, i)["scan_run"]
    job = test_scan_runs.enqueue(bulk, actor, pid, completed)
    with service.db() as s:
        assert s.get(Job, job["id"]).input_json["concurrency"] == 3
        row = s.get(PrototypeScanRun, run["id"])
        row.batch_job_id = None
        row.state = "interrupted"
        row.plan = {k: v for k, v in row.plan.items() if k != "concurrency"}
    legacy = scans.resume(
        actor, pid, run["id"], ScanClient(client_id="resume-legacy-workers"), str(run["selection_version"]), uid()
    )
    assert legacy["concurrency"] == legacy["manifest"]["concurrency"] == 3
    with service.db() as s:
        assert s.get(PrototypeScanRun, run["id"]).plan["concurrency"] == 3


@pytest.mark.parametrize("value", [0, 5, True, 1.5, "3"])
def test_scan_concurrency_is_bounded_strict_integer(value):
    with pytest.raises(ValidationError):
        ScanInput(analysis_set_id="analysis", client_id="bounded-client", concurrency=value)
