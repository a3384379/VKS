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
