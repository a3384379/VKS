"""A fixed, permission-scoped library and its credential-free single-file reader.

Imported prototypes execute only inside the recipient's isolated browser frame.
Markdown, relationships and vectors are projected from VKS, never from a source
handbook's UI, scripts, storage or proprietary search index.
"""

from __future__ import annotations

import base64
import gzip
import json
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .documents import DocumentService
from .exports import allow_artifact, canonical, digest, document_export_choices, read_original
from .interactive import InteractiveKnowledgeService
from .models import Analysis, Artifact, Evidence, Job, Project, Release, Revision
from .prototype_edit import ordered_citation_edges
from .service import DomainError


def document_images(service, s, actor, pid, revision, sections, caps, mounted_attachments=None, markdown=None):
    """Resolve the same exact, permission-scoped image assets as Studio."""
    from .attachments import find_attachment, raster_media

    refs = set()
    source = markdown if markdown is not None else "\n\n".join(c["content"] for c in sections)
    for token in MarkdownIt("commonmark").parse(source):
        for child in [token, *(token.children or [])]:
            if child.type == "image":
                refs.add(child.attrGet("src") or "")
            elif child.type in {"html_inline", "html_block"}:
                for image in BeautifulSoup(child.content, "html.parser").find_all("img", src=True):
                    refs.add(image["src"])
    images, dependencies, missing = {}, set(), 0
    for reference in sorted(refs):
        if reference.startswith("data:"):
            continue
        try:
            attachment, _ = find_attachment(s, revision, reference, mounted_attachments)
        except DomainError:
            missing += 1
            continue
        if not attachment or not allow_artifact(attachment, caps, s):
            missing += 1
            continue
        raw = read_original(service, attachment)
        try:
            media = raster_media(raw)
        except DomainError:
            missing += 1
            continue
        images[reference] = "data:" + media + ";base64," + base64.b64encode(raw).decode()
        dependencies.add(attachment.id)
    return images, dependencies, missing


def capabilities(service, s, actor, job):
    service.require(s, actor, job.project_id, "knowledge:export")
    return {
        "allowed_source": bool(job.input_json.get("include_source"))
        and service._can(s, actor, job.project_id, "source:read"),
        "allowed_confidential": service._can(s, actor, job.project_id, "source:read"),
        "allowed_trace": service._can(s, actor, job.project_id, "trace:read"),
    }


