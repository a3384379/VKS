"""Large task progress reads counters without loading private work payloads."""

from sqlalchemy import event
from vks_api.models import Job, uid
from vks_api.prototype_scan import PrototypeScanRun
from vks_api.scan_pipeline import PrototypeScanTarget, summary

from tests.interactive import test_materials, test_scan_runs

materials = test_materials.materials


def test_progress_counts_states_and_keeps_heavy_payloads_out_of_session(materials, monkeypatch):
    _, service, actor, pid, _, _, _, _, run = test_scan_runs.start(materials, monkeypatch)
    states = ["queued", "retry_wait", "running", "cancelling", "succeeded"]
    with service.db() as session:
        jobs = []
        for state in states:
            job = service._new_job(session, actor, pid, "associate_all", {"private_work": "x" * 65536})
            job.state = state
            jobs.append(job.id)
        for index in range(200):
            slot = index % 8
            session.add(
                PrototypeScanTarget(
                    run_id=run["id"],
                    target_id=uid(),
                    group_key=uid(),
                    item={"private_work": "x" * 65536},
                    job_id=jobs[slot] if slot < 5 else None,
                    state="queued" if slot < 4 else ["complete", "failed", "waiting", "cancelled"][slot - 4],
                    outcome={"saved": slot == 4, "message": "待重试" if slot == 5 else "", "detail": "y" * 1024},
                )
            )
    loaded = []

    def observe(row, _context):
        loaded.append(type(row).__name__)

    event.listen(Job, "load", observe)
    event.listen(PrototypeScanTarget, "load", observe)
    try:
        with service.db() as session:
            result = summary(session, session.get(PrototypeScanRun, run["id"]))
        assert result == {
            "targets": 200,
            "completed": 25,
            "failed": 25,
            "saved": 25,
            "waiting": 25,
            "pending_admission": 25,
            "queued": 50,
            "running": 50,
            "cancelled": 25,
            "jobs": 5,
            "error": "待重试",
        }
        assert loaded == [], "polling must not materialize target work or model job inputs"
    finally:
        event.remove(Job, "load", observe)
        event.remove(PrototypeScanTarget, "load", observe)


def test_empty_pipeline_progress_has_zero_counters(materials, monkeypatch):
    _, service, _, _, _, _, _, _, run = test_scan_runs.start(materials, monkeypatch)
    with service.db() as session:
        result = summary(session, session.get(PrototypeScanRun, run["id"]))
    assert result["error"] == ""
    assert all(value == 0 for key, value in result.items() if key != "error")


def test_scan_dto_reads_same_transaction_after_large_sqlite_write(materials, monkeypatch):
    _, service, actor, pid, _, _, scans, _, run = test_scan_runs.start(materials, monkeypatch)

    def bounded_busy_timeout(connection, _record):
        connection.execute("PRAGMA busy_timeout=50")

    service.engine.dispose()
    event.listen(service.engine, "connect", bounded_busy_timeout)
    try:
        with service.db() as session:
            # Force dirty-page spill and an exclusive SQLite lock. A separate
            # nested reader would block its own transaction and fail here.
            session.connection().exec_driver_sql("PRAGMA cache_size=1")
            row = session.get(PrototypeScanRun, run["id"])
            row.plan = {**row.plan, "_large_history": "x" * 1048576}
            session.flush()
            result = scans.dto(session, actor, pid, row)
            assert result["current_scope"] and result["pages_total"] == 2
    finally:
        event.remove(service.engine, "connect", bounded_busy_timeout)
