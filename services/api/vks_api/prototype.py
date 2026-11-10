"""Serve a raster surface from the exact authorized prototype page revision."""

from hashlib import sha256
from typing import Annotated, Literal

from fastapi import Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select

from .models import Artifact, Entity, Revision
from .reading import ReadingService, artifact_policy
from .service import Actor, DomainError, canonical, sha
from .prototype_edit import AnchorEdit, BindingEdit, Input, PrototypeEditor, Ref, SettingsEdit, draft, version
from .prototype_read import PrototypeReader
from .prototype_contracts import (
    PrototypeAnchorsResult,
    PrototypeBindingResult,
    PrototypeContext,
    PrototypeEnvelope,
    PrototypeMenu,
    PrototypePage,
    PrototypeReferences,
)
from .knowledge import ImportService


class PreviewInput(Input):
    analysis_set_id: Ref
    artifact_id: Ref


class ImportConfirmation(PreviewInput):
    preview_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def preview_fingerprint(parsed, payload, selection_version):
    return sha(
        canonical(
            {
                "manifest": parsed.manifest,
                "analysis_set_id": payload["analysis_set_id"],
                "artifact_ids": payload["artifact_ids"],
                "selection_version": selection_version,
            }
        )
    )


def prototype_image(service, actor, pid, page_key, state_key, page_revision_id, analysis_set_id, release_id):
    if bool(analysis_set_id) == bool(release_id):
        raise DomainError("VKS_INVALID_INPUT", "请选择准确的草稿或快照", 422)
    unavailable = DomainError("VKS_NOT_FOUND", "当前版本未找到可读取的原型画面", 404)
    reading = ReadingService(service)
    with reading.db() as s:
        analysis, release = reading.baseline(s, actor, pid, analysis_set_id, release_id)
        choices, _ = reading.selections(s, analysis, release)
        entity_id = s.scalar(
            select(Entity.id).where(Entity.project_id == pid, Entity.type == "page", Entity.stable_key == page_key)
        )
        revision = s.get(Revision, choices[entity_id]) if entity_id in choices else None
        if not revision or not service._visible_revision(s, actor, pid, revision):
            raise unavailable
        if revision.id != page_revision_id:
            raise DomainError("VKS_VERSION_CONFLICT", "页面已变化，请重新加载原型", 409)
        manifest = revision.metadata_json.get("prototype_manifest", {})
        state = next((row for row in manifest.get("states", []) if row["stateKey"] == state_key), None)
        surface = state.get("surface") if state else None
        origin = s.get(Artifact, revision.artifact_id) if surface else None
        if not origin or origin.project_id != pid:
            raise unavailable
        art = s.scalar(
            select(Artifact).where(
                Artifact.project_id == pid,
                Artifact.snapshot_id == origin.snapshot_id,
                Artifact.relative_path == surface["artifactPath"],
                Artifact.sha256 == surface["sha256"],
                Artifact.kind == "screenshot",
                Artifact.media_type == "image/png",
            )
        )
        if (
            not art
            or art.id not in origin.metadata_json.get("prototype_surface_artifact_ids", [])
            or not reading.policy_visible(
                art.kind, art.sensitivity, artifact_policy(s, art), *reading.permissions(s, actor, pid)
            )
        ):
            raise unavailable
        try:
            raw = (service.data_dir / "objects" / art.object_key).read_bytes()
        except OSError:
            raise unavailable from None
        if len(raw) != art.size_bytes or sha256(raw).hexdigest() != surface["sha256"]:
            raise DomainError("VKS_INTEGRITY_ERROR", "原型画面摘要不匹配", 409)
        return raw


