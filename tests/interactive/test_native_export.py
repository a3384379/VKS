"""Original-document exports keep source appearance and exact permission closure."""

import base64
import gzip
import json
from hashlib import sha256

from sqlalchemy import select
from vks_api.document_presentation import PresentationInput, PresentationService
from vks_api.documents import DocumentService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Entity, Revision, Selection, uid
from vks_api.native_documents import _headings
from vks_api.readonly_export import projection

from tests.exports import test_readonly_library
from tests.interactive import test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents import source_handbook
from tests.interactive.test_template_import import content as authored_content
from tests.interactive.test_template_import import html as authored_html

materials = test_materials.materials
library = test_readonly_library.library
CAPS = {"allowed_source": True, "allowed_confidential": True, "allowed_trace": True}


def test_source_outline_keeps_exact_inline_text_and_ambiguous_ids_use_structural_positions():
    headings = _headings(
        '<h1 id="intro">A <strong>bold</strong> &amp; <code>literal</code></h1>'
        '<template><h2 id="hidden">Hidden template</h2></template><h2 id="rule">Rule</h2>'
    )
    assert [item["heading"] for item in headings] == ["A bold & literal", "Rule"]
    assert [item["anchor"] for item in headings] == ["intro", "rule"]
    duplicate = _headings('<main><p id="same">Duplicate id</p><h1 id="same">Exact heading</h1></main>')
    assert duplicate[0]["anchor"] == "" and duplicate[0]["selector"] == "main:nth-of-type(1) > h1:nth-of-type(1)"


def project(materials, raw, filename="guide.html", *, mode="original", actor=None, caps=None):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    staged = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename=filename)
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    fixed = manager.checkpoint(owner, pid, aid, "Source export", uid())
    with service.db() as s:
        payload, deps, sections = projection(service, s, actor or owner, pid, fixed["id"], caps or CAPS, mode)
    return payload, deps, sections, staged


def test_original_html_is_self_contained_and_explicit_standard_mode_is_preserved(materials):
    raw = '<title>Original title</title><style>article{color:rgb(12,34,56)}</style><article><h1 id="intro">Original</h1><p>Exact body.</p><h2 id="rule">Rule</h2><p>Exact rule.</p></article>'
    payload, deps, sections, staged = project(materials, raw)
    assert payload["reader_mode"] == "original"
    assert len(payload["native_documents"]) == 1
    source = payload["native_documents"][0]
    assert source["source_sha256"] == sha256(raw.encode()).hexdigest()
    assert "rgb(12,34,56)" in source["pages"]["guide.html"]
    document = payload["documents"][0]
    assert document["native"]["package_id"] == staged["artifact_id"]
    mapped = {section["revision_id"] for section in document["native"]["sections"]}
    assert mapped <= set(sections) and len(mapped) == 2
    assert staged["artifact_id"] in deps["_export_artifact_ids"]
    assert staged["revision_id"] in deps["_export_revision_ids"]
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    version = InteractiveKnowledgeService(service).catalog(owner, pid, aid)["selection_version"]
    PresentationService(service).update(
        owner, pid, PresentationInput(analysis_set_id=aid, mode="standard"), str(version), uid()
    )
    fixed = manager.checkpoint(owner, pid, aid, "Standard export", uid())
    with service.db() as s:
        standard, _, _ = projection(service, s, owner, pid, fixed["id"], CAPS)
    assert standard["reader_mode"] == "standard" and not standard["native_documents"]
    assert "native" not in standard["documents"][0]


def test_project_auto_uses_compatible_original_html_and_plain_md_stays_standard(materials):
    payload, _, _, _ = project(materials, '<h1 id="home">Home</h1><p>Original body.</p>', mode="project")
    assert payload["reader_mode"] == "original" and payload["documents"][0]["native"]
    _, service, owner, manager, projects = materials
    pid, aid = projects[1]
    staged = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Plain\n\nExact plain document.")
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    fixed = manager.checkpoint(owner, pid, aid, "Plain auto", uid())
    with service.db() as s:
        plain, _, _ = projection(service, s, owner, pid, fixed["id"], CAPS)
    assert plain["reader_mode"] == "standard" and not plain["native_documents"] and not plain["warnings"]


