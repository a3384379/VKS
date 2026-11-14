"""Chat identities follow authorized immutable sources, not generated bindings."""

import re
from dataclasses import replace

from sqlalchemy import event
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Analysis, Artifact, Revision, Selection

from tests.interactive import test_materials
from tests.interactive.test_document_navigation import zip_docs

materials = test_materials.materials


def prepared(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs(
            [
                ("guide.md", "# Guide\n\nVisible intro.\n\n## Removed\n\nA selected chapter."),
                ("private.md", "# Private\n\nRestricted corpus."),
            ]
        ),
        filename="docs.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    return service, owner, pid, aid, links


def test_binding_counter_does_not_change_sources_but_independent_chapter_removal_does(materials):
    service, owner, pid, aid, links = prepared(materials)
    first = links.catalog(owner, pid, aid)
    doc = next(doc for doc in first["documents"] if doc["name"] == "Guide")
    article = links.document(owner, pid, doc["revision_id"], aid)
    removed = next(section for section in article["sections"] if section["name"] == "Removed")
    with service.db() as db:
        db.get(Analysis, aid).selection_version += 1
    count_only = links.catalog(owner, pid, aid)
    assert count_only["selection_version"] > first["selection_version"]
    assert count_only["source_fingerprint"] == first["source_fingerprint"]
    with service.db() as db:
        revision = db.get(Revision, removed["revision_id"])
        db.delete(db.get(Selection, (aid, revision.entity_id)))
        db.get(Analysis, aid).selection_version += 1
    changed = links.catalog(owner, pid, aid)
    assert changed["documents"] == first["documents"], "The immutable document revision itself was retained"
    assert changed["source_fingerprint"] != first["source_fingerprint"]


def test_source_fingerprint_filters_permissions_before_hashing_and_loads_no_chapter_body(materials):
    service, owner, pid, aid, links = prepared(materials)
    # Keep draft edit/read scope while deliberately dropping source/trace grants.
    limited = replace(owner, scopes=frozenset({"knowledge:read", "knowledge:edit"}))
    first = links.catalog(owner, pid, aid)
    private = next(doc for doc in first["documents"] if doc["name"] == "Private")
    with service.db() as db:
        revision = db.get(Revision, private["revision_id"])
        db.get(Artifact, revision.artifact_id).sensitivity = "confidential"
    public = links.catalog(limited, pid, aid)
    assert [doc["name"] for doc in public["documents"]] == ["Guide"]
    with service.db() as db:
        # The private source is not part of the limited reader's identity.
        revision = db.get(Revision, private["revision_id"])
        revision.content_sha256 = "f" * 64
    assert links.catalog(limited, pid, aid)["source_fingerprint"] == public["source_fingerprint"]
    statements = []
    with links.reader.reading.db() as db:
        _, _, choices, _ = links.reader.scope(db, owner, pid, aid, None)
        documents = links._rows(db, owner, pid, choices, types={"document"})
        packages = links._packages(db, owner, pid, choices)

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(service.engine, "before_cursor_execute", capture)
        try:
            links._source_fingerprint(db, owner, pid, choices, documents, packages)
        finally:
            event.remove(service.engine, "before_cursor_execute", capture)
    assert statements and not any(re.search(r"\bentity_revision\.content\b", statement) for statement in statements)
