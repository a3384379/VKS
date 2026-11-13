"""Native source readers keep exact locations, history, and whole-package access."""

import base64
import gzip
import json
from hashlib import sha256

from sqlalchemy import select
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.knowledge import ImportService
from vks_api.models import Artifact, Revision, Selection
from vks_api.prototype import preview_fingerprint

from tests.interactive import test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.prototype.test_v3_workflow import _upload

materials = test_materials.materials


def source_handbook():
    sources, pages = {}, {}
    for key, path in [("article-a", "a/shared.md"), ("article-b", "b/shared.md")]:
        content = "# Shared\n\nOriginal intro.\n\n## Duplicate\n\nFirst rule.\n\n## Duplicate\n\nSecond rule.\n"
        raw = content.encode()
        sources[key] = {"path": path, "sha256": sha256(raw).hexdigest(), "base64": base64.b64encode(raw).decode()}
        pages[key] = {
            "html": f'<h3 id="{key}--shared">Shared</h3><p>Original intro.</p>'
            f'<h4 id="{key}--duplicate">Duplicate</h4><p>First rule.</p>'
            f'<h4 id="{key}--duplicate-1">Duplicate</h4><p>Second rule.</p>'
        }
    inner = (
        '<!doctype html><article id="article-a"></article><article id="article-b"></article>'
        '<script id="source-data" type="application/json">'
        + json.dumps(sources)
        + '</script><script id="fh-book-pages" type="application/json">'
        + json.dumps(pages)
        + "</script>"
    ).encode()
    metadata = {"format": "fh-gzip-html-1", "bytes": len(inner), "sha256": sha256(inner).hexdigest()}
    return (
        '<script id="fh-pack-meta" type="application/json">'
        + json.dumps(metadata)
        + '</script><script id="fh-pack-data" type="application/octet-stream">'
        + base64.b64encode(gzip.compress(inner)).decode()
        + "</script>"
    ).encode()


def details(service, owner, pid, aid):
    shared = InteractiveKnowledgeService(service)
    catalog = shared.catalog(owner, pid, aid)
    return catalog, [shared.document(owner, pid, row["revision_id"], aid) for row in catalog["documents"]]


def read_http(web, service, owner, pid, rid, aid=None, release=None, actor_token=None):
    token = (
        actor_token
        or service.create_token(owner, pid, {"name": "native-reader", "scopes": ["knowledge:read", "knowledge:edit"]})[
            "token"
        ]
    )
    response = web.get(
        f"/api/v3/projects/{pid}/interactive/documents/{rid}",
        params={k: v for k, v in {"analysis_set_id": aid, "release_id": release}.items() if v},
        headers={"Authorization": "Bearer " + token},
    )
    return response


def test_single_html_native_descriptor_and_duplicate_heading_positions(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = '<!doctype html><title>Source UI</title><style>h1{color:red}</style><h1 id="intro">Guide</h1><p>Intro</p><h2>Same</h2><p>One</p><h2>Same</h2><p>Two</p><script>window.MUST_NOT_RUN=true</script>'
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="guide.html")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    catalog, documents = details(service, owner, pid, aid)
    assert catalog["documents"][0]["native_package_id"] == package["artifact_id"]
    assert catalog["documents"][0]["native_index_modified"] is False
    assert catalog["packages"][0]["native_index_modified"] is False
    response = read_http(web, service, owner, pid, documents[0]["revision_id"], aid)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    native = document["native"]
    assert native["package_id"] == package["artifact_id"]
    assert native["package_key"] == package["package_id"] and native["revision_id"] == package["revision_id"]
    assert native["format"] == "html-document" and native["path"] == "guide.html"
    assert native["document_anchor"] == "" and native["matches_index"] is True
    assert native["source_document_revision_id"] == document["revision_id"]
    assert [location["heading_ordinal"] for location in native["sections"]] == [0, 1, 2]
    assert native["sections"][0]["anchor"] == "intro"
    assert native["sections"][1]["selector"] != native["sections"][2]["selector"]
    assert all(
        location["revision_id"] in {section["revision_id"] for section in document["sections"]}
        for location in native["sections"]
    )
    assert InteractiveKnowledgeService(service).content(owner, pid, package["artifact_id"], aid) == raw.encode()


