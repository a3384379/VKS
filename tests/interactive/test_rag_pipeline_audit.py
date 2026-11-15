"""Separate pipeline assistants share cold-index work and waiting fences."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest
from vks_api import ai_gateway, vector_rag
from vks_api.interactive_ai import InteractiveAssistant
from vks_api.service import DomainError, Service

from tests.interactive import test_rag_export

workspace = test_rag_export.workspace
vectors = test_rag_export.vectors


def test_three_independent_assistants_do_not_embed_the_same_cold_chunks_three_times(vectors, monkeypatch):
    service, actor, pid, aid, first, calls = vectors
    rags = [first, InteractiveAssistant(service).rag, InteractiveAssistant(service).rag]
    barrier, entered, release, duplicate = Barrier(3), Event(), Event(), Event()
    original, lock, sent = ai_gateway.request_model, Lock(), []

    def provider(config, payload):
        with lock:
            sent.extend(payload["input"])
            if len(sent) != len(set(sent)):
                duplicate.set()
        entered.set()
        assert release.wait(5), "the synthetic index request must be released"
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", provider)

    def build(rag):
        barrier.wait(timeout=5)
        return rag.build_documents(actor, pid, analysis_set_id=aid)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(build, rag) for rag in rags]
        try:
            assert entered.wait(3)
            assert not duplicate.wait(0.4), "parallel assistants sent duplicate chunks before the first response"
        finally:
            release.set()
        coverage = [future.result(timeout=5) for future in futures]
    assert all(result["complete"] for result in coverage)
    assert len(sent) == len(set(sent)) and len(calls) == 1


def test_a_cancelled_index_waiter_leaves_without_sending_its_own_model_request(vectors, monkeypatch):
    service, actor, pid, aid, first, calls = vectors
    second = InteractiveAssistant(service).rag
    entered, release, waiting, cancelled = Event(), Event(), Event(), Event()
    original = ai_gateway.request_model

    def provider(config, payload):
        entered.set()
        assert release.wait(5)
        return original(config, payload)

    def check():
        waiting.set()
        if cancelled.is_set():
            raise DomainError("VKS_CANCELLED", "synthetic cancelled waiter", 409)

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first.build_documents, actor, pid, analysis_set_id=aid)
        try:
            assert entered.wait(3)
            waiter = pool.submit(second.build_documents, actor, pid, analysis_set_id=aid, validate_scope=check)
            assert waiting.wait(2)
            cancelled.set()
            with pytest.raises(DomainError, match="synthetic cancelled waiter"):
                waiter.result(timeout=2)
            assert not owner.done(), "the cancelled waiter must not wait for the network owner"
        finally:
            release.set()
        assert owner.result(timeout=5)["complete"]
    assert len(calls) == 1


def test_separate_services_share_a_durable_cold_index_claim(vectors, monkeypatch):
    service, actor, pid, aid, first, calls = vectors
    other = Service(service.data_dir)
    entered, release, duplicate = Event(), Event(), Event()
    original, lock, sent = ai_gateway.request_model, Lock(), []

    def provider(config, payload):
        with lock:
            sent.extend(payload["input"])
            if len(sent) != len(set(sent)):
                duplicate.set()
        entered.set()
        assert release.wait(5)
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(first.build_documents, actor, pid, analysis_set_id=aid)
            try:
                assert entered.wait(3)
                second = pool.submit(InteractiveAssistant(other).rag.build_documents, actor, pid, analysis_set_id=aid)
                assert not duplicate.wait(0.4), "separate services must not both own the same embedding batch"
            finally:
                release.set()
            assert owner.result(timeout=5)["complete"] and second.result(timeout=5)["complete"]
        assert len(calls) == 1
        with first.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM index_build").fetchone()[0] == 0
    finally:
        other.stop_worker()
        other.engine.dispose()


@pytest.mark.parametrize("interruption", ["cancel", "expired_lease"])
def test_cancelled_or_expired_cold_index_owner_cannot_commit_over_the_next_builder(vectors, monkeypatch, interruption):
    service, actor, pid, aid, first, calls = vectors
    other = Service(service.data_dir)
    entered, release, cancelled = Event(), Event(), Event()
    original, lock, sent = ai_gateway.request_model, Lock(), []

    def provider(config, payload):
        with lock:
            sent.append(payload)
            index = len(sent)
        if index == 1:
            entered.set()
            assert release.wait(5)
        return original(config, payload)

    def check():
        if cancelled.is_set():
            raise DomainError("VKS_CANCELLED", "synthetic cancelled index owner", 409)

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(first.build_documents, actor, pid, analysis_set_id=aid, validate_scope=check)
            try:
                assert entered.wait(3)
                if interruption == "cancel":
                    cancelled.set()
                else:
                    with first.store.db() as db:
                        db.execute("UPDATE index_build SET lease_until=0 WHERE project=?", (pid,))
                next_owner = pool.submit(
                    InteractiveAssistant(other).rag.build_documents, actor, pid, analysis_set_id=aid
                )
                if interruption == "expired_lease":
                    # Let the next lease finish before the old response comes
                    # back, proving token fencing rather than timing alone.
                    assert next_owner.result(timeout=3)["complete"]
            finally:
                release.set()
            with pytest.raises(DomainError) as error:
                owner.result(timeout=5)
            assert error.value.code == "VKS_CANCELLED"
            assert next_owner.result(timeout=5)["complete"]
        assert len(sent) == len(calls) == 2
        with first.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM index_build").fetchone()[0] == 0
        assert first.export_index(actor, pid, analysis_set_id=aid, ensure=False)["coverage"]["complete"]
        assert first.store.state(pid)["state"] == "ready", "a late failed owner must not overwrite the new ready state"
        assert "_index_writer" not in first.store.state(pid)
    finally:
        other.stop_worker()
        other.engine.dispose()


def test_a_live_index_owner_renews_its_short_lease_while_the_network_is_waiting(vectors, monkeypatch):
    service, actor, pid, aid, first, calls = vectors
    other = Service(service.data_dir)
    entered, release, duplicate = Event(), Event(), Event()
    original, lock, requests = ai_gateway.request_model, Lock(), []
    monkeypatch.setattr(vector_rag, "_BUILD_LEASE_SECONDS", 1.2)
    monkeypatch.setattr(vector_rag, "_BUILD_HEARTBEAT_SECONDS", 0.1)

    def provider(config, payload):
        with lock:
            requests.append(payload)
            if len(requests) > 1:
                duplicate.set()
        entered.set()
        assert release.wait(5)
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(first.build_documents, actor, pid, analysis_set_id=aid)
            try:
                assert entered.wait(3)
                waiter = pool.submit(InteractiveAssistant(other).rag.build_documents, actor, pid, analysis_set_id=aid)
                assert not duplicate.wait(1.5), "a live owner must retain the lease beyond its initial expiry"
                assert not owner.done() and not waiter.done()
            finally:
                release.set()
            assert owner.result(timeout=5)["complete"] and waiter.result(timeout=5)["complete"]
        assert len(calls) == 1
    finally:
        other.stop_worker()
        other.engine.dispose()


def test_no_build_vector_projection_does_not_wait_for_or_modify_an_active_claim(vectors, monkeypatch):
    service, actor, pid, aid, first, calls = vectors
    entered, release = Event(), Event()
    original = ai_gateway.request_model

    def provider(config, payload):
        entered.set()
        assert release.wait(5)
        return original(config, payload)

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first.build_documents, actor, pid, analysis_set_id=aid)
        try:
            assert entered.wait(3)
            with first.store.db() as db:
                previous = db.execute("SELECT token FROM index_build WHERE project=?", (pid,)).fetchone()
            snapshot = pool.submit(
                InteractiveAssistant(service).rag.export_index, actor, pid, analysis_set_id=aid, ensure=False
            ).result(timeout=2)
            assert not snapshot["coverage"]["complete"] and snapshot["chunks"] == []
            with first.store.db() as db:
                assert db.execute("SELECT token FROM index_build WHERE project=?", (pid,)).fetchone() == previous
            assert not owner.done() and not calls
        finally:
            release.set()
        assert owner.result(timeout=5)["complete"]
    assert len(calls) == 1
