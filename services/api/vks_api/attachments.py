"""Resolve imported document attachments only inside its authorized fixed snapshot."""

import mimetypes
import posixpath
from urllib.parse import urlsplit, unquote, quote
from fastapi import Depends, Request, Query
from fastapi.responses import Response
from sqlalchemy import select
from .models import Artifact, Entity, Revision, Release


def resolve(service, actor, eid, rid, path):
    from .service import DomainError

    # DocumentService enforces both object visibility and published-only readers.
    service.get_document(actor, eid, rid)
    if len(path) > 2048:
        raise DomainError("VKS_INVALID_INPUT", "附件路径过长")
    target = unquote(path)
    url = urlsplit(target)
    if (
        url.scheme
        or url.netloc
        or url.query
        or url.fragment
        or target.startswith("/")
        or "\\" in target
        or "\x00" in target
    ):
        raise DomainError("VKS_INVALID_INPUT", "仅支持文档内的相对附件路径")
    with service.db() as s:
        revision = s.get(Revision, rid)
        entity = s.get(Entity, eid)
        artifact = s.get(Artifact, revision.artifact_id)
        if not artifact:
            raise DomainError("VKS_NOT_FOUND", "未找到原始文档", 404)
        original = artifact.metadata_json.get("original_artifact_id") or revision.metadata_json.get(
            "original_artifact_id"
        )
        if original:
            original = s.get(Artifact, original)
            if original and original.project_id == entity.project_id:
                artifact = original
        relative = posixpath.normpath(posixpath.join(posixpath.dirname(artifact.relative_path), url.path))
        if relative == ".." or relative.startswith("../"):
            raise DomainError("VKS_INVALID_INPUT", "附件路径越出来源快照")
        attachment = s.scalar(
            select(Artifact).where(
                Artifact.project_id == entity.project_id,
                Artifact.snapshot_id == artifact.snapshot_id,
                Artifact.relative_path == relative,
            )
        )
        if not attachment:
            raise DomainError("VKS_NOT_FOUND", "未找到当前文档的附件", 404)
        if (
            attachment.kind == "source"
            or attachment.sensitivity == "confidential"
            or attachment.metadata_json.get("requires_source_read")
        ):
            service.require(s, actor, entity.project_id, "source:read")
        if attachment.kind in ("trace", "capture") or attachment.metadata_json.get("requires_trace_read"):
            service.require(s, actor, entity.project_id, "trace:read")
        service.audit(s, actor, entity.project_id, "document.attachment.read", attachment.id)
        raw = (service.data_dir / "objects" / attachment.object_key).read_bytes()
        media = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        raster = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        if media in raster:
            # Validate actual raster bytes; a disguised HTML/SVG must not run inline.
            from PIL import Image
            from io import BytesIO

            try:
                with Image.open(BytesIO(raw)) as image:
                    if image.width * image.height > 16777216:
                        raise ValueError("image dimensions exceed display policy")
                    expected = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(
                        image.format
                    )
                    image.verify()
                    if media != expected:
                        raise ValueError("format mismatch")
            except Exception:
                raise DomainError("VKS_INVALID_INPUT", "图片内容与支持的栅格格式不一致") from None
            disposition = "inline"
        else:
            media = "application/octet-stream"
            disposition = "attachment"
        return raw, media, disposition, posixpath.basename(relative)


def install_attachments(app, service, actor, API="/api/v1"):
    @app.get(API + "/documents/{eid}/revisions/{rid}/attachments")
    def attachment(eid: str, rid: str, path: str, request: Request, a=Depends(actor)):
        raw, media, disposition, name = resolve(service, a, eid, rid, path)
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
