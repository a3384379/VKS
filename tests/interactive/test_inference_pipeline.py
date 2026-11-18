"""I/O slots are independent of slow durable completion, with bounded work."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context

import pytest
from vks_api.inference_pipeline import CURRENT_PIPELINE, InferencePipeline
from vks_api.service import Service


def test_request_slot_released_while_completion_waits_and_context_preserved():
    trace = ContextVar("test_pipeline_trace")
    trace.set("original")
    saving, release = threading.Event(), threading.Event()
    with InferencePipeline(1) as pipeline, ThreadPoolExecutor(max_workers=2) as pool:

        def first():
            assert CURRENT_PIPELINE.get() is pipeline
            assert pipeline.request(lambda: "response") == "response"

            def save():
                assert trace.get() == "original"
                saving.set()
                assert release.wait(5)
                return "durable"

            return pipeline.complete(save)

        pending = pool.submit(copy_context().run, first)
        try:
            assert saving.wait(5)
            # This would block if completion retained the remote slot.
            assert pool.submit(pipeline.request, lambda: "next response").result(timeout=2) == "next response"
        finally:
            release.set()
        assert pending.result(timeout=5) == "durable"
    assert CURRENT_PIPELINE.get() is None


def test_errors_release_capacity_and_writer_survives():
    with InferencePipeline(2) as pipeline:

        def fail():
            raise ValueError("fresh fence rejects stale input")

        for stage in [pipeline.prepare, pipeline.request, pipeline.complete]:
            with pytest.raises(ValueError, match="fresh fence"):
                stage(fail)
            assert stage(lambda: True)


def test_batch_commit_is_durable_and_rejected_item_does_not_rollback_siblings(tmp_path):
    from sqlalchemy import event, text

    service = Service(tmp_path)
    with service.db() as session:
        session.execute(text("CREATE TABLE batch_probe(id INTEGER PRIMARY KEY)"))
    commits = []
    event.listen(service.engine, "commit", lambda _connection: commits.append(True))

    def save(i):
        with service.db() as session:
            session.execute(text("INSERT INTO batch_probe VALUES (:id)"), {"id": i})
            if i == 3:
                raise ValueError("revoked or stale item")
        return i

    with InferencePipeline(8, service=service) as pipeline, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(pipeline.complete, lambda i=i: save(i)) for i in range(8)]
        for i, future in enumerate(futures):
            if i == 3:
                with pytest.raises(ValueError, match="revoked"):
                    future.result(timeout=5)
            else:
                assert future.result(timeout=5) == i
        # A different connection sees every acknowledged item after real commit.
        with service.engine.connect() as connection:
            assert set(connection.execute(text("SELECT id FROM batch_probe")).scalars()) == set(range(8)) - {3}
    assert len(commits) < 8
    service.engine.dispose()


def test_failed_outer_commit_cannot_acknowledge_saved_items(tmp_path, monkeypatch):
    from sqlalchemy import text

    service = Service(tmp_path)
    with service.db() as session:
        session.execute(text("CREATE TABLE batch_probe(id INTEGER PRIMARY KEY)"))
    original = service.engine.dialect.do_commit
    failed = []

    def fail_once(connection):
        if not failed:
            failed.append(True)
            raise OSError("disk commit failed")
        return original(connection)

    monkeypatch.setattr(service.engine.dialect, "do_commit", fail_once)

    def save():
        with service.db() as session:
            session.execute(text("INSERT INTO batch_probe VALUES (1)"))
        return "must not acknowledge"

    with InferencePipeline(2, service=service) as pipeline, pytest.raises(OSError, match="disk commit"):
        pipeline.complete(save)
    with service.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM batch_probe")).scalar_one() == 0
    service.engine.dispose()


def test_read_batches_share_current_snapshot_and_refresh_between_batches(tmp_path):
    from sqlalchemy import text
    from vks_api.reading import ReadingService

    service = Service(tmp_path)
    with service.db() as session:
        session.execute(text("CREATE TABLE read_probe(value INTEGER)"))
        session.execute(text("INSERT INTO read_probe VALUES (1)"))
    barrier = threading.Barrier(8)

    def read():
        with ReadingService(service).db() as session:
            assert session.info["vks_read_only"] and session.info["vks_read_batch"]
            return id(session), session.scalar(text("SELECT value FROM read_probe"))

    with InferencePipeline(8, service=service) as pipeline, ThreadPoolExecutor(max_workers=8) as pool:

        def caller():
            barrier.wait(timeout=5)
            return pipeline.read(read)

        results = [future.result(timeout=5) for future in [pool.submit(caller) for _ in range(8)]]
        assert {value for _identity, value in results} == {1}
        assert len({identity for identity, _value in results}) < 8
        with service.db() as session:
            session.execute(text("UPDATE read_probe SET value=2"))
        assert pipeline.read(read)[1] == 2
    service.engine.dispose()
