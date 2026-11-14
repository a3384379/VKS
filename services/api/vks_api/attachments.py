"""Resolve document assets against their exact authorized source/package version."""

import mimetypes
import posixpath
import unicodedata
from hashlib import sha256
from io import BytesIO
from typing import Annotated
from urllib.parse import quote, unquote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import select

from .models import Artifact, Entity, Revision
from .reading import artifact_policy
from .service import Actor, DomainError

RASTER_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def document_origin(s, revision):
    artifact = s.get(Artifact, revision.artifact_id)
    if not artifact or artifact.project_id != revision.project_id:
        raise DomainError("VKS_NOT_FOUND", "未找到原始文档", 404)
    original = artifact.metadata_json.get("original_artifact_id") or revision.metadata_json.get("original_artifact_id")
    if original:
        origin = s.get(Artifact, original)
        if origin and origin.project_id == revision.project_id:
            artifact = origin
    return artifact


def attachment_path(s, revision, path):
    if not isinstance(path, str) or len(path) > 2048:
        raise DomainError("VKS_INVALID_INPUT", "附件路径过长")
    # File imports use canonical NFC paths. Preserve Markdown verbatim while
    # matching canonically equivalent macOS references to that exact asset.
    target = unicodedata.normalize("NFC", unquote(path))
    try:
        url = urlsplit(target)
    except ValueError:
        raise DomainError("VKS_INVALID_INPUT", "附件路径不合法") from None
    if (
        url.scheme
        or url.netloc
        or url.query
        or url.fragment
        or target.startswith("/")
        or "\\" in target
        or any(ord(char) < 32 or ord(char) == 127 for char in target)
    ):
        raise DomainError("VKS_INVALID_INPUT", "仅支持文档内的相对附件路径")
    origin = document_origin(s, revision)
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(origin.relative_path), url.path))
    if relative == ".." or relative.startswith("../"):
        raise DomainError("VKS_INVALID_INPUT", "附件路径越出来源快照")
    return origin, relative


def mounted_attachments(s, pid, revision, choices):
    """None means a legacy source; [] deliberately means no current assets."""
    from .materials import ROOT, roots

    package_id = revision.metadata_json.get("material_package_id")
    if not package_id:
        return None
    package = next((root for root in roots(s, pid, choices) if root.entity_id == package_id), None)
    if not package or package.metadata_json[ROOT]["kind"] != "documents":
        raise DomainError("VKS_NOT_FOUND", "当前版本未找到文档资料包", 404)
    return package.metadata_json[ROOT].get("attachment_artifact_ids", [])


def source_attachments(s, origin):
    """Bind unscoped material links to genuine assets of that immutable source.

    A public extracted chapter must never grant access to the retained upload
    ZIP or runtime HTML, which can contain independently restricted chapters.
    Non-material VKS sources retain their existing fixed-snapshot behavior.
    """
    from .knowledge import SourceSnapshot
    from .materials import ROOT

    snapshot = s.get(SourceSnapshot, origin.snapshot_id)
    if not snapshot or snapshot.project_id != origin.project_id or not snapshot.source_key.startswith("material."):
        return None
    cache = s.info.setdefault("vks_material_attachment_ids", {})
    key = (origin.project_id, origin.snapshot_id)
    if key not in cache:
        packages = s.scalars(
            select(Revision)
            .join(Entity, Revision.entity_id == Entity.id)
            .join(Artifact, Revision.artifact_id == Artifact.id)
            .where(
                Entity.project_id == origin.project_id,
                Entity.type == "page",
                Entity.stable_key.startswith("material."),
                Artifact.project_id == origin.project_id,
                Artifact.snapshot_id == origin.snapshot_id,
            )
        )
        declared, has_registry = set(), False
        for package in packages:
            metadata = package.metadata_json.get(ROOT, {})
            if metadata.get("kind") == "documents" and "attachment_artifact_ids" in metadata:
                has_registry = True
                declared.update(metadata["attachment_artifact_ids"])
        if not has_registry:
            # Older material versions had no asset registry. Preserve genuine
            # extracted attachments only; uploads, runtime and parsed document
            # bodies are not attachments and cannot be reached by path guessing.
            declared.update(
                s.scalars(
                    select(Artifact.id).where(
                        Artifact.project_id == origin.project_id,
                        Artifact.snapshot_id == origin.snapshot_id,
                        Artifact.relative_path.startswith("documents/"),
                        Artifact.kind != "markdown",
                    )
                )
            )
        cache[key] = list(
            s.scalars(
                select(Artifact.id).where(
                    Artifact.id.in_(declared),
                    Artifact.project_id == origin.project_id,
                    Artifact.snapshot_id == origin.snapshot_id,
                    Artifact.relative_path.startswith("documents/"),
                )
            )
        )
    return cache[key]


