"""Fresh invariant fences retain exact facts without re-preparing unchanged text."""

import json

import pytest
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy
from vks_api.corpus_validation import validated
from vks_api.documents import DocumentService
from vks_api.identity import UserIdentity
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.models import Analysis, Artifact, Revision, Selection
from vks_api.service import Actor, DomainError

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def setup(assistant):
    (_web, service, owner, pid, aid, _grant, package, section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    state = engine.prepare(owner, pid, form)
    assert validated(engine, owner, pid, form, state[3]) == state
    return engine, service, owner, pid, aid, package, section, form, state


def test_unchanged_full_answer_and_commit_use_fresh_light_fences(assistant, monkeypatch):
    engine, _service, owner, pid, _aid, _package, _section, form, _state = setup(assistant)

    def no_reprepare(*args, **kwargs):
        raise AssertionError("Unchanged model result rebuilt the complete corpus")

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        monkeypatch.setattr(engine, "prepare", no_reprepare)
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    answer = engine.run(owner, pid, form)
    assert answer["saved"] and answer["sections"] and answer["explanation"] == "Document-grounded explanation"


@pytest.mark.parametrize(
    "change", ["source_policy", "dependency_policy", "source_size", "selected_section", "document", "model", "rag"]
)
def test_changed_inputs_fall_back_and_reject_old_key(assistant, change):
    engine, service, owner, pid, aid, package, section, form, state = setup(assistant)
    if change == "document":
        document = state[1][0]
        with service.db() as s:
            eid = s.get(Revision, document["document_revision_id"]).entity_id
        DocumentService(service).save_draft(
            owner,
            eid,
            {
                "analysis_set_id": aid,
                "base_revision_id": document["document_revision_id"],
                "markdown": "# Replacement\n\n## Changed\n\nChanged business rule.",
                "locked_section_ids": [],
            },
            1,
        )
    elif change == "rag":
        settings = engine.rag.store.settings(pid).model_copy(update={"model_id": "changed-embedding"})
        with engine.rag.store.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO config(project,settings) VALUES (?,?)", (pid, settings.model_dump_json())
            )
    else:
        with service.db() as s:
            chapter = s.get(Revision, section["entity_revision_id"])
            if change == "selected_section":
                s.delete(s.get(Selection, (aid, chapter.entity_id)))
                s.get(Analysis, aid).selection_version += 1
            elif change == "model":
                policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                policy.settings = {**policy.settings, "model_id": "changed-model"}
            elif change == "source_policy":
                art = s.get(Artifact, chapter.artifact_id)
                art.metadata_json = {**art.metadata_json, "requires_trace_read": True}
            elif change == "source_size":
                # Offline source closure validation hashes member sizes as
                # well as paths and content digests; the live fence must too.
                s.get(Artifact, package).size_bytes += 1
            else:
                # The dependency was part of the previously prepared source
                # closure; changing it must invalidate even unchanged chapters.
                art = s.get(Artifact, package)
                art.sensitivity = "confidential"
    assert validated(engine, owner, pid, form, state[3]) is None
    with pytest.raises(DomainError):
        engine._unchanged(owner, pid, form, state[3], None, None)


def test_fresh_grants_and_limited_readers_cannot_use_full_reader_fence(assistant):
    engine, service, owner, pid, _aid, _package, _section, form, state = setup(assistant)
    limited = Actor(owner.id, frozenset({"knowledge:read", "knowledge:edit"}), True)
    assert validated(engine, limited, pid, form, state[3]) is None
    with service.db() as s:
        s.get(UserIdentity, owner.id).status = "disabled"
    with pytest.raises(DomainError) as error:
        validated(engine, owner, pid, form, state[3])
    assert error.value.code in {"VKS_FORBIDDEN", "VKS_UNAUTHENTICATED"}


def test_unrelated_association_progress_changes_only_current_version(assistant):
    engine, service, owner, pid, aid, _package, _section, form, state = setup(assistant)
    with service.db() as s:
        s.get(Analysis, aid).selection_version += 1
    current = validated(engine, owner, pid, form, state[3])
    assert current[:4] == state[:4] and current[4] == state[4] + 1 and current[5] == state[5]
    different = form.model_copy(update={"target": form.target.model_copy(update={"label": "Different target"})})
    assert validated(engine, owner, pid, different, state[3]) is None
