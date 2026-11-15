"""Read transactions, exact citation membership and shared immutable native facts."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace

from sqlalchemy import select
from vks_api.documents import DocumentService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Analysis, Entity, Revision, Selection
from vks_api.native_documents import _source_cache
from vks_api.service import sha

from tests.interactive import test_knowledge
from tests.interactive.test_knowledge import form, root
from tests.prototype.test_v3_workflow import _headers

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive


def test_interactive_readers_do_not_wait_for_unrelated_writer(interactive):
    _web, service, owner, pid, aid, _grant, _package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    with service.db() as db:
        chapter = db.get(Revision, section["entity_revision_id"])
        key = chapter.metadata_json["document_key"]
        document = db.scalar(
            select(Entity).where(Entity.project_id == pid, Entity.stable_key == key, Entity.type == "document")
        )
        rid = db.get(Selection, (aid, document.id)).entity_revision_id
    started, release = Event(), Event()

    def writer():
        with service.db():
            started.set()
            release.wait(5)

    def reader():
        assert links.catalog(owner, pid, aid)["documents"]
        assert links.document(owner, pid, rid, aid, native=False)["sections"]
        return links.bindings(owner, pid, aid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(writer)
        assert started.wait(2)
        try:
            assert pool.submit(reader).result(timeout=2) == []
        finally:
            release.set()
            active.result(timeout=5)


def test_selected_orphan_chapter_does_not_cite_replacement_document(interactive):
    web, service, owner, pid, aid, grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    v = links.catalog(owner, pid, aid)["selection_version"]
    saved = web.post(root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, v, "initial"))
    assert saved.status_code == 200, saved.text
    old = links.bindings(owner, pid, aid)[0]["sections"][0]
    document = next(
        d for d in links.catalog(owner, pid, aid)["documents"] if d["revision_id"] == old["document_revision_id"]
    )
    DocumentService(service).save_draft(
        owner,
        document["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": document["revision_id"],
            "markdown": "# Replacement\n\n## New chapter\n\nNew exact rule.",
            "locked_section_ids": [],
        },
        1,
    )
    # A version selector may retain an older chapter, but the selected document's
    # immutable chapter mapping still owns its membership.
    with service.db() as db:
        chapter = db.get(Revision, old["revision_id"])
        selected = db.get(Selection, (aid, chapter.entity_id))
        if selected:
            selected.entity_revision_id = chapter.id
        else:
            db.add(Selection(analysis_set_id=aid, entity_id=chapter.entity_id, entity_revision_id=chapter.id))
        db.get(Analysis, aid).selection_version += 1
    result = links.bindings(owner, pid, aid)[0]
    assert result["sections"] == [] and result["validity_status"] == "outdated"
    version = links.catalog(owner, pid, aid)["selection_version"]
    new_binding = form(aid, package, section)
    new_binding["target"]["selector"] = "#different-control"
    rejected = web.post(root(pid) + "/bindings", json=new_binding, headers=_headers(grant, version, "orphan"))
    assert rejected.status_code == 404, rejected.text


def test_native_source_parse_is_shared_and_prepared_once(tmp_path, monkeypatch):
    from vks_api import native_documents

    objects = tmp_path / "objects"
    objects.mkdir()
    raw = b'<html><body><h1 id="guide">Guide</h1><p>Rule.</p></body></html>'
    digest = sha(raw)
    (objects / digest).write_bytes(raw)
    service = SimpleNamespace(data_dir=tmp_path)
    artifact = SimpleNamespace(id="source", sha256=digest, size_bytes=len(raw), object_key=digest)
    original, counter, lock = native_documents._sources, [], Lock()
    start = Barrier(5)

    def source(*args):
        with lock:
            counter.append(1)
        time.sleep(0.08)
        return original(*args)

    def read():
        start.wait(timeout=3)
        return _source_cache(service, artifact, "html-document", "guide.html")

    monkeypatch.setattr(native_documents, "_sources", source)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: read(), range(5)))
    assert len(counter) == 1 and all(result is results[0] for result in results)
    assert results[0]["guide.html"]["positions"][1]["anchor"] == "guide"

    def no_reparse(*args):
        raise AssertionError("A warm immutable source was parsed again")

    monkeypatch.setattr(native_documents, "parse_sections", no_reparse)
    assert _source_cache(service, artifact, "html-document", "guide.html") is results[0]
    assert len(service._native_document_source_state[2]) == 0


def test_unselected_chapter_marks_partially_retained_binding_outdated(interactive):
    web, service, owner, pid, aid, grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    document = links.document(owner, pid, catalog["documents"][0]["revision_id"], aid, native=False)
    citations = [row["revision_id"] for row in document["sections"]][:2]
    assert len(citations) == 2
    payload = form(aid, package, section)
    payload["section_revision_ids"] = citations
    response = web.post(
        root(pid) + "/bindings", json=payload, headers=_headers(grant, catalog["selection_version"], "two-citations")
    )
    assert response.status_code == 200, response.text
    for rid in citations:
        with service.db() as db:
            chapter = db.get(Revision, rid)
            db.delete(db.get(Selection, (aid, chapter.entity_id)))
            db.get(Analysis, aid).selection_version += 1
        binding = links.bindings(owner, pid, aid)[0]
        assert binding["validity_status"] == "outdated"
        assert rid not in {c["revision_id"] for c in binding["sections"]}
    assert binding["sections"] == []


def test_read_transaction_releases_prefetch_without_waiting_for_gc(interactive):
    import gc
    import weakref

    _web, service, owner, pid, aid, _grant, _package, _section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    enabled = gc.isenabled()
    gc.disable()
    try:
        with links.reader.reading.db() as db:
            _analysis, _release, choices, _index = links.reader.scope(db, owner, pid, aid, None)
            rows = links._rows(db, owner, pid, choices)
            session_ref = weakref.ref(db)
            row_ref = weakref.ref(rows[-1][1])
            del rows
        del db
        assert session_ref() is None, "Read session retained its transaction through session.info"
        assert row_ref() is None, "Read prefetch retained an entire corpus until cyclic GC"
    finally:
        if enabled:
            gc.enable()
        gc.collect()