def test_document_site_zip_keeps_html_paths_resources_and_markdown_standard(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    pages = [
        ("docs/other.html", '<h1 id="other">Other page</h1><h2 id="rule">Other rule</h2><p>Other text</p>'),
        (
            "index.html",
            '<link rel="stylesheet" href="assets/style.css"><h1 id="start">Start</h1><a href="docs/other.html#rule">Other rule</a>',
        ),
        ("assets/style.css", "h1{color:red}"),
        ("notes.md", "# Markdown notes\n\nRetain the standard reader."),
    ]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", zip_docs(pages), filename="site.zip")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    assert package["format"] == "static-site"
    catalog, documents = details(service, owner, pid, aid)
    by_title = {row["name"]: row for row in documents}
    native = by_title["Other page"]["native"]
    assert native["path"] == "docs/other.html" and native["format"] == "static-site"
    assert native["sections"][1]["anchor"] == "rule"
    assert by_title["Markdown notes"]["native"] is None
    assert next(row for row in catalog["documents"] if row["name"] == "Markdown notes")["native_package_id"] is None
    from packages.analyzers.offline_prototype import _scripts

    raw = InteractiveKnowledgeService(service).content(owner, pid, package["artifact_id"], aid)
    resources = json.loads(_scripts(raw, {"vks-site": "application/json"})["vks-site"])
    assert {name: base64.b64decode(value["data"]).decode() for name, value in resources.items()} == dict(pages)
    assert b'window.__prototypePack.show("index.html")' in raw


def test_plain_markdown_and_text_never_offer_generated_pre_wrapper(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs([("guide.md", "# Guide"), ("notes.txt", "Text")]),
        filename="docs.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    catalog, documents = details(service, owner, pid, aid)
    assert all(document["native"] is None for document in documents)
    assert all(document["native_package_id"] is None for document in catalog["documents"])
    assert catalog["packages"][0]["format"] == "markdown"


def test_handbook_source_path_maps_exact_article_and_duplicate_heading_ids(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", source_handbook(), filename="book.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    catalog, documents = details(service, owner, pid, aid)
    assert len(catalog["documents"]) == 2
    by_path = {document["native"]["path"]: document for document in documents}
    native = by_path["handbook/b/shared.md"]["native"]
    assert native["document_anchor"] == "article-b"
    assert [location["anchor"] for location in native["sections"]] == [
        "article-b--shared",
        "article-b--duplicate",
        "article-b--duplicate-1",
    ]
    assert all(location["heading_level"] in {3, 4} for location in native["sections"])


def test_manual_index_edits_explicitly_report_source_version_and_keep_history(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", source_handbook(), filename="book.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    catalog, documents = details(service, owner, pid, aid)
    original = next(document for document in documents if document["native"]["path"] == "handbook/a/shared.md")
    entry = next(document for document in catalog["documents"] if document["revision_id"] == original["revision_id"])
    frozen = manager.checkpoint(owner, pid, aid, "Original", "native-original-snapshot")
    service.save_document(
        owner,
        entry["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": original["revision_id"],
            "markdown": original["content"].replace("First rule.", "Authored new rule."),
            "locked_section_ids": [],
        },
        "1",
    )
    _catalog, updated = details(service, owner, pid, aid)
    assert next(row for row in _catalog["documents"] if row["id"] == original["id"])["native_index_modified"] is True
    assert _catalog["packages"][0]["native_index_modified"] is True
    document = next(document for document in updated if document["id"] == original["id"])
    assert document["native"]["matches_index"] is False
    assert document["native"]["source_document_revision_id"] == original["revision_id"]
    assert [location["anchor"] for location in document["native"]["sections"]] == [
        "article-a--shared",
        "article-a--duplicate-1",
    ]
    saved = read_http(web, service, owner, pid, original["revision_id"], release=frozen["id"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["native"] == original["native"]
    replacement = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "<h1>Replacement source</h1>",
        package_id=package["package_id"],
        filename="new.html",
    )
    test_materials.activate(service, owner, manager, pid, aid, [replacement])
    assert (
        read_http(web, service, owner, pid, original["revision_id"], release=frozen["id"]).json()["data"]["native"]
        == original["native"]
    )


def test_native_entry_rechecks_whole_package_permissions_partial_sections_and_project(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", source_handbook(), filename="book.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    _catalog, documents = details(service, owner, pid, aid)  # Populate immutable parse cache.
    public = next(document for document in documents if document["native"]["document_anchor"] == "article-a")
    private = next(document for document in documents if document["native"]["document_anchor"] == "article-b")
    with service.db() as db:
        art = db.get(Artifact, db.get(Revision, private["revision_id"]).artifact_id)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    token = service.create_token(
        owner, pid, {"name": "partial-native", "scopes": ["knowledge:read", "knowledge:edit"]}
    )["token"]
    limited = service.authenticate(token, "token")
    limited_catalog = InteractiveKnowledgeService(service).catalog(limited, pid, aid)
    assert limited_catalog["packages"] == []
    assert limited_catalog["documents"][0]["native_package_id"] is None
    response = read_http(web, service, owner, pid, public["revision_id"], aid, actor_token=token)
    assert response.status_code == 200 and response.json()["data"]["native"] is None
    content_url = f"/api/v3/projects/{pid}/interactive/packages/{package['artifact_id']}/content"
    assert (
        web.get(content_url, params={"analysis_set_id": aid}, headers={"Authorization": "Bearer " + token}).status_code
        == 404
    )
    other_pid, other_aid = projects[1]
    assert read_http(web, service, owner, other_pid, public["revision_id"], other_aid).status_code == 404
    with service.db() as db:
        selection = db.scalar(
            select(Selection).where(
                Selection.analysis_set_id == aid, Selection.entity_revision_id == public["sections"][1]["revision_id"]
            )
        )
        db.delete(selection)
    partial = InteractiveKnowledgeService(service).document(owner, pid, public["revision_id"], aid)
    assert partial["partial"] is True and partial["native"] is None
    assert InteractiveKnowledgeService(service).catalog(owner, pid, aid)["packages"] == []


def test_legacy_handbook_native_entry_before_material_adoption(materials):
    _web, service, owner, _manager, projects = materials
    pid, aid = projects[0]
    payload = {
        "kind": "prototype",
        "artifact_ids": [_upload(service, owner, pid, source_handbook(), "legacy.html")],
        "analysis_set_id": aid,
    }
    parsed = ImportService(service).prepare(actor=owner, pid=pid, payload=payload)
    selected = service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"]
    with service.db() as db:
        job = service._new_job(
            db,
            owner,
            pid,
            "import",
            {
                **payload,
                "_prototype_preview": {
                    "selection_version": selected,
                    "digest": preview_fingerprint(parsed, payload, selected),
                },
            },
        )
        jid = job.id
    service.process_job(jid)
    assert service.get_job(owner, jid)["state"] == "succeeded"
    catalog, documents = details(service, owner, pid, aid)
    assert len(catalog["packages"]) == 1 and catalog["packages"][0]["format"] == "charge-handbook"
    assert all(entry["native_package_id"] == catalog["packages"][0]["id"] for entry in catalog["documents"])
    assert all(entry["native_index_modified"] is False for entry in catalog["documents"])
    assert catalog["packages"][0]["native_index_modified"] is False
    native = next(document["native"] for document in documents if document["native"]["path"] == "handbook/b/shared.md")
    assert native["document_anchor"] == "article-b" and native["revision_id"] == ""


def test_native_index_modified_digest_tracks_manual_path_move_and_source_replacement(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    shared = InteractiveKnowledgeService(service)
    html = '<h1 id="guide">Guide</h1><p>Original source body.</p>'
    first = test_materials.stage(
        service, owner, manager, pid, aid, "documents", zip_docs([("old/guide.html", html)]), filename="docs.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [first])
    entry = shared.catalog(owner, pid, aid)["documents"][0]
    original = shared.document(owner, pid, entry["revision_id"], aid)
    source_digest = sha256(original["content"].encode()).hexdigest()
    with service.db() as db:
        assert db.get(Revision, entry["revision_id"]).metadata_json["source_body_sha256"] == source_digest
    service.save_document(
        owner,
        entry["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": entry["revision_id"],
            "markdown": original["content"] + "\n\n## Manual note\n\nPreserve this authored text.",
            "locked_section_ids": [],
        },
        "1",
    )
    moved = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs([("new/deep/guide.html", html)]),
        package_id=first["package_id"],
        filename="docs.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [moved])
    moved_entry = shared.catalog(owner, pid, aid)["documents"][0]
    assert moved_entry["id"] == entry["id"] and moved_entry["path"] == "new/deep/guide.html"
    assert moved_entry["native_index_modified"] is True
    moved_document = shared.document(owner, pid, moved_entry["revision_id"], aid)
    assert "Preserve this authored text." in moved_document["content"]
    assert moved_document["native"]["matches_index"] is False
    with service.db() as db:
        row = db.get(Revision, moved_entry["revision_id"])
        assert row.metadata_json["source_body_sha256"] == source_digest
        # Old path-move records lacked a source-body digest. Their retained
        # draft provenance must still keep the initial reader conservative.
        row.metadata_json = {key: value for key, value in row.metadata_json.items() if key != "source_body_sha256"}
    assert shared.catalog(owner, pid, aid)["documents"][0]["native_index_modified"] is True
    replacement = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs([("new/deep/guide.html", '<h1 id="guide">Guide</h1><p>New uploaded source body.</p>')]),
        package_id=first["package_id"],
        filename="docs.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [replacement])
    catalog = shared.catalog(owner, pid, aid)
    replaced = shared.document(owner, pid, catalog["documents"][0]["revision_id"], aid)
    assert "New uploaded source body." in replaced["content"]
    assert replaced["native"]["matches_index"] is True
    assert catalog["documents"][0]["native_index_modified"] is False
    assert catalog["packages"][0]["native_index_modified"] is False


def test_native_index_modified_legacy_draft_fallback_and_missing_source_fail_closed(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    shared = InteractiveKnowledgeService(service)
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "<h1>Guide</h1><p>Original.</p>", filename="guide.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    entry = shared.catalog(owner, pid, aid)["documents"][0]
    original = shared.document(owner, pid, entry["revision_id"], aid)
    with service.db() as db:
        row = db.get(Revision, entry["revision_id"])
        row.metadata_json = {key: value for key, value in row.metadata_json.items() if key != "source_body_sha256"}
    assert shared.catalog(owner, pid, aid)["documents"][0]["native_index_modified"] is False
    service.save_document(
        owner,
        entry["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": entry["revision_id"],
            "markdown": original["content"] + "\n\nChanged.",
            "locked_section_ids": [],
        },
        "1",
    )
    changed = shared.catalog(owner, pid, aid)["documents"][0]
    assert changed["native_index_modified"] is True
    with service.db() as db:
        row = db.get(Revision, changed["revision_id"])
        row.metadata_json = {key: value for key, value in row.metadata_json.items() if key != "original_artifact_id"}
        artifact = db.get(Artifact, row.artifact_id)
        artifact.metadata_json = {
            key: value for key, value in artifact.metadata_json.items() if key != "original_artifact_id"
        }
    assert shared.catalog(owner, pid, aid)["documents"][0]["native_index_modified"] is True
