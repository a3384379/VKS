"""Warm immutable text reuse must match fresh permission and version projection."""

import pytest
from vks_api import corpus_cache
from vks_api.documents import DocumentService
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.models import Artifact, Entity, Revision
from vks_api.service import Actor, DomainError, canonical, sha

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def reference(monkeypatch, engine, actor, pid, form):
    with monkeypatch.context() as patch:
        patch.setattr(
            corpus_cache,
            "corpus_rows",
            lambda assistant, session, user, project, choices, **kwargs: assistant.links._rows(
                session, user, project, choices, types={"document", "section"}
            ),
        )

        def exhaustive(assistant, session, rows, policy_for, *, for_model, allow_source):
            sections = {r.id for e, r in rows if e.type == "section"}
            chapters, revisions = [], {}
            for entity, document in rows:
                if entity.type != "document":
                    continue
                for chapter in assistant.links.documents._sections(session, entity, document):
                    if chapter.id not in sections:
                        continue
                    artifact = session.get(Artifact, chapter.artifact_id)
                    policy = policy_for(artifact)
                    if for_model and (
                        artifact.sensitivity == "confidential"
                        or policy.get("requires_source_read")
                        and not allow_source
                    ):
                        continue
                    chapters.append(
                        {
                            "revision_id": chapter.id,
                            "name": chapter.name,
                            "content": chapter.content,
                            "document_revision_id": document.id,
                            "document_name": document.name,
                        }
                    )
                    revisions[chapter.id] = {
                        "sha": chapter.content_sha256,
                        "artifact": chapter.artifact_id,
                        "policy": policy,
                        "document": document.id,
                    }
            return chapters, sha(canonical(revisions))

        patch.setattr(corpus_cache, "corpus_projection", exhaustive)
        return engine.prepare(actor, pid, form, for_model=False)


def test_warm_corpus_matches_fresh_rows_across_distinct_readers(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _raw), body, calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    initial = engine.prepare(owner, pid, form, for_model=False)
    assert engine.prepare(owner, pid, form, for_model=False) == initial
    limited = Actor(owner.id, frozenset({"knowledge:read", "knowledge:edit"}), True)
    assert engine.prepare(limited, pid, form, for_model=False) == reference(monkeypatch, engine, limited, pid, form)
    assert engine.prepare(owner, pid, form, for_model=False) == reference(monkeypatch, engine, owner, pid, form)
    assert not calls and len(engine.corpora) == 1


def test_warm_corpus_refreshes_artifact_visibility_and_denies_revoked_reader(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, section, _raw), body, calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    original = engine.prepare(owner, pid, form, for_model=False)
    with service.db() as s:
        chapter = s.get(Revision, section["entity_revision_id"])
        source = s.get(Artifact, chapter.artifact_id)
        source.metadata_json = {**source.metadata_json, "requires_source_read": True}
    limited = Actor(owner.id, frozenset({"knowledge:read", "knowledge:edit"}), True)
    actual = engine.prepare(limited, pid, form, for_model=False)
    assert actual == reference(monkeypatch, engine, limited, pid, form)
    assert len(actual[1]) < len(original[1]) and section["entity_revision_id"] not in {
        c["revision_id"] for c in actual[1]
    }
    # The privileged reader may still consume its authorized text from the same facts cache.
    assert engine.prepare(owner, pid, form, for_model=False) == reference(monkeypatch, engine, owner, pid, form)
    revoked = Actor(owner.id, frozenset(), True)
    with pytest.raises(DomainError) as error:
        engine.prepare(revoked, pid, form, for_model=False)
    assert error.value.code == "VKS_FORBIDDEN" and not calls


def test_corpus_switch_discards_replaced_document_chapters(assistant, monkeypatch):
    (_web, service, owner, pid, aid, _grant, _package, _section, _raw), body, calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    original = engine.prepare(owner, pid, form, for_model=False)
    old = original[1][0]
    with service.db() as s:
        document = s.get(Revision, old["document_revision_id"])
        eid = document.entity_id
    DocumentService(service).save_draft(
        owner,
        eid,
        {
            "analysis_set_id": aid,
            "base_revision_id": old["document_revision_id"],
            "markdown": "# Replacement guide\n\n## Current rule\n\nCurrent precise business rule.",
            "locked_section_ids": [],
        },
        1,
    )
    changed = engine.prepare(owner, pid, form, for_model=False)
    assert changed == reference(monkeypatch, engine, owner, pid, form)
    assert changed[2] != original[2] and changed[3] != original[3]
    assert old["revision_id"] not in {r["revision_id"] for r in changed[1]}
    assert any("Current precise business rule" in r["content"] for r in changed[1]) and not calls


def test_warm_cache_rechecks_selected_entity_project(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    engine.prepare(owner, pid, form, for_model=False)
    # An invalid current identity may never be authorized merely by cached text.
    other = service.create_project(owner, {"key": "cache-other", "name": "Other"}, "cache-other")["id"]
    with service.db() as s:
        chapter = s.get(Revision, section["entity_revision_id"])
        entity = s.get(Entity, chapter.entity_id)
        entity.project_id = other
    with pytest.raises(DomainError) as actual:
        engine.prepare(owner, pid, form, for_model=False)
    with pytest.raises(DomainError) as expected:
        reference(monkeypatch, engine, owner, pid, form)
    assert actual.value.code == expected.value.code


def test_failed_write_cannot_expire_shared_revision_or_root_facts(assistant):
    (_web, service, owner, pid, aid, _grant, _package, _section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    with pytest.raises(RuntimeError, match="rolled back"):
        with service.db() as s:
            first = engine.prepare(owner, pid, form, session=s)
            raise RuntimeError("rolled back")
    # Session.rollback expires ORM instances even with expire_on_commit=False.
    # Cached facts must remain independent values, usable by the next request.
    assert engine.prepare(owner, pid, form) == first
    assert engine.links.catalog(owner, pid, aid)["packages"]