def test_source_site_keeps_local_css_images_and_page_links(materials):
    raw = zip_docs(
        [
            (
                "index.html",
                '<title>Home</title><link rel="stylesheet" href="assets/theme.css"><h1 id="home">Home</h1><p>Read here.</p><a href="next.html#rule">Next</a><img src="assets/icon.png">',
            ),
            ("next.html", '<title>Next</title><h1 id="rule">Rule</h1><p>Second page.</p>'),
            ("assets/theme.css", "h1{color:rgb(12,34,56)}"),
            (
                "assets/icon.png",
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII="
                ),
            ),
        ]
    )
    payload, _, _, _ = project(materials, raw, "site.zip")
    source = payload["native_documents"][0]
    assert set(source["pages"]) == {"index.html", "next.html"}
    first = source["pages"]["index.html"]
    assert "data:text/css;base64," in first and "data:image/png;base64," in first
    assert 'data-vks-native-path="next.html"' in first and 'data-vks-native-anchor="rule"' in first
    assert len([doc for doc in payload["documents"] if doc.get("native")]) == 2


def test_unknown_executable_source_falls_back_without_serializing_source_script(materials):
    raw = "<h1>Guide</h1><p>Readable source.</p><script>window.UNKNOWN_EXECUTABLE_SOURCE=true</script>"
    payload, _, _, _ = project(materials, raw)
    assert not payload["native_documents"] and not payload["documents"][0].get("native")
    assert "UNKNOWN_EXECUTABLE_SOURCE" not in json.dumps(payload)
    assert any("未适配" in warning for warning in payload["warnings"])


def test_source_refresh_navigation_uses_offline_standard_fallback(materials):
    raw = '<meta http-equiv="Refresh" content="0;url=https://example.invalid/source"><h1>Guide</h1><p>Current readable body.</p>'
    payload, _, _, _ = project(materials, raw)
    assert not payload["native_documents"]
    assert payload["documents"][0]["native_fallback"] == "unsupported_source"
    assert "example.invalid" not in json.dumps(payload)


def test_authoring_original_is_never_embedded_as_an_editable_readonly_source(materials):
    from vks_api.readonly_export import html_library

    payload, _, _, _ = project(materials, authored_html(authored_content()), "authored.html")
    assert not payload.get("authoring") and not payload["native_documents"]
    assert payload["warnings"]
    assert "MUST_NOT_EXECUTE" not in json.dumps(payload)
    assert any("原始 **加粗** 保留。" in doc["markdown"] for doc in payload["documents"])
    raw = html_library(payload)
    assert b'id="vks-authoring-runtime"' not in raw and b'id="vks-template-data"' not in raw


def test_single_html_zip_uses_its_resource_wrapper_without_treating_it_as_source_script(materials):
    raw = zip_docs(
        [
            ("index.html", '<h1 id="guide">Guide</h1><link rel="stylesheet" href="theme.css"><p>Exact original.</p>'),
            ("theme.css", "h1{font-weight:700}"),
        ]
    )
    payload, _, _, staged = project(materials, raw, "single.zip")
    assert staged["format"] == "html-document"
    assert len(payload["native_documents"]) == 1
    assert "data:text/css;base64," in payload["native_documents"][0]["pages"]["index.html"]


def test_modified_current_text_and_partial_selection_do_not_leak_old_whole_source(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = "<h1>Guide</h1><p>Old private body.</p><h2>Rule</h2><p>Old second section.</p>"
    staged = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="guide.html")
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    current = InteractiveKnowledgeService(service).catalog(owner, pid, aid)["documents"][0]
    DocumentService(service).save_draft(
        owner,
        current["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": current["revision_id"],
            "markdown": "# Guide\n\nCurrent exact replacement.",
            "locked_section_ids": [],
        },
        1,
    )
    fixed = manager.checkpoint(owner, pid, aid, "Edited", uid())
    with service.db() as s:
        payload, _, _ = projection(service, s, owner, pid, fixed["id"], CAPS, "original")
    assert not payload["native_documents"]
    assert "Old private body" not in json.dumps(payload)
    assert "Current exact replacement" in payload["documents"][0]["markdown"]
    assert any("已修改" in message for message in payload["warnings"])