def projection(service, s, actor, pid, release_id, caps, document_mode="project", document_ids=None):
    """Read an immutable release using the same closure as Studio, before names."""
    from .materials import ROOT, roots

    links = InteractiveKnowledgeService(service)
    _, release, choices, _ = links.reader.scope(s, actor, pid, None, release_id)
    choices = document_export_choices(s, choices, document_ids)
    if document_mode not in {"project", "original"}:
        raise DomainError("VKS_INVALID_INPUT", "文档导出方式无效", 422)
    from .document_presentation import presentation_for_release

    presentation = presentation_for_release(s, release)
    original_requested = document_mode == "original" or presentation["mode"] == "original"
    original = original_requested or presentation["mode"] == "auto"
    rows = {
        row.id: (entity, row)
        for entity, row in links._rows(s, actor, pid, choices)
        if allow_artifact(s.get(Artifact, row.artifact_id) if row.artifact_id else None, caps, s)
    }
    mounted_documents = {
        r.entity_id: r.metadata_json[ROOT]
        for r in roots(s, pid, choices)
        if r.metadata_json[ROOT]["kind"] == "documents"
    }
    packages, artifacts, revisions = [], set(), set()
    for package in links._packages(s, actor, pid, choices):
        if package["kind"] != "prototype":
            continue
        art = s.get(Artifact, package["id"])
        if not allow_artifact(art, caps, s):
            continue
        root = s.get(Revision, package.get("revision_id")) if package.get("revision_id") else None
        members = root.metadata_json.get(ROOT, {}).get("members", []) if root else []
        if root and (
            root.id not in rows
            or any(
                not (member := s.get(Revision, rid))
                or not allow_artifact(s.get(Artifact, member.artifact_id) if member.artifact_id else None, caps, s)
                for rid in members
            )
        ):
            continue
        packages.append((package, art, root))
        artifacts.add(art.id)
        if root:
            revisions.update([root.id, *members])
    documents = []
    missing_images = 0
    section_ids = set()
    for entity, row in rows.values():
        if entity.type != "document":
            continue
        from .document_projection import readable_document

        projected = readable_document(row, DocumentService._sections(s, entity, row), set(rows))
        sections = [{**c, "id": c["revision_id"], "title": c["name"]} for c in projected["sections"]]
        if not sections:
            continue
        material = mounted_documents.get(row.metadata_json.get("material_package_id"), {})
        images, image_dependencies, absent = document_images(
            service,
            s,
            actor,
            pid,
            row,
            sections,
            caps,
            material.get("attachment_artifact_ids"),
            markdown=projected["content"],
        )
        artifacts.update(image_dependencies)
        missing_images += absent
        # Do not leak unselected/hidden chapter text through document.content.
        documents.append(
            {
                "id": row.id,
                "title": row.name,
                "package_id": row.metadata_json.get("material_package_id"),
                "namespace": row.metadata_json.get("material_package_id")
                or row.metadata_json.get("logical_source_key")
                or s.get(Artifact, row.artifact_id).snapshot_id,
                "path": row.metadata_json.get("material_path") or s.get(Artifact, row.artifact_id).relative_path,
                "images": images,
                "markdown": projected["content"],
                "partial": projected["partial"],
                "sections": sections,
            }
        )
        revisions.add(row.id)
        section_ids.update(c["id"] for c in sections)
    revisions.update(section_ids)
    package_ids = {p["id"] for p, _, _ in packages}
    edges = [
        edge
        for edge in service._relations(s, s.get(Analysis, release.analysis_set_id), release)
        if edge["from_revision_id"] in rows
        and edge["to_revision_id"] in rows
        and not edge["excluded"]
        and edge["validity_status"] == "valid"
        and service._visible_relation(s, actor, pid, edge)
        and all(
            (ev := s.get(Evidence, eid))
            and service._visible_evidence(s, actor, pid, ev)
            and allow_artifact(s.get(Artifact, ev.artifact_id), caps, s)
            for eid in edge.get("evidence_ids", [])
        )
    ]
    citation_edges = {}
    for edge in edges:
        if edge["relation_type"] == "supported_by_section" and edge["to_revision_id"] in section_ids:
            citation_edges.setdefault(edge["from_revision_id"], []).append(edge)
    bindings, relation_ids, evidence_ids = [], set(), set()
    for edge in edges:
        if edge["relation_type"] != "has_business_point":
            continue
        action = rows[edge["from_revision_id"]][1]
        anchor = action.metadata_json.get("interactive_anchor", {})
        if anchor.get("package_id") not in package_ids:
            continue
        point = rows[edge["to_revision_id"]][1]
        citations = ordered_citation_edges(point, citation_edges.get(point.id, []))
        if not citations:
            continue
        bindings.append(
            {
                "id": edge["id"],
                "package_id": anchor["package_id"],
                "target": anchor["target"],
                "scope": "module" if anchor["target"].get("selector") == "body" else "control",
                "title": point.name,
                "summary": point.content,
                "section_ids": [c["to_revision_id"] for c in citations],
                "origin": point.metadata_json.get("origin", "manual"),
            }
        )
        revisions.update([action.id, point.id])
        for dep in [edge, *citations]:
            relation_ids.add(dep["id"])
            evidence_ids.update(dep.get("evidence_ids", []))
    for eid in evidence_ids:
        ev = s.get(Evidence, eid)
        if (
            not ev
            or not service._visible_evidence(s, actor, pid, ev)
            or not allow_artifact(s.get(Artifact, ev.artifact_id), caps, s)
        ):
            raise DomainError("VKS_FORBIDDEN", "关联出处不能用于本次导出", 403)
        artifacts.add(ev.artifact_id)
    from .document_navigation import build_navigation

    navigation_dependencies = {"revision_ids": set(), "artifact_ids": set()}
    navigation = build_navigation(
        service,
        s,
        actor,
        pid,
        choices,
        [document["id"] for document in documents],
        capabilities=caps,
        dependencies=navigation_dependencies,
    )
    revisions.update(navigation_dependencies["revision_ids"])
    artifacts.update(navigation_dependencies["artifact_ids"])
    native_documents, native_warnings = [], []
    if original:
        from .native_export import project_originals

        native_documents, native_warnings, native_revisions, native_artifacts = project_originals(
            service, s, actor, pid, choices, links, rows, documents, caps, warn_missing=original_requested
        )
        revisions.update(native_revisions)
        artifacts.update(native_artifacts)
    for rid in revisions:
        row = s.get(Revision, rid)
        if row.artifact_id:
            artifacts.add(row.artifact_id)
    project = s.get(Project, pid)

    payload = {
        "schema_version": 1,
        "project": {"id": pid, "name": project.name},
        "snapshot": {
            "id": release.id,
            "name": release.release_no,
            "created_at": str(release.created_at),
            "sha256": release.manifest_sha256,
        },
        "prototypes": [
            {
                "id": p["id"],
                "name": p["name"],
                "format": p.get("format", "html"),
                "html": read_original(service, art).decode("utf-8-sig"),
                "targets": root.metadata_json.get(ROOT, {}).get("targets", []) if root else [],
            }
            for p, art, root in packages
        ],
        "documents": sorted(documents, key=lambda d: d["title"]),
        "document_mode": document_mode,
        "original_requested": original_requested,
        "reader_mode": "original"
        if original_requested or native_documents
        else ("handbook" if presentation.get("mode") == "handbook" else "standard"),
        "native_documents": native_documents,
        "bindings": bindings,
        "navigation": navigation,
        "warnings": ([f"{missing_images} 张文档引用图片未包含在可用资料中，保留原文说明。"] if missing_images else [])
        + native_warnings,
        # Recipient chooses their own connection. Never serialize publisher settings.
        "ai": {"base_url": "https://api.openai.com/v1", "protocol": "responses", "model": ""},
    }
    deps = {
        "_export_revision_ids": sorted(revisions),
        "_export_relation_ids": sorted(relation_ids),
        "_export_evidence_ids": sorted(evidence_ids),
        "_export_artifact_ids": sorted(artifacts),
        "_export_source_snapshot_ids": [],
        "_export_navigation_scope_version": 1,
    }
    return payload, deps, sorted(section_ids)


