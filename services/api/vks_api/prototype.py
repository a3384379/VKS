"""Serve a raster surface from the exact authorized prototype page revision."""

from hashlib import sha256
from typing import Annotated, Literal

from fastapi import Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .models import Artifact, Entity
from .reading import ReadingService, artifact_policy, prototype_surface
from .service import Actor, DomainError, canonical, sha
from .prototype_edit import AnchorEdit, BindingEdit, Input, PrototypeEditor, Ref, SettingsEdit, draft, version
from .prototype_read import PrototypeReader, find_selected, page_state
from .prototype_contracts import (
    PrototypeAnchorsResult,
    PrototypeBindingResult,
    PrototypeContext,
    PrototypeEnvelope,
    PrototypeMenu,
    PrototypePage,
    PrototypeReferences,
    PrototypeSettingsResult,
)
from .knowledge import ImportService
from .prototype_import import BatchConfirmation, BatchPreview, operations, preview_rows, queue_import, queue_preview
from .v2_contracts import JobOutput


class PreviewInput(Input):
    analysis_set_id: Ref
    artifact_id: Ref


class ImportConfirmation(PreviewInput):
    preview_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ImportPreviewResult(BaseModel):
    valid: bool
    preview_digest: str
    selection_version: int
    file_count: int
    entity_count: int
    operations: dict[str, int]
    mappings: list[dict]
    relation_count: int
    size_bytes: int
    issues: list[dict]
    prototype_import: dict | None


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
    unavailable = DomainError("VKS_NOT_FOUND", "当前版本未找到可读取的原型画面", 404)
    reader = PrototypeReader(service)
    reading = reader.reading
    with reading.db() as s:
        _, _, choices, _ = reader.scope(s, actor, pid, analysis_set_id, release_id, keys=[("page", page_key)])
        _, revision = find_selected(s, service, actor, pid, choices, "page", page_key)
        if revision.id != page_revision_id:
            raise DomainError("VKS_VERSION_CONFLICT", "页面已变化，请重新加载原型", 409)
        art = prototype_surface(s, pid, revision.artifact_id, page_state(revision, state_key))
        if not art or not reading.policy_visible(
            art.kind, art.sensitivity, artifact_policy(s, art), *reading.permissions(s, actor, pid)
        ):
            raise unavailable
        try:
            raw = (service.data_dir / "objects" / art.object_key).read_bytes()
        except OSError:
            raise unavailable from None
        if len(raw) != art.size_bytes or sha256(raw).hexdigest() != art.sha256:
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

    @app.put(prefix + "/settings", response_model=PrototypeEnvelope[PrototypeSettingsResult])
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
        trigger_state_key: Annotated[str, Query(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9._-]*$")],
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

    @app.post(
        prefix + "/import-previews",
        responses={
            200: {"model": PrototypeEnvelope[ImportPreviewResult], "description": "Single material preview report"},
            202: {"model": PrototypeEnvelope[JobOutput], "description": "Batch preview job accepted"},
        },
    )
    def import_preview(
        pid: str,
        payload: PreviewInput | BatchPreview,
        request: Request,
        response: Response,
        a: ActorDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if isinstance(payload, BatchPreview):
            with service.db() as s:
                accepted = queue_preview(service, s, a, pid, payload, idempotency_key)
            response.status_code = 202
            return envelope(request, accepted)
        with service.db() as s:
            analysis, _ = draft(s, service, a, pid, payload.analysis_set_id, "source:import")
            selection = analysis.selection_version
        body = {
            "kind": "prototype",
            "artifact_ids": [payload.artifact_id],
            "analysis_set_id": payload.analysis_set_id,
            "_native_prototype": True,
        }
        parsed = importer.prepare(actor=a, pid=pid, payload=body)
        with service.db() as s:
            analysis, choices = draft(s, service, a, pid, payload.analysis_set_id, "source:import")
            version(analysis.selection_version, selection)
            counts, mappings = operations(s, analysis, preview_rows(service, s, a, pid, choices), parsed)
        return envelope(
            request,
            {
                "valid": True,
                "preview_digest": preview_fingerprint(parsed, body, selection),
                "selection_version": selection,
                "file_count": len(parsed.artifacts),
                "entity_count": len(parsed.entities),
                "operations": counts,
                "mappings": mappings,
                "relation_count": len(parsed.relations),
                "size_bytes": sum(row["size_bytes"] for row in parsed.artifacts),
                "issues": parsed.issues,
                "prototype_import": parsed.analysis_metrics.get("prototype_import"),
            },
        )

    @app.post(prefix + "/imports", status_code=202, responses={202: {"model": PrototypeEnvelope[JobOutput]}})
    def import_material(
        pid: str,
        payload: ImportConfirmation | BatchConfirmation,
        request: Request,
        a: ActorDep,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ):
        try:
            selection = int(if_match.strip('"'))
        except ValueError:
            raise DomainError("VKS_INVALID_INPUT", "If-Match 必须是草稿版本", 422) from None
        with service.db() as s:
            if isinstance(payload, BatchConfirmation):
                return envelope(request, queue_import(service, s, a, pid, payload, selection, idempotency_key))
            analysis, _ = draft(s, service, a, pid, payload.analysis_set_id, "source:import", allow_frozen=True)
            service._upload_data(s, a, pid, payload.artifact_id)
            body = {
                "kind": "prototype",
                "artifact_ids": [payload.artifact_id],
                "analysis_set_id": payload.analysis_set_id,
                "_native_prototype": True,
                "_prototype_preview": {"digest": payload.preview_digest, "selection_version": selection},
            }

            def queue():
                if analysis.state == "frozen":
                    raise DomainError("VKS_STATE_CONFLICT", "快照已冻结，请先创建草稿", 409)
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