def test_partial_document_scope_refuses_original_package_bytes(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    staged = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "<h1>Guide</h1><p>Visible body.</p><h2>Hidden</h2><p>HIDDEN_ORIGINAL_BODY</p>",
        filename="guide.html",
    )
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    complete = manager.checkpoint(owner, pid, aid, "Complete before partial selection", uid())
    with service.db() as s:
        warm, _, _ = projection(service, s, owner, pid, complete["id"], CAPS, "original")
    assert warm["native_documents"]
    with service.db() as s:
        hidden = s.scalar(
            select(Revision)
            .join(Entity, Entity.id == Revision.entity_id)
            .join(Selection, Selection.entity_revision_id == Revision.id)
            .where(Selection.analysis_set_id == aid, Entity.type == "section", Revision.name == "Hidden")
        )
        s.delete(s.get(Selection, (aid, hidden.entity_id)))
    fixed = manager.checkpoint(owner, pid, aid, "Partial", uid())
    with service.db() as s:
        payload, deps, _ = projection(service, s, owner, pid, fixed["id"], CAPS, "original")
    assert not payload["native_documents"]
    # The source artifact may remain a permission dependency of navigation,
    # while none of its complete bytes enter the exported native payload.
    assert staged["artifact_id"] in deps["_export_artifact_ids"]
    assert "HIDDEN_ORIGINAL_BODY" not in json.dumps(payload)


def test_known_handbook_retains_source_reader_without_original_ai_or_updater(materials):
    packed = source_handbook().decode()
    data = packed.split('<script id="fh-pack-data" type="application/octet-stream">')[1].split("</script>")[0]
    inner = gzip.decompress(base64.b64decode(data))
    inner += b'<div id="fh-ai-root"><input id="fh-ai-key" value="SOURCE_RECIPIENT_KEY"></div><script id="fh-ai-runtime">window.SOURCE_UPDATER_EXECUTED=true;</script><input id="fh-update-file" type="file"><button id="fh-update-save">Update source</button>'
    metadata = {"format": "fh-gzip-html-1", "bytes": len(inner), "sha256": sha256(inner).hexdigest()}
    raw = (
        '<script id="fh-pack-meta" type="application/json">'
        + json.dumps(metadata)
        + '</script><script id="fh-pack-data" type="application/octet-stream">'
        + base64.b64encode(gzip.compress(inner)).decode()
        + "</script>"
    )
    payload, _, _, _ = project(materials, raw, "handbook.html")
    assert len(payload["native_documents"]) == 1
    source = payload["native_documents"][0]["pages"]["__handbook__"]
    assert 'id="article-a"' in source and 'id="fh-book-pages"' in source
    assert "SOURCE_RECIPIENT_KEY" not in source and "SOURCE_UPDATER_EXECUTED" not in source
    assert 'disabled="" hidden="" id="fh-update-save"' in source
    assert all(doc["native"]["page"] == "__handbook__" for doc in payload["documents"])


def test_compressed_original_exports_through_durable_job_with_live_scope_checks(library, monkeypatch):
    from vks_api import readonly_export

    from packages.analyzers import cancellation, offline_prototype

    ctx = library
    service, owner, manager, pid, aid = (ctx[key] for key in ("service", "owner", "manager", "pid", "aid"))
    original = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        source_handbook(),
        filename="compressed-handbook.html",
        package_id=ctx["document"]["package_id"],
    )
    test_materials.activate(service, owner, manager, pid, aid, [original])
    fixed = manager.checkpoint(owner, pid, aid, "Durable compressed native export", uid())
    original_projection, original_prepare = readonly_export.projection, readonly_export.prepare_job
    reading, checks = {"active": False}, []

    def projection_scope(service, session, *args, **kwargs):
        if reading["active"]:
            assert session.info.get("vks_read_only"), "Prebuild must not reserve the SQLite writer"
            checks.append("projection")
        return original_projection(service, session, *args, **kwargs)

    def prepare_with_scope(service, jid, validate_scope=None):
        assert callable(validate_scope), "Exercise the durable worker's real cancellation/authorization callback"
        reading["active"] = True
        try:
            return original_prepare(service, jid, validate_scope)
        finally:
            reading["active"] = False

    monkeypatch.setattr(readonly_export, "projection", projection_scope)
    monkeypatch.setattr(readonly_export, "prepare_job", prepare_with_scope)
    # Even this small gzip fixture must traverse the real job fence while its
    # source-reader projection transaction is open.
    monkeypatch.setattr(offline_prototype, "checkpoint", lambda: cancellation.checkpoint(force=True))
    job = service.export(
        owner,
        pid,
        {"release_id": fixed["id"], "format": "readonly_html", "include_source": True, "document_mode": "original"},
        uid(),
    )
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded", result
    assert len(checks) == 2
    raw, _ = service.download_export(owner, pid, job["id"])
    assert raw.startswith(b"<!doctype html>") and ctx["calls"]
