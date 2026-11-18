"""Recall limits retain specific sibling chapters without changing vector identity."""

from array import array

from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.service import canonical, sha
from vks_api.vector_rag import RagSettings

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_recall_limits_can_change_without_rebuilding_vectors(assistant):
    (_, service, owner, pid, _, _, _, _, _), _, _ = assistant
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(dimensions=64, candidate_limit=12, per_document_limit=3))
    first = engine.rag.config(owner, pid)[2]
    engine.rag.save(owner, pid, RagSettings(dimensions=64, candidate_limit=24, per_document_limit=8))
    assert engine.rag.config(owner, pid)[2] == first
    assert engine.rag.store.settings(pid).candidate_limit == 24


def test_vector_fusion_keeps_more_than_three_relevant_sections_from_one_manual(assistant, monkeypatch):
    (_, service, owner, pid, _, _, _, _, _), body, _ = assistant
    engine = InteractiveAssistant(service)
    settings = RagSettings(dimensions=64, candidate_limit=24, per_document_limit=8)
    rows = [
        {
            "revision_id": f"chapter-{i}",
            "document_revision_id": "manual",
            "document_name": "完整业务手册",
            "name": f"具体规则 {i}",
            "content": f"规则{i}：金额=单价×月数。",
        }
        for i in range(10)
    ]
    matches = []
    for i, row in enumerate(rows):
        expected = row["document_name"] + "\n" + row["name"] + "\n" + row["content"]
        matches.append(
            (sha(canonical([row["revision_id"], 0, expected])), row["revision_id"], 0, row["content"], 0.9 - i * 0.01)
        )
    monkeypatch.setattr(engine.rag, "config", lambda *a: (settings, {}, "same-vector-space"))
    monkeypatch.setattr(engine.rag.store, "has_vectors", lambda *a: True)
    monkeypatch.setattr(engine.rag.store, "query_vector", lambda *a: array("f", [1.0] + [0.0] * 63).tobytes())
    monkeypatch.setattr(engine.rag.vector_search, "snapshot", lambda *a: None)
    limits = []
    monkeypatch.setattr(engine.rag.vector_search, "nearest", lambda *a, **kw: limits.append(kw["limit"]) or matches)
    form = AssistanceInput.model_validate(body)
    result = engine.rag.hybrid(owner, pid, form, rows, "corpus", [])
    assert len(result) == 8 and limits == [48]
    settings.per_document_limit = 5
    assert len(engine.rag.hybrid(owner, pid, form, rows, "corpus", [])) == 5
