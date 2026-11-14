"""Sparse retrieval preserves exhaustive BM25 ordering and one-snapshot reads."""

import math
import random
from collections import Counter

from vks_api.interactive_ai import AssistanceInput, ChapterIndex, InteractiveAssistant, tokens

from tests.interactive import test_assistant
from tests.interactive.test_knowledge import current, form, root
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def exhaustive(chapters, target, context, preferred):
    """The previous exhaustive BM25 formula is the behavioral reference."""
    words = [tokens(c["document_name"] + " " + c["name"] + " " + c["content"]) for c in chapters]
    counts = Counter(t for terms in words for t in terms)
    idf = {t: math.log(1 + (len(chapters) - n + 0.5) / (n + 0.5)) for t, n in counts.items()}
    lengths = [sum(terms.values()) for terms in words]
    average = sum(lengths) / max(1, len(chapters))
    query = tokens(
        " ".join(
            [target.label] * 3
            + [context.page_title] * 2
            + [context.section_title] * 2
            + (context.nearby_labels if context.scope == "control" else [])
        )
    )
    ranked = []
    for row, terms, length in zip(chapters, words, lengths, strict=True):
        score = sum(
            idf.get(t, 0)
            * min(w, 3)
            * terms.get(t, 0)
            * 2.2
            / (terms.get(t, 0) + 1.2 * (0.25 + 0.75 * length / max(1, average)))
            for t, w in query.items()
        )
        score += 20 if row["revision_id"] in preferred else 0
        score += 5 if len(target.label) > 1 and target.label in row["name"] else 0
        if score > 0:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], item[1]["revision_id"]))
    result, per_doc = [], Counter()
    for _, row in ranked:
        if per_doc[row["document_revision_id"]] >= 3:
            continue
        per_doc[row["document_revision_id"]] += 1
        content = row["content"]
        positions = [content.lower().find(t) for t in query if content.lower().find(t) >= 0]
        start = max(0, min(positions, default=0) - 100)
        result.append({**row, "content": content[start : start + 650]})
        if len(result) == 6:
            break
    return result


def test_sparse_retrieval_matches_exhaustive_order_and_source_snippets():
    randomizer = random.Random(20261114)
    vocabulary = ["charge", "refund", "invoice", "retry", "parking", "收费标准", "自动账单", "临时停车", "知识帮助"]
    chapters = [
        {
            "revision_id": f"revision-{i:04d}",
            "document_revision_id": f"document-{i // 5:04d}",
            "document_name": "业务说明",
            "name": f"{vocabulary[i % len(vocabulary)]} / x+ / {i % 4}",
            "content": "Introduction. "
            + " ".join(randomizer.choices(vocabulary, k=30 + i % 15))
            + "\n"
            + "source context " * 70,
        }
        for i in range(160)
    ]
    index = ChapterIndex(chapters)
    for scope in ("control", "module"):
        for label in vocabulary + ["missingterm", "x+", "水", ""]:
            target = type("Target", (), {"label": label})()
            context = type(
                "Context",
                (),
                {"scope": scope, "page_title": "invoice", "section_title": "", "nearby_labels": ["临时停车"]},
            )()
            for preferred in (set(), {chapters[30]["revision_id"], chapters[50]["revision_id"]}):
                assert index.search(target, context, preferred) == exhaustive(chapters, target, context, preferred)
    empty = type("Context", (), {"scope": "module", "page_title": "", "section_title": "", "nearby_labels": []})()
    # A punctuation-only label still retains the original literal-name bonus.
    assert index.search(type("Target", (), {"label": "x+"})(), empty, set()) == exhaustive(
        chapters, type("Target", (), {"label": "x+"})(), empty, set()
    )
    assert ChapterIndex([]).search(type("Target", (), {"label": "query"})(), empty, set()) == []


def test_related_reuses_one_read_snapshot_for_preparation_and_binding(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _raw), body, calls = assistant
    engine = InteractiveAssistant(service)
    prepare, binding = engine.prepare, engine._binding
    sessions = []

    def prepare_once(*args, **kwargs):
        sessions.append(("prepare", kwargs.get("session")))
        return prepare(*args, **kwargs)

    def binding_once(*args, **kwargs):
        sessions.append(("binding", kwargs.get("session")))
        return binding(*args, **kwargs)

    monkeypatch.setattr(engine, "prepare", prepare_once)
    monkeypatch.setattr(engine, "_binding", binding_once)
    result = engine.related(owner, pid, AssistanceInput.model_validate(body))
    assert result["sections"] and not calls
    assert [operation for operation, _ in sessions] == ["prepare", "binding"]
    assert sessions[0][1] is not None and sessions[0][1] is sessions[1][1]
    assert not sessions[0][1].info