def install_prototype(app, service, actor, envelope):
    reader = PrototypeReader(service)
    editor = PrototypeEditor(service)
    importer = ImportService(service)
    prefix = "/api/v3/projects/{pid}/prototype"
    ActorDep = Annotated[Actor, Depends(actor)]
    IfMatch = Annotated[str, Header(alias="If-Match")]
    IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
    Policy = Literal["valid", "outdated", "all"]

    @app.put(prefix + "/settings")
    def settings(pid: str, payload: SettingsEdit, request: Request, a: ActorDep, if_match: IfMatch):
        return envelope(request, editor.save_settings(a, pid, payload, if_match))

    @app.get(prefix + "/menu", response_model=PrototypeEnvelope[PrototypeMenu])
    def menu(
        pid: str,
        request: Request,
        a: ActorDep,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return envelope(request, reader.menu(a, pid, analysis_set_id, release_id, after, limit))

    @app.get(prefix + "/pages/{page_key}", response_model=PrototypeEnvelope[PrototypePage])
    def page(
        pid: str,
        page_key: str,
        request: Request,
        a: ActorDep,
        state_key: str | None = None,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
    ):
        return envelope(request, reader.page(a, pid, page_key, state_key, analysis_set_id, release_id))

    @app.get(prefix + "/actions/{action_key}/context", response_model=PrototypeEnvelope[PrototypeContext])
    def context(
        pid: str,
        action_key: str,
        request: Request,
        a: ActorDep,
        page_key: str,
        trigger_state_key: str,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        policy: Policy = "valid",
    ):
        return envelope(
            request,
            reader.context(a, pid, page_key, action_key, trigger_state_key, analysis_set_id, release_id, policy),
        )

    @app.get(
        "/api/v3/projects/{pid}/sections/{section_key}/prototype-references",
        response_model=PrototypeEnvelope[PrototypeReferences],
    )
    def references(
        pid: str,
        section_key: str,
        request: Request,
        a: ActorDep,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return envelope(request, reader.references(a, pid, section_key, analysis_set_id, release_id, after, limit))

    @app.put(prefix + "/pages/{page_key}/anchors", response_model=PrototypeEnvelope[PrototypeAnchorsResult])
    def anchors(
        pid: str,
        page_key: str,
        payload: AnchorEdit,
        request: Request,
        a: ActorDep,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ):
        return envelope(request, editor.save_anchors(a, pid, page_key, payload, if_match, idempotency_key))

    @app.post(
        prefix + "/actions/{action_key}/business-points", response_model=PrototypeEnvelope[PrototypeBindingResult]
    )
    def binding(
        pid: str,
        action_key: str,
        payload: BindingEdit,
        request: Request,
        a: ActorDep,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ):
        return envelope(request, editor.save_binding(a, pid, action_key, payload, if_match, idempotency_key))

    @app.post(prefix + "/import-previews")
    def import_preview(pid: str, payload: PreviewInput, request: Request, a: ActorDep):
        with service.db() as s:
            analysis, _ = draft(s, service, a, pid, payload.analysis_set_id, "source:import")
            selection = analysis.selection_version
        body = {"kind": "prototype", "artifact_ids": [payload.artifact_id], "analysis_set_id": payload.analysis_set_id}
        parsed = importer.prepare(actor=a, pid=pid, payload=body)
        with service.db() as s:
            analysis, choices = draft(s, service, a, pid, payload.analysis_set_id, "source:import")
            version(analysis.selection_version, selection)
            reading = ReadingService(service)
            visible = reading.visible_ids(s, a, pid, choices)
            same = any(row.get("import_manifest_sha256") == sha(canonical(parsed.manifest)) for row in analysis.sources)
            versions = {
                (entity.type, entity.stable_key): (s.get(Artifact, row.artifact_id).sha256 if row.artifact_id else None)
                for row in reading.revision_rows(s, visible.values())
                if (entity := s.get(Entity, row.entity_id))
            }
            incoming = {row["path"]: row["sha256"] for row in parsed.artifacts}
            operations = {"added": 0, "updated": 0, "reused": 0}
            for row in parsed.entities:
                key = (row["type"], row["stable_key"])
                if key not in versions:
                    operations["added"] += 1
                elif same or versions[key] == incoming.get(row.get("artifact_path")):
                    operations["reused"] += 1
                else:
                    operations["updated"] += 1
        return envelope(
            request,
            {
                "valid": True,
                "preview_digest": preview_fingerprint(parsed, body, selection),
                "selection_version": selection,
                "file_count": len(parsed.artifacts),
                "entity_count": len(parsed.entities),
                "operations": operations,
                "relation_count": len(parsed.relations),
                "size_bytes": sum(row["size_bytes"] for row in parsed.artifacts),
                "issues": parsed.issues,
                "prototype_import": parsed.analysis_metrics.get("prototype_import"),
            },
        )

    @app.post(prefix + "/imports", status_code=202)
    def import_material(
        pid: str,
        payload: ImportConfirmation,
        request: Request,
        a: ActorDep,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ):
        with service.db() as s:
            analysis, _ = draft(s, service, a, pid, payload.analysis_set_id, "source:import")
            service._upload_data(s, a, pid, payload.artifact_id)
            try:
                selection = int(if_match.strip('"'))
            except ValueError:
                raise DomainError("VKS_INVALID_INPUT", "If-Match 必须是草稿版本", 422) from None
            body = {
                "kind": "prototype",
                "artifact_ids": [payload.artifact_id],
                "analysis_set_id": payload.analysis_set_id,
                "_prototype_preview": {"digest": payload.preview_digest, "selection_version": selection},
            }

            def queue():
                version(analysis.selection_version, selection)
                return service.job_dto(service._new_job(s, a, pid, "import", body))

            return envelope(request, service.idem(s, a, pid, "prototype.import", idempotency_key, body, queue))

    @app.get(
        "/api/v3/projects/{pid}/prototype/pages/{page_key}/states/{state_key}/image",
        responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
        response_class=Response,
    )
    def image(
        pid: str,
        page_key: str,
        state_key: str,
        page_revision_id: str,
        a: Annotated[Actor, Depends(actor)],
        analysis_set_id: str | None = None,
        release_id: str | None = None,
    ):
        return Response(
            prototype_image(service, a, pid, page_key, state_key, page_revision_id, analysis_set_id, release_id),
            media_type="image/png",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )
