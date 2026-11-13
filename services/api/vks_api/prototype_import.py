"""Preview and commit a selected batch of independently uploaded materials."""

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import Field
from sqlalchemy import select

from packages.analyzers.cancellation import checkpoint
from packages.analyzers.vks_ingest import MAX_BUNDLE_BYTES, MAX_TRANSPORT_BYTES

from .knowledge import ImportService, _unchanged_handbook_input, persist_bundle, same_prototype_action
from .models import Artifact, Entity, Job, Revision, Selection, Upload
from .prototype_edit import Input, Ref, draft, version
from .reading import ReadingService
from .service import DomainError, canonical, sha


ArtifactIds = Annotated[list[Ref], Field(min_length=1, max_length=32)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MAPPING_LIMIT = 1024
DOCUMENT_EXTENSIONS = {".md", ".markdown", ".json", ".yaml", ".yml"}


class BatchPreview(Input):
    analysis_set_id: Ref
    artifact_ids: ArtifactIds


class BatchConfirmation(Input):
    analysis_set_id: Ref
    preview_id: Ref
    preview_digest: Digest
    selected_artifact_ids: ArtifactIds


def _uploads(s, actor, pid, ids):
    if len(ids) != len(set(ids)):
        raise DomainError("VKS_INVALID_INPUT", "同一上传对象只能选择一次", 422)
    rows = {
        row.artifact_id: row
        for row in s.scalars(
            select(Upload).where(
                Upload.project_id == pid,
                Upload.created_by == actor.id,
                Upload.complete.is_(True),
                Upload.artifact_id.in_(ids),
            )
        )
    }
    if len(rows) != len(ids):
        raise DomainError("VKS_NOT_FOUND", "未找到已完成的上传", 404)
    if sum(row.size_bytes for row in rows.values()) > MAX_TRANSPORT_BYTES:
        raise DomainError("VKS_ARTIFACT_TOO_LARGE", "每批上传资料总量最多 128 MiB", 413)
    return [(key, PurePosixPath(rows[key].filename.replace("\\", "/")).name) for key in ids]


def queue_preview(service, s, actor, pid, request, key):
    analysis, _ = draft(s, service, actor, pid, request.analysis_set_id, "source:import")
    _uploads(s, actor, pid, request.artifact_ids)
    payload = request.model_dump()

    def create():
        return service.job_dto(
            service._new_job(
                s, actor, pid, "import_preview", dict(payload, selection_version=analysis.selection_version)
            )
        )

    return service.idem(s, actor, pid, "prototype.import_preview", key, payload, create) if key else create()


def _receipt(service, s, actor, pid, payload):
    preview = s.get(Job, payload["preview_id"])
    if (
        not preview
        or preview.kind != "import_preview"
        or preview.project_id != pid
        or preview.created_by != actor.id
        or preview.input_json["analysis_set_id"] != payload["analysis_set_id"]
    ):
        raise DomainError("VKS_NOT_FOUND", "未找到当前草稿的导入预检", 404)
    submitted = set(preview.input_json.get("_authorization", {}).get("scopes", []))
    if any((scope in submitted) != service._can(s, actor, pid, scope) for scope in ("source:read", "trace:read")):
        raise DomainError("VKS_VERSION_CONFLICT", "资料读取范围已变化，请重新预检", 409)
    result = preview.result_json
    if (
        preview.state != "succeeded"
        or result.get("preview_digest") != payload["preview_digest"]
        or result.get("selection_version") != payload["selection_version"]
    ):
        raise DomainError("VKS_VERSION_CONFLICT", "导入范围与预检摘要或草稿不一致，请重新预检", 409)
    ids = payload["artifact_ids"]
    materials = {row["artifact_id"]: row for row in result["materials"] if row["status"] == "ready"}
    if not set(ids) <= materials.keys():
        raise DomainError("VKS_INVALID_INPUT", "请选择已通过预检的资料", 422)
    if any(set(materials[key]["conflicts_with"]) & set(ids) for key in ids):
        raise DomainError("VKS_STATE_CONFLICT", "所选资料含有重叠的逻辑身份，请分开导入", 409)
    return materials, len(result["materials"]) - len(ids)


def queue_import(service, s, actor, pid, request, expected, key):
    analysis, _ = draft(s, service, actor, pid, request.analysis_set_id, "source:import", allow_frozen=True)
    ids = sorted(request.selected_artifact_ids)
    _uploads(s, actor, pid, ids)
    payload = {
        "analysis_set_id": analysis.id,
        "artifact_ids": ids,
        "preview_id": request.preview_id,
        "preview_digest": request.preview_digest,
        "selection_version": expected,
    }
    _receipt(service, s, actor, pid, payload)

    def create():
        if analysis.state == "frozen":
            raise DomainError("VKS_STATE_CONFLICT", "快照已冻结，请先创建草稿", 409)
        version(analysis.selection_version, expected)
        body = dict(payload, kind="prototype", _prototype_batch=True)
        return service.job_dto(service._new_job(s, actor, pid, "import", body))

    return service.idem(s, actor, pid, "prototype.import.batch", key, payload, create)


def _format(filename, parsed):
    if parsed and (report := parsed.analysis_metrics.get("prototype_import")):
        return report["format"]
    suffix = PurePosixPath(filename).suffix.lower()
    return {
        ".zip": "vksbundle",
        ".png": "png",
        ".jpg": "screenshot",
        ".jpeg": "screenshot",
        ".webp": "screenshot",
        ".html": "html",
        ".htm": "html",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "openapi",
        ".yaml": "openapi",
        ".yml": "openapi",
    }.get(suffix, "unknown")


def prepare_batch(service, actor, pid, payload, *, preview):
    """Parse outside the write transaction. Each upload supplies its own dependency set."""
    with service.db() as s:
        service.require(s, actor, pid, "source:import")
        uploads = _uploads(s, actor, pid, payload["artifact_ids"])
    importer = ImportService(service)
    results, total = [], 0
    for artifact_id, filename in uploads:
        checkpoint()
        material = {"artifact_id": artifact_id, "filename": filename}
        kind = "documents" if PurePosixPath(filename).suffix.lower() in DOCUMENT_EXTENSIONS else "prototype"
        try:
            parsed = importer.prepare(
                actor=actor,
                pid=pid,
                payload={
                    "kind": kind,
                    "artifact_ids": [artifact_id],
                    "analysis_set_id": payload["analysis_set_id"],
                    "_native_prototype": True,
                },
            )
            size = sum(row["size_bytes"] for row in parsed.artifacts)
            if size + total > MAX_BUNDLE_BYTES:
                raise DomainError("VKS_ARTIFACT_TOO_LARGE", "每批解包资料总量最多 100 MiB", 413)
            total += size
        except DomainError as exc:
            if not preview or exc.retryable or exc.status not in (413, 422):
                raise
            material.update(
                format=_format(filename, None),
                status="blocked",
                issues=[
                    {"code": exc.details.get("reason_code", exc.code), "message": exc.message, "severity": "error"}
                ],
                conflicts_with=[],
            )
            results.append((material, None))
        else:
            material.update(
                status="ready",
                format=_format(filename, parsed),
                content_digest=sha(
                    canonical(
                        {name: getattr(parsed, name) for name in ("manifest", "entities", "evidence", "relations")}
                    )
                ),
                size_bytes=size,
                file_count=len(parsed.artifacts),
                relation_count=len(parsed.relations),
                issues=parsed.issues,
                conflicts_with=[],
            )
            if report := parsed.analysis_metrics.get("prototype_import"):
                material["prototype_import"] = report
            results.append((material, parsed))
    return results


def preview_rows(service, s, actor, pid, choices):
    reading = ReadingService(service)
    return {
        (entity.type, entity.stable_key): row
        for row in reading.revision_rows(s, reading.visible_ids(s, actor, pid, choices).values())
        if (entity := s.get(Entity, row.entity_id))
    }


def operations(s, analysis, versions, parsed):
    incoming = {row["path"]: row for row in parsed.artifacts}
    same = any(row.get("import_manifest_sha256") == sha(canonical(parsed.manifest)) for row in analysis.sources)
    retained_documents = set()
    if parsed.analysis_metrics.get("prototype_import", {}).get("format") == "charge-handbook":
        for row in parsed.entities:
            if row["type"] != "document":
                continue
            prior = versions.get(("document", row["stable_key"]))
            path = row["artifact_path"]
            if prior and _unchanged_handbook_input(
                s, analysis.project_id, analysis.sources, prior, path, incoming[path]
            ):
                retained_documents.add(row["stable_key"])
    counts = {"added": 0, "updated": 0, "reused": 0}
    mappings = []
    for row in parsed.entities:
        prior = versions.get((row["type"], row["stable_key"]))
        origin = s.get(Artifact, prior.artifact_id) if prior and prior.artifact_id else None
        document_key = row["stable_key"] if row["type"] == "document" else row["metadata"].get("document_key")
        if document_key in retained_documents:
            op = "reused"
        elif not prior:
            op = "added"
        elif (
            same
            or same_prototype_action(row, prior)
            or (origin and origin.sha256 == incoming.get(row.get("artifact_path"), {}).get("sha256"))
        ):
            op = "reused"
        else:
            op = "updated"
        counts[op] += 1
        if len(mappings) < MAPPING_LIMIT:
            states = row["metadata"].get("prototype_manifest", {}).get("states", [])
            mappings.append(
                {
                    "kind": row["type"],
                    "key": row["stable_key"],
                    "name": row["display_name"],
                    "operation": op,
                    "artifact_path": row.get("artifact_path"),
                    "state_count": len(states),
                    "missing_surface_count": sum("surface" not in state for state in states),
                }
            )
    return counts, mappings


def finish_preview(service, s, actor, pid, payload, prepared):
    analysis, choices = draft(s, service, actor, pid, payload["analysis_set_id"], "source:import")
    version(analysis.selection_version, payload["selection_version"])
    prior = preview_rows(service, s, actor, pid, choices)
    owners = defaultdict(set)
    for material, parsed in prepared:
        if parsed is None:
            continue
        counts, mappings = operations(s, analysis, prior, parsed)
        material.update(operations=counts, mappings=mappings, entity_count=len(parsed.entities))
        for row in parsed.entities:
            owners[(row["type"], row["stable_key"])].add(material["artifact_id"])
    conflicts = defaultdict(set)
    for ids in owners.values():
        for item in ids:
            conflicts[item].update(ids - {item})
    materials = [dict(row, conflicts_with=sorted(conflicts[row["artifact_id"]])) for row, _ in prepared]
    return {
        "analysis_set_id": analysis.id,
        "selection_version": analysis.selection_version,
        "preview_digest": sha(
            canonical({"analysis_set_id": analysis.id, "version": analysis.selection_version, "materials": materials})
        ),
        "valid": any(row["status"] == "ready" for row in materials),
        "materials": materials,
        "issues": [issue for material in materials for issue in material["issues"]],
    }


def finish_batch(service, s, actor, pid, payload, prepared):
    analysis, _ = draft(s, service, actor, pid, payload["analysis_set_id"], "source:import")
    version(analysis.selection_version, payload["selection_version"])
    receipt, skipped_count = _receipt(service, s, actor, pid, payload)
    seen = set()
    for material, parsed in prepared:
        if receipt[material["artifact_id"]]["content_digest"] != material["content_digest"]:
            raise DomainError("VKS_VERSION_CONFLICT", "导入资料与预检摘要不一致，请重新预检", 409)
        keys = {(row["type"], row["stable_key"]) for row in parsed.entities}
        if seen & keys:
            raise DomainError("VKS_STATE_CONFLICT", "所选资料含有重叠的逻辑身份，请分开导入", 409)
        seen.update(keys)
    results = []
    sources = list(analysis.sources)
    for material, parsed in prepared:
        result = persist_bundle(
            service,
            s,
            pid,
            actor.id,
            parsed,
            analysis.id,
            actor=actor,
            staged_sources=sources,
            reuse_imported_manifest=True,
        )
        first_page = None
        for row in parsed.entities:
            if row["type"] != "page":
                continue
            selected = s.scalar(
                select(Revision)
                .join(Selection, Selection.entity_revision_id == Revision.id)
                .join(Entity, Entity.id == Selection.entity_id)
                .where(
                    Selection.analysis_set_id == analysis.id,
                    Entity.type == "page",
                    Entity.stable_key == row["stable_key"],
                )
            )
            if selected and service._visible_revision(s, actor, pid, selected):
                first_page = row["stable_key"]
                break
        results.append(
            {
                "artifact_id": material["artifact_id"],
                "filename": material["filename"],
                "status": "reused" if result["reused"] else "imported",
                "operations": result.get("operations", {"added": 0, "updated": 0, "reused": len(parsed.entities)}),
                "first_page_key": first_page,
                "issues": result["issues"],
            }
        )
    if any(row["status"] == "imported" for row in results):
        analysis.sources = sources
        analysis.selection_version += 1
        analysis.state = "draft"
    return {
        "result_refs": [{"kind": "analysis_set", "id": analysis.id}],
        "analysis_set_id": analysis.id,
        "selection_version": analysis.selection_version,
        "materials": results,
        "skipped_count": skipped_count,
        "first_page_key": next((row["first_page_key"] for row in results if row["first_page_key"]), None),
        "issues": [issue for result in results for issue in result["issues"]],
    }