def test_semantic_saved_binding_does_not_prepare_the_corpus_twice(assistant, monkeypatch):
    (web, _service, _owner, pid, aid, grant, package, section, _raw), body, calls = assistant
    v = current(web, pid, aid, grant)["selection_version"]
    saved = web.post(root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, v, "binding"))
    assert saved.status_code == 200, saved.text
    original = InteractiveAssistant.prepare
    preparations = []

    def prepare_once(self, *args, **kwargs):
        preparations.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InteractiveAssistant, "prepare", prepare_once)
    result = web.post(root(pid) + "/semantic", json=body, headers=_headers(grant, 0, "semantic"))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["origin"] == "binding" and result.json()["data"]["sections"]
    assert len(preparations) == 1 and not calls


def test_tokenization_keeps_ascii_and_cjk_boundary_semantics():
    import re

    samples = [
        "ABC abc_123 a _ $$ water收费标准",
        "㐀㐁水\u9ffe\u9fff",
        "İ Élève Straße 中文单字 水",
        "退款 refund REFUND",
        "",
        "- x+ 12 1",
        "混合Python3中文_变量",
    ]
    for sample in samples:
        words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", sample.lower())
        expected = Counter(
            t
            for word in words
            for t in (
                [word] if not re.match(r"[\u3400-\u9fff]", word) else [word[i : i + 2] for i in range(len(word) - 1)]
            )
        )
        assert tokens(sample) == expected


def test_stale_binding_results_are_retrieval_and_can_receive_hybrid_matches(assistant, monkeypatch):
    from vks_api.models import Analysis, Relation
    from vks_api.vector_rag import VectorRag

    (web, service, _owner, pid, aid, grant, package, section, _raw), body, calls = assistant
    version = current(web, pid, aid, grant)["selection_version"]
    saved = web.post(
        root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, version, "stale-binding")
    )
    assert saved.status_code == 200, saved.text
    rid = saved.json()["data"]["binding_id"]
    with service.db() as db:
        db.get(Relation, rid).validity_status = "outdated"
        db.get(Analysis, aid).selection_version += 1
    related = web.post(root(pid) + "/related", json=body, headers=_headers(grant, 0, "related-stale"))
    assert related.status_code == 200, related.text
    assert related.json()["data"]["origin"] == "retrieval"
    assert related.json()["data"]["retrieval"] == "keyword"
    hybrid_calls = []
    monkeypatch.setattr(VectorRag, "available", lambda *args: True)

    def hybrid(self, actor, project, form, chapters, corpus, lexical):
        hybrid_calls.append(1)
        return lexical

    monkeypatch.setattr(VectorRag, "hybrid", hybrid)
    semantic = web.post(root(pid) + "/semantic", json=body, headers=_headers(grant, 0, "semantic-stale"))
    assert semantic.status_code == 200, semantic.text
    assert semantic.json()["data"]["origin"] == "retrieval" and semantic.json()["data"]["retrieval"] == "hybrid"
    assert hybrid_calls == [1] and not calls

    def removed_during_embedding(self, actor, project, form, chapters, corpus, lexical):
        with service.db() as db:
            db.get(Relation, rid).excluded = True
            db.get(Analysis, aid).selection_version += 1
        return lexical

    monkeypatch.setattr(VectorRag, "hybrid", removed_during_embedding)
    removed = web.post(root(pid) + "/semantic", json=body, headers=_headers(grant, 0, "semantic-removed"))
    assert removed.status_code == 409, removed.text
    with service.db() as db:
        assert db.get(Relation, rid).excluded


def test_cached_explanation_does_not_mark_stale_binding_as_saved(assistant):
    from vks_api.models import Analysis, Relation

    (web, service, _owner, pid, aid, grant, _package, _section, _raw), body, calls = assistant
    first = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "first-explanation"))
    assert first.status_code == 200, first.text
    binding_id = first.json()["data"]["binding_id"]
    with service.db() as db:
        db.get(Relation, binding_id).validity_status = "outdated"
        db.get(Analysis, aid).selection_version += 1
    cached = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "cached-explanation"))
    assert cached.status_code == 200, cached.text
    data = cached.json()["data"]
    assert data["cached"] and not data["saved"] and data["binding_id"] is None
    assert len(calls) == 1
