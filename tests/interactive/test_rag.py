"""Real vector persistence and scoped hybrid search; external embeddings substituted."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from vks_api import ai_gateway
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.vector_rag import RagSettings

from tests.interactive import test_assistant, test_knowledge
from tests.interactive.test_knowledge import root
from tests.prototype.test_v3_workflow import _headers

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
assistant = test_assistant.assistant


def test_local_retrieval_works_without_model_and_never_calls_provider(interactive, monkeypatch):
    web, _, _, pid, aid, grant, package, section, _ = interactive

    def forbidden(*args):
        raise AssertionError("local lookup called a model")

    monkeypatch.setattr(ai_gateway, "request_model", forbidden)
    body = {
        k: v
        for k, v in test_knowledge.form(aid, package, section).items()
        if k not in ("point", "section_revision_ids")
    }
    body["context"] = {"page_title": section["name"]}
    r = web.post(root(pid) + "/related", json=body, headers=_headers(grant, 0, "local"))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sections"] and not r.json()["data"]["model_available"]
    assert r.json()["data"]["origin"] == "retrieval"
    assert not web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")
    ).json()["data"]


def test_dense_vectors_recall_without_keyword_match_and_persist(assistant, monkeypatch):
    (web, service, owner, pid, _aid, grant, _, _, _), body, calls = assistant
    embedding_calls = []

    def embeddings(config, payload):
        assert config["protocol"] == "embeddings"
        embedding_calls.append(payload)
        return {
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * (payload["dimensions"] - 1)}
                for i, _ in enumerate(payload["input"])
            ],
            "usage": {"total_tokens": len(payload["input"]) * 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    form = AssistanceInput.model_validate(body)
    engine.rag._build(owner, pid, form)
    status = engine.rag.status(owner, pid, form)
    assert status["state"] == "ready" and status["indexed_chunks"] > 0, status
    original_state = engine.rag.store.state
    reads = 0

    def finished_during_status(pid, data=None):
        nonlocal reads
        if data is not None:
            return original_state(pid, data)
        reads += 1
        result = original_state(pid)
        return {**result, "state": "building"} if reads == 1 else result

    with monkeypatch.context() as patch:
        patch.setattr(engine.rag.store, "state", finished_during_status)
        assert engine.rag.status(owner, pid, form)["state"] == "ready"
    before = len(embedding_calls)
    module = AssistanceInput.model_validate(
        {
            **body,
            "target": {**body["target"], "selector": "body", "route": "/page.html"},
            "context": {"scope": "module", "page_title": "Synthetic module"},
        }
    )
    module_state = engine.prepare(owner, pid, module)
    assert engine.rag.hybrid(owner, pid, module, module_state[1], module_state[2], [])
    assert len(embedding_calls) == before
    unrelated = {
        **body,
        "target": {**body["target"], "label": "quuxzsemantic"},
        "context": {"page_title": "", "section_title": "", "nearby_labels": []},
    }
    initial = web.post(root(pid) + "/related", json=unrelated, headers=_headers(grant, 0, "local"))
    assert not initial.json()["data"]["sections"]
    answer = web.post(root(pid) + "/semantic", json=unrelated, headers=_headers(grant, 0, "semantic"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["data"]["retrieval"] == "hybrid" and answer.json()["data"]["sections"]
    assert len(embedding_calls) == before + 1
    # A fresh assistant/store reuses both corpus vectors and cached query vectors.
    fresh = InteractiveAssistant(service)
    state = fresh.prepare(owner, pid, AssistanceInput.model_validate(unrelated))
    result = fresh.rag.hybrid(owner, pid, AssistanceInput.model_validate(unrelated), state[1], state[2], [])
    assert result and len(embedding_calls) == before + 1
    # Embedding accounting does not consume the answer model's separate ledger.
    assert status["used_tokens"] > 0
    assert not calls
    # Cached vectors cannot reveal a chapter after its source permission changes.
    from vks_api.models import Artifact, Revision

    with service.db() as db:
        chapter = db.get(Revision, result[0]["revision_id"])
        artifact = db.get(Artifact, chapter.artifact_id)
        artifact.metadata_json = {**artifact.metadata_json, "requires_source_read": True}
    hidden = web.post(root(pid) + "/semantic", json=unrelated, headers=_headers(grant, 0, "revoked"))
    assert hidden.status_code in (200, 404), hidden.text
    if hidden.status_code == 200:
        assert not hidden.json()["data"]["sections"]
    assert len(embedding_calls) == before + 1
    denied = web.post(
        root(pid) + "/rag/settings", json={"enabled": True}, headers=_headers(grant, 0, "settings-denied")
    )
    assert denied.status_code == 403


def test_distinct_targets_do_not_queue_behind_one_model_lock(assistant, monkeypatch):
    (web, _, _, pid, aid, grant, _, _, _), body, _ = assistant
    barrier = threading.Barrier(2)

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        barrier.wait(timeout=4)
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)

    def run(i):
        form = {**body, "target": {**body["target"], "selector": f"#target{i}"}}
        return web.post(root(pid) + "/assist", json=form, headers=_headers(grant, 0, f"parallel{i}"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1, 2]))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert (
        len(
            web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")).json()[
                "data"
            ]
        )
        == 2
    )


def test_module_query_cache_isolated_between_prototypes_with_same_route(assistant, monkeypatch):
    from vks_api.interactive import InteractiveKnowledgeService
    from vks_api.materials import ActivateMaterials, MaterialService, PrepareMaterial
    from vks_api.models import uid

    from tests.prototype.test_v3_workflow import _upload

    (_web, service, owner, pid, aid, _grant, _package, _section, _raw), body, _calls = assistant
    calls = []

    def embed(config, payload):
        calls.extend(payload["input"])
        return {
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * (payload["dimensions"] - 1)}
                for i, _ in enumerate(payload["input"])
            ],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embed)
    manager = MaterialService(service)
    manager.open(owner, pid, aid)

    def version():
        return str(InteractiveKnowledgeService(service).catalog(owner, pid, aid)["selection_version"])

    upload = _upload(service, owner, pid, b"<title>Another module</title><button>Another action</button>", "other.html")
    p = manager.prepare(
        owner,
        pid,
        PrepareMaterial(analysis_set_id=aid, kind="prototype", name="Other prototype", artifact_ids=[upload]),
        version(),
        uid(),
    )
    manager.activate(
        owner, pid, ActivateMaterials(analysis_set_id=aid, revision_ids=[p["revision_id"]]), version(), uid()
    )
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    first = AssistanceInput.model_validate(
        {
            **body,
            "target": {**body["target"], "selector": "body", "route": "/", "label": "First module"},
            "context": {"scope": "module"},
        }
    )
    engine.rag._build(owner, pid, first)
    state = engine.prepare(owner, pid, first)
    engine.rag.hybrid(owner, pid, first, state[1], state[2], [])
    before = len(calls)
    second = first.model_copy(
        update={"package_id": p["artifact_id"], "target": first.target.model_copy(update={"label": "Another module"})}
    )
    second_state = engine.prepare(owner, pid, second)
    engine.rag.hybrid(owner, pid, second, second_state[1], second_state[2], [])
    assert len(calls) == before + 1
    assert calls[-1] == "模块：Another module"


def test_questions_use_distinct_query_vectors_and_keep_module_context(assistant, monkeypatch):
    from vks_api.interactive_ai import InteractiveQuestionInput

    (_web, service, owner, pid, _aid, _grant, _package, _section, _), body, _calls = assistant
    queries = []

    def embeddings(config, payload):
        assert config["protocol"] == "embeddings"
        queries.extend(payload["input"])
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 63} for i, _ in enumerate(payload["input"])],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    engine.rag._build(owner, pid, AssistanceInput.model_validate(body))
    for question in ["前提是什么", "后续流程是什么"]:
        form = InteractiveQuestionInput.model_validate(
            {**body, "question": question, "context": {"scope": "module", "page_title": "当前收费模块"}}
        )
        state = engine.prepare(owner, pid, form)
        before = len(queries)
        assert engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
        assert len(queries) == before + 1
        assert question in queries[-1] and "当前收费模块" in queries[-1]
        assert engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
        assert len(queries) == before + 1
    # A distinct free-form question must not retain a permanent thread lock.
    assert not engine.rag.query_locks


def test_vector_store_closes_connections_and_rolls_back_failed_transactions(tmp_path):
    import sqlite3

    import pytest
    from vks_api.vector_rag import VectorStore

    store = VectorStore(tmp_path)
    with store.db() as closed:
        assert closed.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        closed.execute("SELECT 1")
    with pytest.raises(RuntimeError), store.db() as failed:
        failed.execute("INSERT INTO status VALUES (?,?)", ("failed-project", "{}"))
        raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")
    with store.db() as db:
        assert not db.execute("SELECT project FROM status WHERE project=?", ("failed-project",)).fetchone()


def test_hybrid_repairs_bad_query_vectors_and_rejects_corrupt_cached_excerpts(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _), body, _calls = assistant
    queries = []

    def embeddings(config, payload):
        queries.extend(payload["input"])
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 63} for i, _ in enumerate(payload["input"])],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    form = AssistanceInput.model_validate(body)
    engine.rag._build(owner, pid, form)
    state = engine.prepare(owner, pid, form)
    original = engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
    assert original
    before = len(queries)
    with engine.rag.store.db() as db:
        db.execute("UPDATE queries SET embedding=? WHERE project=?", (b"bad-vector", pid))
    assert engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
    assert len(queries) == before + 1
    with engine.rag.store.db() as db:
        db.execute("UPDATE chunks SET content=? WHERE project=?", ("CACHE_CONTENT_NOT_IN_SOURCE", pid))
    assert not engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
    with engine.rag.store.db() as db:
        db.execute("UPDATE chunks SET embedding=? WHERE project=?", (b"bad-vector", pid))
    # Invalid cache data cannot break a query or replace authoritative excerpts.
    assert not engine.rag.hybrid(owner, pid, form, state[1], state[2], [])


def test_embedding_usage_is_recorded_without_daily_token_quota(tmp_path):
    from vks_api.vector_rag import VectorStore, RagSettings

    store = VectorStore(tmp_path)
    settings = RagSettings(daily_token_limit=10000)
    first = store.reserve("project", settings, 30000)
    store.settle(first, 25000, "succeeded")
    with ThreadPoolExecutor(max_workers=3) as pool:
        keys = list(pool.map(lambda _: store.reserve("project", settings, 20000), range(3)))
    for key in keys:
        store.settle(key, 18000, "succeeded")
    with store.db() as db:
        assert db.execute("SELECT SUM(used),SUM(reserved) FROM usage").fetchone() == (79000, 0)
