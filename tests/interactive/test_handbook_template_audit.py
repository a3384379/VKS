"""Search must honor selected chapter projection, including document matches."""

from vks_api.document_presentation import PresentationSearchInput, PresentationService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Analysis, Revision, Selection

from tests.interactive import test_materials

materials = test_materials.materials


def test_template_search_cannot_return_unselected_chapter_through_document_body(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# Guide\n\nPublic content.\n\n## Removed\n\nremoved-secret-marker is no longer selected.",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    doc = links.catalog(owner, pid, aid)["documents"][0]
    article = links.document(owner, pid, doc["revision_id"], aid)
    removed = next(section for section in article["sections"] if section["name"] == "Removed")
    with service.db() as db:
        revision = db.get(Revision, removed["revision_id"])
        db.delete(db.get(Selection, (aid, revision.entity_id)))
        db.get(Analysis, aid).selection_version += 1
    projected = links.document(owner, pid, doc["revision_id"], aid)
    assert projected["partial"] and "removed-secret-marker" not in projected["content"]
    result = PresentationService(service).search(
        owner, pid, PresentationSearchInput(analysis_set_id=aid, query="removed-secret-marker")
    )
    assert result == [], result
