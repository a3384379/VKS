"""Exact package membership uses selective facts, never cached permission grants."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from vks_api import native_documents
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.materials import ROOT, readable
from vks_api.models import Artifact, Entity, Revision, uid
from vks_api.native_documents import package_documents
from vks_api.service import DomainError, sha

from tests.interactive import test_materials

materials = test_materials.materials


def install_doc(service, owner, manager, pid, aid, title):
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", f"# {title}\n\n## Rule\n\nExact rule."
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    return package


def test_package_projection_filters_both_owners_and_reuses_only_read_facts(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    other_pid, other_aid = projects[1]
    local = install_doc(service, owner, manager, pid, aid, "Local")
    foreign = install_doc(service, owner, manager, other_pid, other_aid, "Other")
    with service.db() as db:
        foreign_root = db.get(Revision, foreign["revision_id"])
        other_document = db.scalar(
            select(Revision)
            .join(Entity, Revision.entity_id == Entity.id)
            .where(Revision.id.in_(foreign_root.metadata_json[ROOT]["members"]), Entity.type == "document")
        )
        foreign_rid = other_document.id
        forged = Revision(
            id=uid(),
            project_id=pid,
            entity_id=other_document.entity_id,
            artifact_id=other_document.artifact_id,
            name="Invalid ownership fixture",
            content="",
            content_sha256=sha("forged ownership"),
            semantic_content_sha256=sha(""),
            locator={},
            metadata_json={},
        )
        db.add(forged)
        forged_rid = forged.id
    reader = InteractiveKnowledgeService(service).reader.reading
    with reader.db() as db:
        root = db.get(Revision, local["revision_id"])
        queries = []

        def count(_connection, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(service.engine, "before_cursor_execute", count)
        try:
            result = package_documents(db, pid, root)
            assert len(result) == 1 and queries
            calls = len(queries)
            result.append("cannot poison cached facts")
            assert len(package_documents(db, pid, root)) == 1
            assert len(queries) == calls
            metadata = deepcopy(root.metadata_json)
            metadata[ROOT]["members"].extend([foreign_rid, forged_rid])
            mixed = SimpleNamespace(id=root.id, project_id=pid, metadata_json=metadata)
            assert package_documents(db, pid, mixed) == result[:-1]
            assert len(queries) > calls
            # Full-capability source readers still reject any other-project
            # revision in a raw package, before exposing its bytes or count.
            mixed.id = uid()
            assert readable(service, db, owner, pid, mixed) is False
        finally:
            event.remove(service.engine, "before_cursor_execute", count)


def test_package_membership_is_not_cached_in_write_transaction(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = install_doc(service, owner, manager, pid, aid, "Guide")
    with service.db() as db:
        root = db.get(Revision, package["revision_id"])
        queries = []

        def count(_connection, _cursor, statement, _parameters, _context, _many):
            queries.append(statement)

        event.listen(service.engine, "before_cursor_execute", count)
        try:
            first = package_documents(db, pid, root)
            calls = len(queries)
            assert first and calls > 0
            assert package_documents(db, pid, root) == first
            assert len(queries) == calls * 2
            assert "vks_package_document_members" not in db.info
        finally:
            event.remove(service.engine, "before_cursor_execute", count)


def test_read_membership_cache_does_not_survive_a_later_authorization_change(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = install_doc(service, owner, manager, pid, aid, "Public guide")
    token = service.create_token(owner, pid, {"name": "Scoped reader", "scopes": ["knowledge:read", "knowledge:edit"]})
    actor = service.authenticate(token["token"], "token")
    links = InteractiveKnowledgeService(service)
    with links.reader.reading.db() as db:
        root = db.get(Revision, package["revision_id"])
        assert readable(service, db, actor, pid, root)
        assert package_documents(db, pid, root)
        assert package_documents(db, pid, root)
        member_id = root.metadata_json[ROOT]["members"][0]
    with service.db() as db:
        member = db.get(Revision, member_id)
        db.get(Artifact, member.artifact_id).sensitivity = "confidential"
    with links.reader.reading.db() as db:
        root = db.get(Revision, package["revision_id"])
        assert not readable(service, db, actor, pid, root)
    assert not links.catalog(actor, pid, aid)["packages"]


def test_original_content_validates_only_the_requested_plugin(materials, monkeypatch):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype_raw = "<title>Prototype</title><button>Open</button>"
    handbook_raw = "<title>Guide</title><h1>Original guide</h1><p>Exact source.</p>"
    prototype = test_materials.stage(service, owner, manager, pid, aid, "prototype", prototype_raw)
    handbook = test_materials.stage(service, owner, manager, pid, aid, "documents", handbook_raw, filename="guide.html")
    test_materials.activate(service, owner, manager, pid, aid, [prototype, handbook])
    original = native_documents.complete_selection
    checked = []

    def check(*args):
        checked.append(args[-1])
        return original(*args)

    monkeypatch.setattr(native_documents, "complete_selection", check)
    links = InteractiveKnowledgeService(service)
    assert links.content(owner, pid, prototype["artifact_id"], aid) == prototype_raw.encode()
    assert checked == []
    assert links.content(owner, pid, handbook["artifact_id"], aid) == handbook_raw.encode()
    assert len(checked) == 1
    test_materials.activate(service, owner, manager, pid, aid, remove=[prototype["package_id"]])
    with pytest.raises(DomainError):
        links.content(owner, pid, prototype["artifact_id"], aid)
