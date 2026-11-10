"""Serve a raster surface from the exact authorized prototype page revision."""

from hashlib import sha256
from typing import Annotated

from fastapi import Depends
from fastapi.responses import Response
from sqlalchemy import select

from .models import Artifact, Entity, Revision
from .reading import ReadingService, artifact_policy
from .service import Actor, DomainError


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


def install_prototype(app, service, actor):
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
