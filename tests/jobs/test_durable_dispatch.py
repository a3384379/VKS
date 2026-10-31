import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select
from vks_api.jobs import Checkpoint, DispatchState, dispatch_pending, load_parse_checkpoint, reconcile
from vks_api.models import Job
from vks_api.service import DomainError, Service


@pytest.fixture
def setup(tmp_path):
    service = Service(tmp_path)
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "持久任务"}, "project")
    yield (service, owner, project)
    service.engine.dispose()


def past():
    return (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")


def make_due(service, jid):
    with service.db() as s:
        entry = s.scalar(select(DispatchState).where(DispatchState.job_id == jid))
        if entry:
            entry.next_attempt_at = past()


def test_outbox_can_redeliver_but_worker_commits_only_once(setup):
    service, owner, p = setup
    job = service.demo_import(owner, p["id"], "demo")
    sent = []
    assert dispatch_pending(service, lambda jid, kind: sent.append((jid, kind))) == 1
    assert sent == [(job["id"], "import")]
    assert dispatch_pending(service, lambda *args: sent.append(args)) == 0
    make_due(service, job["id"])
    dispatch_pending(service, lambda *args: sent.append(args))
    assert len(sent) == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.process_job(job["id"]), range(2)))
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded" and result["attempt"] == 1
    assert len(service.workspace(owner, p["id"])["analysis_sets"]) == 1


def test_broker_unavailable_backoff_is_bounded_and_explicit_retry_recovers(setup):
    service, owner, p = setup
    job = service.demo_import(owner, p["id"], "demo")

    def unavailable(*args):
        raise ConnectionError("secret://never-logged")

    for number in range(8):
        dispatch_pending(service, unavailable)
        with service.db() as s:
            entry = s.scalar(select(DispatchState).where(DispatchState.job_id == job["id"]))
            assert entry.attempt == number + 1 and entry.last_error_code == "BROKER_UNAVAILABLE"
            assert entry.next_attempt_at > __import__("vks_api.models", fromlist=["now"]).now()
        make_due(service, job["id"])
    failed = service.get_job(owner, job["id"])
    assert failed["state"] == "failed" and failed["error"]["retryable"]
    assert "secret://" not in json.dumps(failed)
    service.retry_job(owner, job["id"])
    sent = []
    dispatch_pending(service, lambda *args: sent.append(args))
    assert sent and sent[0][0] == job["id"]


def test_parse_checkpoint_retries_without_reparsing_and_validates_cache_hash(setup, monkeypatch):
    service, owner, p = setup
    job = service.demo_import(owner, p["id"], "demo")
    commit = service._commit_bundle

    def interrupted(*args, **kwargs):
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "模拟事务提交前失败", 503, retryable=True)

    monkeypatch.setattr(service, "_commit_bundle", interrupted)
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "failed"
    with service.db() as s:
        point = s.scalar(select(Checkpoint).where(Checkpoint.job_id == job["id"], Checkpoint.stage == "PARSE"))
        assert point and "object_key" in point.result_json
        key = point.result_json["object_key"]
        input_hash = point.input_hash
    assert load_parse_checkpoint(service, job["id"], input_hash)
    monkeypatch.setattr(service, "_commit_bundle", commit)
    monkeypatch.setattr(service, "_parse", lambda *a: (_ for _ in ()).throw(AssertionError("不应重复解析")))
    service.retry_job(owner, job["id"])
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "succeeded"
    (service.data_dir / "objects" / key).write_bytes(b'{"malicious":"not a pickle"}')
    assert load_parse_checkpoint(service, job["id"], input_hash) is None


def test_expired_capture_lease_never_replays_a_business_action(setup):
    service, owner, p = setup
    with service.db() as s:
        job = service._new_job(s, owner, p["id"], "capture", {"risk_level": "test_write"})
        job.state = "running"
        job.attempt = 1
        job.lease_until = past()
        jid = job.id
    reconcile(service)
    result = service.get_job(owner, jid)
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_STATE_CONFLICT"
    assert result["error"]["retryable"] is False
    assert service.get_job(owner, jid)["state"] == "failed"
    restarted = service.retry_job(owner, jid)
    assert restarted["state"] == "queued"


def test_distributed_transient_task_retry_stops_after_three_attempts(setup, monkeypatch):
    service, owner, p = setup
    job = service.demo_import(owner, p["id"], "demo")
    monkeypatch.setenv("VKS_TASK_MODE", "celery")
    monkeypatch.setattr(
        service,
        "_parse",
        lambda *a: (_ for _ in ()).throw(DomainError("VKS_DEPENDENCY_UNAVAILABLE", "瞬态错误", 503, retryable=True)),
    )
    for attempt in range(1, 4):
        service.process_job(job["id"])
        result = service.get_job(owner, job["id"])
        assert result["attempt"] == attempt
        assert result["state"] == ("retry_wait" if attempt < 3 else "failed")
        if attempt < 3:
            with service.db() as s:
                s.get(Job, job["id"]).lease_until = past()
            reconcile(service)
    assert service.get_job(owner, job["id"])["error"]["retryable"]


@pytest.mark.parametrize("revoked", [False, True])
def test_real_parser_cooperatively_stops_before_new_work_after_cancel_or_revocation(setup, monkeypatch, revoked):
    from threading import Event
    import time
    import packages.analyzers.vks_ingest as ingest

    service, owner, project = setup
    token = service.create_token(
        owner, project["id"], {"name": "processor", "scopes": ["knowledge:read", "source:import"]}
    )
    principal = service.authenticate(token["token"], "token")
    job = service.demo_import(principal, project["id"], "cancel-parser")
    started, resume = Event(), Event()
    completed = []
    read_zip = ingest._read_zip

    def delayed(*args, **kwargs):
        started.set()
        assert resume.wait(timeout=5)
        # The processor polls at a bounded 100 ms interval; advance past that
        # interval before entering its real per-entry ZIP checkpoints.
        time.sleep(0.12)
        value = read_zip(*args, **kwargs)
        completed.append(True)
        return value

    monkeypatch.setattr(ingest, "_read_zip", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.process_job, job["id"])
        assert started.wait(timeout=5)
        before = time.monotonic()
        try:
            if revoked:
                service.revoke_token(owner, project["id"], token["id"])
            else:
                service.cancel_job(owner, job["id"])
        finally:
            resume.set()
        future.result(timeout=3)
        assert time.monotonic() - before < 2
    result = service.get_job(owner, job["id"])
    assert result["state"] == ("failed" if revoked else "cancelled")
    assert result["error"]["code"] == ("VKS_UNAUTHENTICATED" if revoked else "VKS_CANCELLED")
    assert not completed and not result.get("result")
    assert service.workspace(owner, project["id"])["analysis_sets"] == []
    with service.db() as session:
        assert session.scalar(select(Checkpoint).where(Checkpoint.job_id == job["id"])) is None