def html_library(payload):
    """Embed data as inert gzip/base64 and platform-owned runtime as inline code."""
    assets = Path(__file__).with_name("reader_assets")
    from .document_assistant import embedded_assistant
    from .interactive_ai import EXPLANATION_GUIDANCE

    template = (assets / "template.html").read_text()
    encoded = base64.b64encode(gzip.compress(canonical(payload), compresslevel=9, mtime=0)).decode()
    # Replacement markers occur only in platform-owned template, never in data.
    return (
        template.replace(
            "<title>VKS 成品知识库</title>", "<title>" + escape(payload["project"]["name"]) + " · 成品知识库</title>"
        )
        .replace(
            "/* VKS_READER_CSS */",
            (assets / "reader.css").read_text() + (assets / "assistant-composer.css").read_text(),
        )
        .replace("/* VKS_HANDBOOK_CSS */", (assets / "handbook.css").read_text())
        .replace("/* VKS_MARKDOWN_CSS */", (assets / "markdown-vendor.css").read_text())
        .replace("VKS_EXPLANATION_GUIDANCE", json.dumps(EXPLANATION_GUIDANCE, ensure_ascii=False).replace("<", "\\u003c"))
        .replace("VKS_READER_PAYLOAD", encoded)
        .replace("VKS_DOCUMENT_AI_SOURCE", embedded_assistant())
        .replace("/* VKS_READER_JS */", (assets / "reader.js").read_text())
        .replace("/* VKS_ASSISTANT_JS */", (assets / "assistant-runtime.js").read_text())
        .replace("/* VKS_HANDBOOK_JS */", (assets / "handbook.js").read_text())
        .replace("/* VKS_MARKDOWN_JS */", (assets / "markdown-vendor.js").read_text())
        .replace(
            "VKS_MARKDOWN_LICENSES",
            json.dumps((assets / "markdown-licenses.txt").read_text(), ensure_ascii=False).replace("<", "\\u003c"),
        )
    ).encode()


def prepare_job(service, jid, validate_scope=None):
    """Network and compression run outside the knowledge transaction/worker lock."""
    from .interactive_ai import InteractiveAssistant
    from .jobs import current_actor
    from .reading import ReadingService

    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        actor = current_actor(service, job.created_by, job.project_id, session=s, job=job)
        caps = capabilities(service, s, actor, job)
        pid, release_id = job.project_id, job.input_json["release_id"]
        mode = job.input_json.get("document_mode", "project")
        document_ids = job.input_json.get("document_ids")
        payload, deps, section_ids = projection(service, s, actor, pid, release_id, caps, mode, document_ids)
        before = digest(canonical([payload, deps]))
    if validate_scope:
        validate_scope()
    rag = InteractiveAssistant(service).rag.export_index(
        actor, pid, release_id=release_id, section_revision_ids=section_ids, ensure=True, validate_scope=validate_scope
    )
    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        actor = current_actor(service, job.created_by, pid, session=s, job=job)
        caps_now = capabilities(service, s, actor, job)
        latest, current_deps, _ = projection(service, s, actor, pid, release_id, caps_now, mode, document_ids)
        if caps != caps_now or before != digest(canonical([latest, current_deps])):
            raise DomainError("VKS_VERSION_CONFLICT", "导出期间资料或权限发生变化，请重新导出", 409)
    if validate_scope:
        validate_scope()
    payload["rag"] = rag
    return {
        "object_key": service._put_object(html_library(payload)),
        "deps": deps,
        "caps": caps,
        "projection_hash": before,
    }


def finalize(service, s, job, prepared):
    from .jobs import current_actor

    if not prepared:
        raise DomainError("VKS_STATE_CONFLICT", "成品库未完成预构建", 409)
    actor = current_actor(service, job.created_by, job.project_id, session=s, job=job)
    caps = capabilities(service, s, actor, job)
    payload, deps, _ = projection(
        service,
        s,
        actor,
        job.project_id,
        job.input_json["release_id"],
        caps,
        job.input_json.get("document_mode", "project"),
        job.input_json.get("document_ids"),
    )
    if caps != prepared["caps"] or digest(canonical([payload, deps])) != prepared["projection_hash"]:
        raise DomainError("VKS_VERSION_CONFLICT", "导出期间资料或权限发生变化，请重新导出", 409)
    release = s.get(Release, job.input_json["release_id"])
    job.input_json = dict(job.input_json, **caps, **deps)
    return {
        "result_refs": [{"kind": "export", "id": job.id}],
        "download_url": f"/api/v1/projects/{job.project_id}/exports/{job.id}/download",
        "object_key": prepared["object_key"],
        "filename": f"vks-{release.release_no}-readonly.html",
        "format": "readonly_html",
    }