def find_attachment(s, revision, path, attachment_ids=None):
    origin, relative = attachment_path(s, revision, path)
    if attachment_ids is None:
        attachment_ids = source_attachments(s, origin)
    if attachment_ids is not None and not relative.startswith("documents/"):
        return None, relative
    if attachment_ids is not None:
        attachment = s.scalar(
            select(Artifact).where(
                Artifact.id.in_(attachment_ids),
                Artifact.project_id == revision.project_id,
                Artifact.relative_path == relative,
            )
        )
    else:
        attachment = s.scalar(
            select(Artifact).where(
                Artifact.project_id == revision.project_id,
                Artifact.snapshot_id == origin.snapshot_id,
                Artifact.relative_path == relative,
            )
        )
    return attachment, relative


def raster_media(raw, expected=None):
    """Raster decode/size validation shared by browser reading and HTML export."""
    try:
        with Image.open(BytesIO(raw)) as image:
            media = RASTER_TYPES.get(image.format)
            if not media or image.width * image.height > 16777216 or expected and media != expected:
                raise ValueError()
            image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise DomainError("VKS_INVALID_INPUT", "图片内容与支持的栅格格式不一致") from None
    return media


def resolve(service, actor, eid, rid, path, analysis_set_id=None, release_id=None, selection_version=None):
    if not analysis_set_id and not release_id:
        service.get_document(actor, eid, rid)
    with service.db() as s:
        revision, entity = s.get(Revision, rid), s.get(Entity, eid)
        if (
            not revision
            or not entity
            or entity.type != "document"
            or revision.entity_id != eid
            or revision.project_id != entity.project_id
        ):
            raise DomainError("VKS_NOT_FOUND", "未找到文档", 404)
        service.require(s, actor, entity.project_id)
        if not service._visible_revision(s, actor, entity.project_id, revision):
            raise DomainError("VKS_NOT_FOUND", "未找到文档", 404)
        ids = None
        if analysis_set_id or release_id:
            from .prototype_read import PrototypeReader, selected_id

            analysis, release, choices, _ = PrototypeReader(service).scope(
                s, actor, entity.project_id, analysis_set_id, release_id
            )
            if not release and selection_version is not None and selection_version != analysis.selection_version:
                raise DomainError("VKS_VERSION_CONFLICT", "文档资料版本已更新，请刷新后查看图片", 412)
            selected_entity, _ = selected_id(s, service, actor, entity.project_id, choices, rid, "document")
            if selected_entity.id != eid:
                raise DomainError("VKS_NOT_FOUND", "当前版本未找到文档", 404)
            ids = mounted_attachments(s, entity.project_id, revision, choices)
        attachment, relative = find_attachment(s, revision, path, ids)
        if not attachment:
            raise DomainError("VKS_NOT_FOUND", "未找到当前文档的附件", 404)
        policy = artifact_policy(s, attachment)
        if policy.get("requires_source_read"):
            service.require(s, actor, entity.project_id, "source:read")
        if policy.get("requires_trace_read"):
            service.require(s, actor, entity.project_id, "trace:read")
        raw = (service.data_dir / "objects" / attachment.object_key).read_bytes()
        if len(raw) != attachment.size_bytes or sha256(raw).hexdigest() != attachment.sha256:
            raise DomainError("VKS_INTEGRITY_ERROR", "附件完整性校验失败", 409)
        media = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        disposition = "attachment"
        if media in RASTER_TYPES.values():
            media = raster_media(raw, media)
            disposition = "inline"
        else:
            media = "application/octet-stream"
        service.audit(s, actor, entity.project_id, "document.attachment.read", attachment.id)
        return raw, media, disposition, posixpath.basename(relative)


def install_attachments(app, service, actor, API="/api/v1"):
    ActorDep = Annotated[Actor, Depends(actor)]

    @app.get(API + "/documents/{eid}/revisions/{rid}/attachments")
    def attachment(
        eid: str,
        rid: str,
        path: str,
        request: Request,
        a: ActorDep,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        selection_version: int | None = None,
    ):
        raw, media, disposition, name = resolve(
            service, a, eid, rid, path, analysis_set_id, release_id, selection_version
        )
        return Response(
            raw,
            media_type=media,
            headers={
                "Content-Disposition": disposition + "; filename*=UTF-8''" + quote(name),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )
