"""Internal target templates preserve exact full preparation and current fences."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from sqlalchemy import select
from vks_api import corpus_cache
from vks_api.ai_gateway import ModelPolicy
from vks_api.documents import DocumentService
from vks_api.identity import UserIdentity
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant, InteractiveQuestionInput
from vks_api.models import Artifact, Revision
from vks_api.service import Actor, DomainError

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def forms(body):
    base = AssistanceInput.model_validate(body)
    return [
        base.model_copy(
            update={
                "target": base.target.model_copy(
                    update={
                        "selector": "body" if i == 3 else f"#control-{i}",
                        "label": f"业务操作 {i}",
                        "state": "#dialog" if i == 2 else "",
                    }
                ),
                "context": base.context.model_copy(
                    update={
                        "scope": "module" if i == 3 else "control",
                        "page_title": f"业务页面 {i}",
                        "nearby_labels": ["新增", "检查"],
                    }
                ),
            }
        )
        for i in range(5)
    ]


def test_templates_produce_exact_per_target_full_keys_and_chapters_without_rehydrating(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _raw), body, calls = assistant
    reference, engine = InteractiveAssistant(service), InteractiveAssistant(service)
    targets = forms(body)
    expected = [reference.prepare(owner, pid, form) for form in targets]
    engine.prepare(owner, pid, AssistanceInput.model_validate(body))  # Root may prewarm outside the batch context.
    original, hydrated = corpus_cache.corpus_rows, []

    def observed(reader, *args, **kwargs):
        if reader is engine:
            hydrated.append(True)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(corpus_cache, "corpus_rows", observed)
    with engine.batch(owner, pid):
        actual = [engine.prepare(owner, pid, form) for form in targets]
    assert actual == expected
    assert len({state[3] for state in actual}) == len(targets)
    assert not hydrated and not calls
    # Ordinary external requests deliberately retain their full preparation path.
    assert engine.prepare(owner, pid, targets[0]) == expected[0]
    assert len(hydrated) == 1


@pytest.mark.parametrize("change", ["document", "source_policy", "model", "rag", "package", "revoked"])
def test_templates_fall_back_on_current_input_or_scope_changes(assistant, monkeypatch, change):
    (_web, service, owner, pid, aid, _grant, package, section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    base = AssistanceInput.model_validate(body)
    old = engine.prepare(owner, pid, base)
    form = forms(body)[0]
    if change == "document":
        chapter = old[1][0]
        with service.db() as s:
            eid = s.get(Revision, chapter["document_revision_id"]).entity_id
        DocumentService(service).save_draft(
            owner,
            eid,
            {
                "analysis_set_id": aid,
                "base_revision_id": chapter["document_revision_id"],
                "markdown": "# Replacement\n\n## Changed\n\nNew exact rule.",
                "locked_section_ids": [],
            },
            1,
        )
    elif change == "rag":
        settings = engine.rag.store.settings(pid).model_copy(update={"model_id": "new-embedding"})
        with engine.rag.store.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO config(project,settings) VALUES (?,?)", (pid, settings.model_dump_json())
            )
    elif change == "package":
        form = form.model_copy(update={"package_id": "not-currently-selected"})
    else:
        with service.db() as s:
            if change == "source_policy":
                art = s.get(Artifact, s.get(Revision, section["entity_revision_id"]).artifact_id)
                art.metadata_json = {**art.metadata_json, "requires_trace_read": True}
            elif change == "model":
                policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                policy.settings = {**policy.settings, "model_id": "new-model"}
            else:
                s.get(UserIdentity, owner.id).status = "disabled"
    reference = InteractiveAssistant(service)
    try:
        expected = reference.prepare(owner, pid, form)
    except DomainError as error:
        with engine.batch(owner, pid), pytest.raises(DomainError) as actual:
            engine.prepare(owner, pid, form)
        assert actual.value.code == error.code
    else:
        original, hydrated = corpus_cache.corpus_rows, []

        def observed(reader, *args, **kwargs):
            if reader is engine:
                hydrated.append(True)
            return original(reader, *args, **kwargs)

        monkeypatch.setattr(corpus_cache, "corpus_rows", observed)
        with engine.batch(owner, pid):
            assert engine.prepare(owner, pid, form) == expected
        assert hydrated


def test_limited_question_and_frozen_requests_keep_full_preparation(assistant, monkeypatch):
    (_web, service, owner, pid, aid, _grant, _package, _section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    base = AssistanceInput.model_validate(body)
    engine.prepare(owner, pid, base)
    original, hydrated = corpus_cache.corpus_rows, []

    def observed(reader, *args, **kwargs):
        if reader is engine:
            hydrated.append(True)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(corpus_cache, "corpus_rows", observed)
    limited = Actor(owner.id, frozenset({"knowledge:read", "knowledge:edit"}), True)
    with engine.batch(limited, pid):
        engine.prepare(limited, pid, base)
        engine.prepare(limited, pid, base)
    assert len(hydrated) == 2
    question = InteractiveQuestionInput(**base.model_dump(), question="当前功能有什么前提？")
    with engine.batch(owner, pid):
        engine.prepare(owner, pid, question)
        engine.prepare(owner, pid, question)
    assert len(hydrated) == 4
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "template-frozen")
    frozen = base.model_copy(update={"analysis_set_id": None, "release_id": release["id"]})
    with engine.batch(owner, pid):
        assert not engine.prepare(owner, pid, frozen)[5]
        assert not engine.prepare(owner, pid, frozen)[5]
    assert len(hydrated) == 6
    with engine.batch(owner, pid):
        assert not engine.prepare(owner, pid, base)[5]
        assert not engine.prepare(owner, pid, base)[5]
    assert len(hydrated) == 8


def test_parallel_prepared_targets_do_not_hold_template_lock_during_database_reads(assistant):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    engine.prepare(owner, pid, AssistanceInput.model_validate(body))
    targets = forms(body)
    with engine.batch(owner, pid), ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(copy_context().run, engine.prepare, owner, pid, form) for form in targets]
        results = [future.result(timeout=4) for future in futures]
    assert len({state[3] for state in results}) == 5 and all(state[1] for state in results)
