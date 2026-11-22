"""A fixed, permission-scoped library and its credential-free single-file reader.

Imported prototypes execute only inside the recipient's isolated browser frame.
Markdown, relationships and vectors are projected from VKS, never from a source
handbook's UI, scripts, storage or proprietary search index.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import zlib
from collections import defaultdict
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from sqlalchemy import select

from .connector_source import ROOT
from .documents import DocumentService
from .exports import allow_artifact, canonical, digest, document_export_choices, read_original
from .interactive import InteractiveKnowledgeService
from .models import Analysis, Artifact, Evidence, Job, Project, Release, Revision
from .prototype_edit import ordered_citation_edges
from .service import DomainError

# isort: split
from vks_connect.library import MAX_DATA_BYTES, MAX_FILE_BYTES, decode_envelope
from vks_connect.library_vectors import LibraryVectors
from vks_connect.local_embeddings import DIMENSIONS, MODEL, PROFILE, LocalModelError, build_local_index, encoder


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


def projection(service, s, actor, pid, release_id, caps, document_mode="project", document_ids=None, *, explanation_cutoff=None):
    """Read an immutable release using the same closure as Studio, before names."""
    from .explanation_batches import saved_explanations
    from .materials import ROOT, roots
    from .reading import artifact_policy

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
    model_section_ids = set()
    model_artifacts = {}

    def model_eligible(artifact_id):
        if not artifact_id:
            return False
        if artifact_id not in model_artifacts:
            artifact = s.get(Artifact, artifact_id)
            policy = artifact_policy(s, artifact)
            model_artifacts[artifact_id] = bool(
                artifact and artifact.sensitivity != "confidential" and artifact.kind not in {"source", "trace", "capture"}
                and not policy.get("requires_source_read") and not policy.get("requires_trace_read")
            )
        return model_artifacts[artifact_id]

    for entity, row in rows.values():
        if entity.type != "document":
            continue
        from .document_projection import readable_document

        projected = readable_document(row, DocumentService._sections(s, entity, row), set(rows))
        sections = [{**c, "id": c["revision_id"], "title": c["name"]} for c in projected["sections"]]
        if not sections:
            continue
        if model_eligible(row.artifact_id):
            model_section_ids.update(
                section["id"] for section in sections if model_eligible(rows[section["id"]][1].artifact_id)
            )
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
    bindings, explanation_refs, relation_ids, evidence_ids = [], [], set(), set()
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
        explanation_refs.append({
            **bindings[-1], "point_revision_id": point.id,
        })
        revisions.update([action.id, point.id])
        for dep in [edge, *citations]:
            relation_ids.add(dep["id"])
            evidence_ids.update(dep.get("evidence_ids", []))
    explanations = saved_explanations(service, s, pid, explanation_refs, caps, cutoff=explanation_cutoff)
    for binding in bindings:
        if saved := explanations.get(binding["id"]):
            artifact_id, answer = saved
            artifacts.add(artifact_id)
            binding["saved_explanation"] = answer
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
        "chatgpt_plan": {"section_ids": sorted(model_section_ids)},
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


def html_library(payload, *, encoded=None):
    """Embed data as inert gzip/base64 and platform-owned runtime as inline code."""
    assets = Path(__file__).with_name("reader_assets")
    assistant_assets = Path(__file__).resolve().parents[3] / "packages/assistant-runtime/src"
    from .document_assistant import embedded_assistant
    from .interactive_ai import EXPLANATION_GUIDANCE

    template = (assets / "template.html").read_text()
    if encoded is None:
        data = canonical(payload)
        if "local_rag" in payload and len(data) > MAX_DATA_BYTES:
            raise DomainError("VKS_BUDGET_EXCEEDED", "成品库正文与向量超出插件读取限制，请缩小导出范围", 422)
        encoded = base64.b64encode(gzip.compress(data, compresslevel=9, mtime=0)).decode()
    # Replacement markers occur only in platform-owned template, never in data.
    return (
        template.replace(
            "<title>VKS 成品知识库</title>", "<title>" + escape(payload["project"]["name"]) + " · 成品知识库</title>"
        )
        .replace(
            "/* VKS_READER_CSS */",
            (assets / "reader.css").read_text() + (assets / "assistant-composer.css").read_text()
            + (assistant_assets / "source-preview.css").read_text(),
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


def recent_local_exports(s, job):
    return local_export_objects(s, job.project_id, job.created_by, job.input_json["release_id"])


def local_export_objects(s, project_id, actor_id, release_id, *, exact=False):
    release = Job.input_json["release_id"].as_string() == release_id
    # Use this actor's server-generated products as inputs to the newly authorized projection.
    return s.execute(
        select(Job.input_json, Job.result_json["object_key"].as_string())
        .where(
            Job.project_id == project_id,
            Job.created_by == actor_id,
            Job.kind == "export",
            Job.state == "succeeded",
            Job.input_json["format"].as_string() == "readonly_html",
            Job.input_json["local_vectors"].as_boolean().is_(True),
            *([release] if exact else []),
        )
        .order_by(release.desc(), Job.created_at.desc(), Job.id.desc())
        .limit(8)
    ).all()


def section_inputs(payload):
    return {
        section["id"]: (doc["title"], section["title"], section["content"])
        for doc in payload["documents"]
        for section in doc["sections"]
    }


def reuse_local_export(service, candidates, payload, deps, caps):
    if not candidates:
        return None, {}
    inputs = section_inputs(payload)
    reusable, seen = {}, set()
    for arguments, key in candidates:
        if len(reusable) == len(inputs) and arguments.get("release_id") != payload["snapshot"]["id"]:
            break
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) or key in seen:
            continue
        seen.add(key)
        path = service.data_dir / "objects" / key
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or digest(raw) != key:
                continue
            (start, end), saved = decode_envelope(raw)
            index = saved.pop("local_rag")
            if (index.get("profile"), index.get("model"), index.get("dimensions"), index.get("encoding")) != (
                PROFILE, MODEL, DIMENSIONS, "float32-le-base64"
            ):
                continue
            old_inputs = section_inputs(saved)
            if old_inputs or index["chunks"] != []:
                rows = LibraryVectors.read(index, old_inputs)
                if (
                    not rows or len(rows.chunks) != len(index["chunks"])
                    or {sid for sid, _, _ in rows.chunks} != set(old_inputs)
                ):
                    continue
        except (OSError, ValueError, TypeError, AttributeError, KeyError, EOFError, RecursionError, zlib.error):
            continue
        if (
            saved == payload
            and all(arguments.get(name) == value for name, value in {**caps, **deps}.items())
            and raw[:start] + raw[end:] == html_library(payload, encoded="")
        ):
            return key, {}
        matching = defaultdict(list)
        for row in index["chunks"]:
            matching[row["section_id"]].append(row["vector"])
        by_text = {old_inputs[sid]: vectors for sid, vectors in matching.items()}
        for sid, text in inputs.items():
            if sid not in reusable and text in by_text:
                reusable[sid] = matching[sid] if text == old_inputs.get(sid) else by_text[text]
        if arguments.get("release_id") != payload["snapshot"]["id"]:
            break
    return None, reusable


def studio_local_index(payload, validate_scope, reusable, report):
    model_dir = Path(os.getenv("VKS_LOCAL_EMBEDDING_MODEL_DIR") or ROOT / "output/vks-connect/local-model")
    try:
        documents = payload["documents"]
        model = encoder(model_dir.resolve()) if any(document["sections"] for document in documents) else None
        return build_local_index(
            documents,
            model,
            checkpoint=validate_scope,
            reusable=reusable,
            progress=lambda done, total: report("EXPORT_VECTORS", 15 + (70 * done // max(total, 1))),
        )
    except LocalModelError as error:
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", str(error), 503) from error


def prepare_job(service, jid, validate_scope=None, report=lambda *_: None):
    """Network and compression run outside the knowledge transaction/worker lock."""
    from .interactive_ai import InteractiveAssistant
    from .jobs import current_actor
    from .reading import ReadingService

    report("EXPORT_PREPARE", 5)
    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        actor = current_actor(service, job.created_by, job.project_id, session=s, job=job)
        caps = capabilities(service, s, actor, job)
        pid, release_id = job.project_id, job.input_json["release_id"]
        mode = job.input_json.get("document_mode", "project")
        document_ids = job.input_json.get("document_ids")
        local_vectors = job.input_json.get("local_vectors", False)
        payload, deps, section_ids = projection(
            service, s, actor, pid, release_id, caps, mode, document_ids, explanation_cutoff=job.created_at
        )
        before = digest(canonical([payload, deps]))
        candidates = recent_local_exports(s, job) if local_vectors else []
    prepared = {"caps": caps, "projection_hash": before}
    if validate_scope:
        validate_scope()
    if local_vectors:
        report("EXPORT_CACHE", 12)
        object_key, reusable = reuse_local_export(service, candidates, payload, deps, caps)
        if object_key:
            report("EXPORT_VERIFY", 95)
            return {**prepared, "object_key": object_key}
        report("EXPORT_VECTORS", 15)
        index = studio_local_index(payload, validate_scope, reusable, report)
    else:
        index = InteractiveAssistant(service).rag.export_index(
            actor, pid, release_id=release_id, section_revision_ids=section_ids, ensure=True, validate_scope=validate_scope
        )
    report("EXPORT_PACK", 88)
    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        actor = current_actor(service, job.created_by, pid, session=s, job=job)
        caps_now = capabilities(service, s, actor, job)
        latest, current_deps, _ = projection(
            service, s, actor, pid, release_id, caps_now, mode, document_ids, explanation_cutoff=job.created_at
        )
        if caps != caps_now or before != digest(canonical([latest, current_deps])):
            raise DomainError("VKS_VERSION_CONFLICT", "导出期间资料或权限发生变化，请重新导出", 409)
    if validate_scope:
        validate_scope()
    payload["local_rag" if local_vectors else "rag"] = index
    html = html_library(payload)
    if local_vectors and len(html) > MAX_FILE_BYTES:
        raise DomainError("VKS_BUDGET_EXCEEDED", "成品库文件超出插件读取限制，请缩小导出范围", 422)
    if validate_scope:
        validate_scope()
    report("EXPORT_VERIFY", 95)
    return {
        **prepared,
        "object_key": service._put_object(html),
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
        explanation_cutoff=job.created_at,
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
