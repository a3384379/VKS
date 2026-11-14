"""Imported navigation order, path identities and permission-first projections."""

import base64
import gzip
import json
import threading
from contextlib import contextmanager
from hashlib import sha256
from html import escape
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image
from vks_api.attachments import resolve
from vks_api.document_navigation import build_navigation
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.materials import ROOT
from vks_api.models import Artifact, Job, Revision, uid
from vks_api.service import DomainError

from tests.exports.test_readonly_library import (
    edit_document,
)
from tests.exports.test_readonly_library import (
    export as export_library,
)
from tests.exports.test_readonly_library import library as configured_library
from tests.exports.test_readonly_library import (
    reader as export_reader,
)
from tests.exports.test_readonly_library import (
    snapshot as fixed_snapshot,
)
from tests.interactive import test_materials

materials = test_materials.materials
CATEGORIES = [
    "阅读指南",
    "系统整体介绍",
    "项目业务说明",
    "技术方案",
    "核心业务流程",
    "线上问题复盘",
    "技术专题",
    "面试资料",
]
COUNTS = [2, 6, 50, 16, 29, 46, 37, 18]


def handbook(groups):
    entries = [
        (f"doc-{g}-{i}", path, title) for g, (_, docs) in enumerate(groups) for i, (path, title) in enumerate(docs)
    ]
    sources = {}
    for key, path, title in reversed(entries):
        raw = ("# " + title + "\n\nOrdinary Markdown.\n").encode()
        sources[key] = {"path": path, "sha256": sha256(raw).hexdigest(), "base64": base64.b64encode(raw).decode()}
    nav = []
    for g, (name, docs) in enumerate(groups):
        items = "".join(
            f'<li class="toc-doc" data-doc="doc-{g}-{i}"><a>{escape(title)}</a></li>'
            for i, (_, title) in enumerate(docs)
        )
        nav.append(f'<section class="toc-group" data-name="{escape(name)}">{items}</section>')
    inner = (
        '<!doctype html><nav class="toc">'
        + "".join(nav)
        + '</nav><script id="source-data" type="application/json">'
        + json.dumps(sources, ensure_ascii=False)
        + "</script>"
    ).encode()
    meta = {"format": "fh-gzip-html-1", "bytes": len(inner), "sha256": sha256(inner).hexdigest()}
    return (
        '<script id="fh-pack-meta" type="application/json">'
        + json.dumps(meta)
        + '</script><script id="fh-pack-data" type="application/octet-stream">'
        + base64.b64encode(gzip.compress(inner)).decode()
        + "</script>"
    ).encode()


def zip_docs(entries):
    result = BytesIO()
    with ZipFile(result, "w") as archive:
        for path, content in entries:
            archive.writestr(path, content)
    return result.getvalue()


def stage_docs(service, owner, manager, pid, aid, raw, *, name="Documents", package_id=None, filename="docs.zip"):
    row = test_materials.stage(
        service, owner, manager, pid, aid, "documents", raw, name=name, package_id=package_id, filename=filename
    )
    test_materials.activate(service, owner, manager, pid, aid, [row])
    return row


def leaves(nodes):
    return [leaf for node in nodes for leaf in ([node] if node["kind"] == "document" else leaves(node["children"]))]


def navigation(service, owner, pid, aid=None, release=None):
    return InteractiveKnowledgeService(service).catalog(owner, pid, aid, release)["navigation"]


def test_handbook_original_eight_categories_and_curated_flat_order_survive_legacy_and_snapshot(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    groups = [
        (
            name,
            [
                (
                    f"{g:02d}_folder/{'nested/' if g == 4 and i > 0 else ''}{i:03d}.md",
                    f"{g:02d} article {count - i:03d}",
                )
                for i in range(count)
            ],
        )
        for g, (name, count) in enumerate(zip(CATEGORIES, COUNTS, strict=True))
    ]
    raw = handbook(groups)
    package = stage_docs(service, owner, manager, pid, aid, raw, filename="handbook.html", name="Original handbook")
    before = navigation(service, owner, pid, aid)
    categories = before["packages"][0]["categories"]
    assert [c["name"] for c in categories] == CATEGORIES
    assert [len(c["children"]) for c in categories] == COUNTS
    assert all(n["kind"] == "document" for c in categories for n in c["children"])
    assert [[n["name"] for n in c["children"]] for c in categories] == [
        [title for _, title in docs] for _, docs in groups
    ]
    with service.db() as db:
        root = db.get(Revision, package["revision_id"])
        metadata = dict(root.metadata_json[ROOT])
        metadata.pop("document_navigation")
        root.metadata_json = {ROOT: metadata}  # Simulate an existing package before navigation metadata.
    assert navigation(service, owner, pid, aid) == before
    frozen = manager.checkpoint(owner, pid, aid, "Original order", uid())
    changed = [(name, list(reversed(docs))) for name, docs in reversed(groups)]
    stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        handbook(changed),
        package_id=package["package_id"],
        filename="handbook.html",
        name="Reordered handbook",
    )
    assert [c["name"] for c in navigation(service, owner, pid, aid)["packages"][0]["categories"]] == list(
        reversed(CATEGORIES)
    )
    assert navigation(service, owner, pid, release=frozen["id"]) == before
    with service.db() as db:
        origin = db.get(Artifact, db.get(Revision, package["revision_id"]).artifact_id)
        assert (service.data_dir / "objects" / origin.object_key).read_bytes() == raw


