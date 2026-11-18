"""Exact SQL membership preserves full-map corruption rejection."""

from copy import deepcopy

import pytest
from sqlalchemy import event
from vks_api.documents import DocumentService
from vks_api.models import Entity, Revision, uid
from vks_api.service import DomainError

from tests.documents import test_documents

ctx = test_documents.ctx


def mapped(ctx, session):
    _service, _actor, _project, _workspace, _documents = ctx
    document = ctx[3]["documents"][0]
    entity = session.get(Entity, document["entity_id"])
    revision = session.get(Revision, document["entity_revision_id"])
    assert revision.metadata_json["section_revision_ids"]
    return entity, revision


def test_exact_map_check_does_not_load_all_section_bodies(ctx):
    service = ctx[0]
    with service.db() as session:
        entity, revision = mapped(ctx, session)
        ids = revision.metadata_json["section_revision_ids"]
        statements = []

        def queried(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(service.engine, "before_cursor_execute", queried)
        try:
            assert DocumentService.section_members(session, entity, revision, [ids[0], "unknown"]) == {ids[0]}
        finally:
            event.remove(service.engine, "before_cursor_execute", queried)
        assert len(statements) == 1 and "count(distinct" in statements[0].lower()
        assert "entity_revision.content" not in statements[0]
        assert {r.id for r in DocumentService._sections(session, entity, revision)} == set(ids)


def test_prefetched_full_maps_share_one_query_and_keep_membership_separate(ctx):
    service = ctx[0]
    with service.db() as session:
        entity, revision = mapped(ctx, session)
        # An empty exact document map is valid and must not acquire the other
        # document's membership through the batch's LEFT JOIN.
        from types import SimpleNamespace

        empty = SimpleNamespace(id="empty-document", project_id=entity.project_id, stable_key="empty", type="document")
        empty_revision = SimpleNamespace(
            id="empty-revision",
            artifact_id=None,
            metadata_json={"section_revision_ids": [], "section_count": 0},
        )
        session.info["vks_completion_batch"] = True
        statements = []

        def queried(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(service.engine, "before_cursor_execute", queried)
        try:
            DocumentService.prefetch_section_members(session, [(entity, revision), (empty, empty_revision)])
            ids = revision.metadata_json["section_revision_ids"]
            assert DocumentService.section_members(session, entity, revision, ids) == set(ids)
            assert DocumentService.section_members(session, empty, empty_revision, ids) == set()
        finally:
            event.remove(service.engine, "before_cursor_execute", queried)
        assert len(statements) == 1 and "count(distinct" in statements[0].lower()
        assert "entity_revision.content" not in statements[0]


@pytest.mark.parametrize("damage", ["duplicate_id", "missing", "count", "document_key", "artifact", "kind", "identity"])
def test_rejects_every_invalid_mapping_the_original_resolver_rejects(ctx, damage):
    service = ctx[0]
    with service.db() as session:
        entity, document = mapped(ctx, session)
        ids = document.metadata_json["section_revision_ids"]
        section = session.get(Revision, ids[0])
        metadata = deepcopy(document.metadata_json)
        if damage == "duplicate_id":
            metadata["section_revision_ids"].append(ids[0])
            metadata["section_count"] += 1
        elif damage == "missing":
            metadata["section_revision_ids"][0] = "nonexistent"
        elif damage == "count":
            metadata["section_count"] += 1
        elif damage == "document_key":
            section.metadata_json = {**section.metadata_json, "document_key": "another-document"}
        elif damage == "artifact":
            section.artifact_id = None
        elif damage == "kind":
            session.get(Entity, section.entity_id).type = "action"
        elif damage == "identity":
            duplicate = Revision(
                id=uid(),
                project_id=section.project_id,
                entity_id=section.entity_id,
                artifact_id=section.artifact_id,
                name=section.name,
                content=section.content,
                content_sha256=uid(),
                semantic_content_sha256=section.semantic_content_sha256,
                locator=section.locator,
                metadata_json=section.metadata_json,
            )
            session.add(duplicate)
            metadata["section_revision_ids"].append(duplicate.id)
            metadata["section_count"] += 1
        document.metadata_json = metadata
        session.flush()
        session.info["vks_completion_batch"] = True
        DocumentService.prefetch_section_members(session, [(entity, document)])
        for resolver in [
            lambda: DocumentService._sections(session, entity, document),
            lambda: DocumentService.section_members(session, entity, document, ids),
        ]:
            with pytest.raises(DomainError) as error:
                resolver()
            assert error.value.code == "VKS_DOCUMENT_SECTIONS_UNRESOLVED"
