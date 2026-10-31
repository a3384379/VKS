import pytest
from sqlalchemy import delete, func, select

from test_journeys import store as store  # pytest fixture registration
from vks_api.jobs import Checkpoint
from vks_api.models import Job
from vks_api.service import DomainError, future
from vks_api.source_cache import SourceUnitCache, STAGES


def running(service, actor, pid, aid, key):
    job = (
        service.create_analysis_job(actor, aid, {"kind": "index"}, key) if aid else service.demo_import(actor, pid, key)
    )
    with service.db() as db:
        saved = db.get(Job, job["id"])
        saved.state, saved.attempt, saved.lease_until = "running", 1, future(60)
    return SourceUnitCache(service, job["id"], 1)


def test_buffer_cannot_flush_after_cancellation_or_new_attempt(store):
    service, actor, pid, w, _, _ = store
    for invalid in ("cancel", "attempt"):
        cache = running(service, actor, pid, w["selected_analysis_set"]["id"], invalid)
        cache.store("semantic", invalid, "test", {"payload": "private-data"})
        with service.db() as db:
            saved = db.get(Job, cache.jid)
            if invalid == "cancel":
                saved.cancel_requested = True
            else:
                saved.attempt += 1
        with pytest.raises(DomainError):
            cache.flush()
        with service.db() as db:
            assert not db.scalar(select(Checkpoint.id).where(Checkpoint.job_id == cache.jid))


def test_quota_eviction_is_project_scoped_and_keeps_task_artifacts(store, monkeypatch):
    from vks_api import source_cache

    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    other_pid = service.create_project(actor, dict(key="other-cache", name="Other cache"), "other-cache")["id"]
    other = running(service, actor, other_pid, None, "other-cache-job")
    other.store("semantic", "same-key", "test", {"payload": "other-project"})
    other.flush()
    with service.db() as db:
        db.execute(
            delete(Checkpoint).where(
                Checkpoint.job_id.in_(select(Job.id).where(Job.project_id == pid)),
                Checkpoint.stage.in_(list(STAGES.values())),
            )
        )
    own = running(service, actor, pid, aid, "quota-cache")
    assert own.load("semantic", "same-key", "test") is None
    monkeypatch.setattr(source_cache, "MAX_PROJECT_UNITS", 2)
    monkeypatch.setattr(source_cache, "MAX_PROJECT_BYTES", 1000)
    for index in range(3):
        own.store("semantic", str(index), "test", {"payload": "x" * 100})
    own.flush()
    with service.db() as db:
        assert db.scalar(select(func.count()).select_from(Checkpoint).where(Checkpoint.job_id == own.jid)) == 2
        assert db.scalar(select(Checkpoint.id).where(Checkpoint.stage == "PARSE"))
    assert other.load("semantic", "same-key", "test")["payload"] == "other-project"
    monkeypatch.setattr(source_cache, "MAX_PROJECT_BYTES", 1)
    own.store("semantic", "larger", "test", {"payload": "x" * 100})
    own.flush()
    with service.db() as db:
        assert not db.scalar(select(Checkpoint.id).where(Checkpoint.job_id == own.jid))
    assert other.load("semantic", "same-key", "test")["payload"] == "other-project"