def test_generic_nested_zip_order_duplicate_paths_and_packages_are_separate(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    entries = [
        ("docs/Z.md", "# Zebra"),
        ("README.md", "# Root intro"),
        ("docs/nested/B.md", "# Nested"),
        ("docs/A.md", "# Alpha"),
        ("other/C.md", "# Other"),
    ]
    first = stage_docs(service, owner, manager, pid, aid, zip_docs(entries), name="First package")
    stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        zip_docs([("docs/Z.md", "# Same path different package")]),
        name="Second package",
    )
    result = navigation(service, owner, pid, aid)["packages"]
    assert [p["name"] for p in result] == ["First package", "Second package"]
    assert [c["name"] for c in result[0]["categories"]] == ["docs", "文档", "other"]
    assert [d["name"] for d in leaves(result[0]["categories"][0]["children"])] == ["Zebra", "Nested", "Alpha"]
    assert result[0]["categories"][0]["children"][1]["kind"] == "folder"
    assert result[1]["categories"][0]["children"][0]["path"] == "docs/Z.md"
    assert result[1]["categories"][0]["children"][0]["id"] != result[0]["categories"][0]["children"][0]["id"]
    assert result[0]["id"] == first["package_id"]


def test_hidden_source_categories_package_names_and_empty_containers_are_not_projected(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        handbook(
            [
                ("Public category", [("public/A.md", "Public article")]),
                ("SECRET_CATEGORY_LABEL", [("private/secret.md", "SECRET_ARTICLE_TITLE")]),
            ]
        ),
        name="SECRET_PACKAGE_LABEL",
        filename="source.html",
    )
    assert "SECRET_CATEGORY_LABEL" in json.dumps(navigation(service, owner, pid, aid))
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    secret = next(doc for doc in catalog["documents"] if doc["name"] == "SECRET_ARTICLE_TITLE")
    with service.db() as db:
        art = db.get(Artifact, db.get(Revision, secret["revision_id"]).artifact_id)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    token = service.create_token(owner, pid, {"name": "limited-nav", "scopes": ["knowledge:read", "knowledge:edit"]})
    limited = service.authenticate(token["token"], "token")
    result = navigation(service, limited, pid, aid)
    encoded = json.dumps(result)
    assert all(
        secret not in encoded
        for secret in ["SECRET_CATEGORY_LABEL", "SECRET_ARTICLE_TITLE", "SECRET_PACKAGE_LABEL", "private/secret.md"]
    )
    assert [n["name"] for p in result["packages"] for c in p["categories"] for n in leaves(c["children"])] == [
        "Public article"
    ]
    with service.db() as db:
        shared = InteractiveKnowledgeService(service)
        _, _, choices, _ = shared.reader.scope(db, owner, pid, aid, None)
        exported = build_navigation(
            service,
            db,
            owner,
            pid,
            choices,
            [d["revision_id"] for d in catalog["documents"]],
            capabilities={"allowed_source": False, "allowed_confidential": False, "allowed_trace": False},
        )
    assert all(
        secret not in json.dumps(exported)
        for secret in ["SECRET_CATEGORY_LABEL", "SECRET_ARTICLE_TITLE", "SECRET_PACKAGE_LABEL"]
    )
    assert package["package_id"] == result["packages"][0]["id"]


