"""Scalar mapped membership preserves the authoritative chapter validator."""

import pytest
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Artifact, Entity, Revision
from vks_api.native_documents import _complete_mapped_selection, complete_selection

from tests.interactive import test_materials

materials = test_materials.materials


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "legacy",
        "empty",
        "duplicate",
        "missing",
        "count",
        "numeric_key",
        "null_key",
        "section_type",
        "revision_project",
        "entity_project",
        "artifact_project",
        "wrong_artifact",
    ],
)
def test_projection_matches_full_validator(materials, change):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# Guide\n\nIntro.\n\n## Rule\n\nExact rule."
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    info = links.catalog(owner, pid, aid)["documents"][0]
    with service.db() as db:
        document = db.get(Revision, info["revision_id"])
        metadata = dict(document.metadata_json)
        chapter = db.get(Revision, metadata["section_revision_ids"][0])
        if change == "legacy":
            metadata.pop("section_revision_ids")
        elif change == "empty":
            metadata.update(section_revision_ids=[], section_count=0)
        elif change == "duplicate":
            metadata["section_revision_ids"] = [chapter.id, chapter.id]
        elif change == "missing":
            metadata["section_revision_ids"] = ["00000000-0000-4000-8000-000000000000"]
            metadata["section_count"] = 1
        elif change == "count":
            metadata["section_count"] += 1
        elif change in {"numeric_key", "null_key"}:
            chapter.metadata_json = {**chapter.metadata_json, "document_key": 123 if change == "numeric_key" else None}
        elif change == "section_type":
            db.get(Entity, chapter.entity_id).type = "action"
        elif change == "revision_project":
            chapter.project_id = projects[1][0]
        elif change == "entity_project":
            db.get(Entity, chapter.entity_id).project_id = projects[1][0]
        elif change == "artifact_project":
            db.get(Artifact, chapter.artifact_id).project_id = projects[1][0]
        elif change == "wrong_artifact":
            chapter.artifact_id = package["artifact_id"]
        document.metadata_json = metadata
    with links.reader.reading.db() as db:
        _analysis, _release, choices, _index = links.reader.scope(db, owner, pid, aid, None)
        projected = _complete_mapped_selection(db, pid, choices, [info["id"]])
        fast = complete_selection(service, db, owner, pid, choices, [info["id"]])
        db.info["vks_read_only"] = False
        slow = complete_selection(service, db, owner, pid, choices, [info["id"]])
        assert fast == slow
        assert (projected is None) == (change == "legacy")
        if change != "legacy":
            assert fast == (change == "valid")


def test_catalog_and_binding_do_not_load_unrelated_chapter_bodies(materials, monkeypatch):
    from sqlalchemy import event

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Guide\n\n## Rule\n\nExact rule.")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    loaded = []

    def on_load(row, _context):
        if row.metadata_json.get("document_key") and "content" in row.__dict__:
            loaded.append(row.id)

    event.listen(Revision, "load", on_load)
    try:
        links = InteractiveKnowledgeService(service)
        assert links.catalog(owner, pid, aid)["documents"]
        assert links.bindings(owner, pid, aid) == []
        assert loaded == []
    finally:
        event.remove(Revision, "load", on_load)