def test_moving_unchanged_source_keeps_manual_text_locks_identity_and_exact_image_versions(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]

    def image(color):
        stream = BytesIO()
        Image.new("RGB", (3, 2), color).save(stream, format="PNG")
        return stream.getvalue()

    raw_markdown = "# Guide\n\n![Image](../assets/flow.png)\n"
    first = stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        zip_docs([("docs/guide.md", raw_markdown), ("assets/flow.png", image("red"))]),
    )
    links = InteractiveKnowledgeService(service)
    doc = links.catalog(owner, pid, aid)["documents"][0]
    details = service.get_document(owner, doc["id"], doc["revision_id"])
    current = service.save_document(
        owner,
        doc["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": doc["revision_id"],
            "markdown": raw_markdown + "\n## Local note\n\n**MANUAL_TEXT_MUST_STAY**\n",
            "locked_section_ids": [details["sections"][0]["id"]],
        },
        details["row_version"],
    )
    frozen = manager.checkpoint(owner, pid, aid, "Manual old path", uid())
    old = links.catalog(owner, pid, aid)["documents"][0]
    stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        zip_docs([("new/deep/guide.md", raw_markdown), ("new/assets/flow.png", image("blue"))]),
        package_id=first["package_id"],
    )
    moved = links.catalog(owner, pid, aid)["documents"][0]
    assert moved["id"] == old["id"] and moved["revision_id"] != old["revision_id"]
    assert moved["path"] == "new/deep/guide.md"
    result = service.get_document(owner, moved["id"], moved["revision_id"])
    assert "MANUAL_TEXT_MUST_STAY" in result["markdown"]
    with service.db() as db:
        assert (
            db.get(Revision, moved["revision_id"]).metadata_json["locked_section_ids"]
            == db.get(Revision, current["entity_revision_id"]).metadata_json["locked_section_ids"]
        )
    assert resolve(service, owner, moved["id"], moved["revision_id"], "../assets/flow.png", analysis_set_id=aid)[
        0
    ] == image("blue")
    assert resolve(service, owner, old["id"], old["revision_id"], "../assets/flow.png", release_id=frozen["id"])[
        0
    ] == image("red")
    assert (
        leaves(navigation(service, owner, pid, aid)["packages"][0]["categories"][0]["children"])[0]["path"]
        == "new/deep/guide.md"
    )
    assert (
        leaves(navigation(service, owner, pid, release=frozen["id"])["packages"][0]["categories"][0]["children"])[0][
            "path"
        ]
        == "docs/guide.md"
    )
    with service.db() as db:
        prior = db.get(Artifact, db.get(Revision, old["revision_id"]).artifact_id)
        new_art = db.get(Artifact, db.get(Revision, moved["revision_id"]).artifact_id)
        assert prior.id in new_art.metadata_json["prototype_input_artifact_ids"]
        prior.metadata_json = {**prior.metadata_json, "requires_trace_read": True}
    token = service.create_token(owner, pid, {"name": "limited-editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    assert links.catalog(service.authenticate(token["token"], "token"), pid, aid)["documents"] == []


@pytest.mark.parametrize("cancel_after_parse", [False, True])
def test_real_export_worker_does_not_reenter_transaction_fence_and_honors_postparse_cancel(
    materials, monkeypatch, cancel_after_parse
):
    from vks_api import document_navigation
    from vks_api.reading import ReadingService

    from packages.analyzers import cancellation

    ctx = configured_library.__wrapped__(materials, monkeypatch)
    service, owner, manager, pid, aid = (ctx[k] for k in ("service", "owner", "manager", "pid", "aid"))
    package = stage_docs(
        service,
        owner,
        manager,
        pid,
        aid,
        handbook([("Guide", [("README.md", "Worker source")])]),
        filename="handbook.html",
    )
    with service.db() as db:
        row = db.get(Revision, package["revision_id"])
        meta = dict(row.metadata_json[ROOT])
        meta.pop("document_navigation")
        row.metadata_json = {ROOT: meta}
    frozen = manager.checkpoint(owner, pid, aid, "Worker navigation source", uid())
    job = service.export(
        owner, pid, {"release_id": frozen["id"], "format": "readonly_html", "include_source": True}, uid()
    )
    original_scope, original_db, original_read_db, original_parse = (
        cancellation.cancellation_scope,
        service.db,
        ReadingService.db,
        document_navigation.handbook_navigation,
    )
    local = threading.local()
    main_thread = threading.get_ident()
    reentered, parsed, cancelled = [], [], []

    def forced_scope(callback, interval_seconds=0.1):
        # Force the real processing_cancelled callback at every checkpoint,
        # making even this tiny fixture exercise the production large-file bug.
        return original_scope(
            callback, interval_seconds=0 if callback.__name__ == "processing_cancelled" else interval_seconds
        )

    def parse_source(raw):
        cancellation.checkpoint(force=True)
        result = original_parse(raw)
        cancellation.checkpoint(force=True)
        parsed.append(True)
        return result

    def cancel_at_read_boundary():
        if (
            cancel_after_parse
            and parsed
            and not cancelled
            and not getattr(local, "depth", 0)
            and not getattr(local, "read_depth", 0)
            and threading.get_ident() == main_thread
        ):
            # Cancellation becomes durable immediately after the source read
            # transaction exits, before the worker may send any embedding call.
            cancelled.append(True)
            with original_db() as db:
                current = db.get(Job, job["id"])
                current.cancel_requested = True

    @contextmanager
    def guarded_db():
        if getattr(local, "depth", 0) or getattr(local, "read_depth", 0):
            reentered.append(True)
            raise AssertionError("Job writer fence re-entered the open document transaction")
        local.depth = 1
        try:
            with original_db() as db:
                yield db
        finally:
            local.depth = 0
        cancel_at_read_boundary()

    @contextmanager
    def guarded_read_db(reading):
        local.read_depth = getattr(local, "read_depth", 0) + 1
        try:
            with original_read_db(reading) as db:
                yield db
        finally:
            local.read_depth -= 1
        cancel_at_read_boundary()

    monkeypatch.setattr(cancellation, "cancellation_scope", forced_scope)
    monkeypatch.setattr(document_navigation, "handbook_navigation", parse_source)
    monkeypatch.setattr(service, "db", guarded_db)
    monkeypatch.setattr(ReadingService, "db", guarded_read_db)
    service.process_job(job["id"])
    outcome = service.get_job(owner, job["id"])
    assert parsed and not reentered
    if cancel_after_parse:
        assert outcome["state"] == "cancelled" and not ctx["calls"]
        with original_db() as db:
            assert "object_key" not in db.get(Job, job["id"]).result_json
    else:
        assert outcome["state"] == "succeeded", outcome
        assert ctx["calls"]


def test_export_tracks_document_navigation_runtime_and_original_unselected_members(materials, monkeypatch):
    ctx = configured_library.__wrapped__(materials, monkeypatch)
    service, _owner, pid = (ctx[k] for k in ("service", "owner", "pid"))
    edit_document(ctx, "# 收费项\n\n当前人工维护的可见正文。")
    fixed = fixed_snapshot(ctx)
    limited = export_reader(ctx)
    payload, _raw, _name, job = export_library(ctx, fixed, actor=limited)
    with service.db() as db:
        root = db.get(Revision, ctx["document"]["revision_id"])
        label, root_id, runtime_id = root.name, root.id, root.artifact_id
        old_members = set(root.metadata_json[ROOT]["members"])
        stored = db.get(Job, job["id"])
        assert stored.input_json["_export_navigation_scope_version"] == 1
        assert {root_id, *old_members} <= set(stored.input_json["_export_revision_ids"])
        assert runtime_id in stored.input_json["_export_artifact_ids"]
        assert (
            next(package for package in payload["navigation"]["packages"] if package["id"] == root.entity_id)["name"]
            == label
        )
        artifact = db.get(Artifact, runtime_id)
        artifact.metadata_json = {**artifact.metadata_json, "requires_source_read": True}
    with pytest.raises(DomainError) as exc:
        service.download_export(limited, pid, job["id"])
    assert exc.value.status == 403
    fresh, *_ = export_library(ctx, fixed, actor=limited)
    assert "当前人工维护" in fresh["documents"][0]["markdown"]
    assert all(package["name"] != label for package in fresh["navigation"]["packages"])


def test_old_readonly_navigation_scope_is_conservative_without_affecting_owner_or_other_formats(materials, monkeypatch):
    ctx = configured_library.__wrapped__(materials, monkeypatch)
    service, owner, pid = (ctx[k] for k in ("service", "owner", "pid"))
    fixed = fixed_snapshot(ctx)
    limited = export_reader(ctx)
    _payload, _raw, _name, job = export_library(ctx, fixed, actor=limited)
    with service.db() as db:
        stored = db.get(Job, job["id"])
        metadata = dict(stored.input_json)
        metadata.pop("_export_navigation_scope_version")
        stored.input_json = metadata
    with pytest.raises(DomainError) as exc:
        service.download_export(limited, pid, job["id"])
    assert exc.value.status == 403 and "旧成品库" in exc.value.message
    _payload, raw, _name, owned = export_library(ctx, fixed, actor=owner)
    with service.db() as db:
        stored = db.get(Job, owned["id"])
        metadata = dict(stored.input_json)
        metadata.pop("_export_navigation_scope_version")
        stored.input_json = metadata
    assert service.download_export(owner, pid, owned["id"])[0] == raw
