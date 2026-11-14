"""Read the standalone handbook author's inert document envelope, never scripts."""

import base64
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from packages.analyzers.offline_prototype import _scripts
from packages.analyzers.vks_ingest import BundleError, _json, safe_path

from .attachments import raster_media
from .prototype_edit import Input
from .service import DomainError

NODE = "vks-template-data"
SCHEMA = "vks-handbook-authoring-1"
MAX_DOCUMENTS = 2000
MAX_FILES = 5000
MAX_MARKDOWN = 2 * 1024 * 1024
MAX_IMAGE = 12 * 1024 * 1024
MAX_CONTENT = 64 * 1024 * 1024
MAX_HTML = 80 * 1024 * 1024
IMAGE_SUFFIXES = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
}


class TemplateDocument(Input):
    path: str = Field(min_length=1, max_length=1024, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=160, strict=True, pattern=r"^[^\x00-\x1f\x7f]+$")
    markdown: str = Field(max_length=MAX_MARKDOWN, strict=True)


class TemplateImage(Input):
    path: str = Field(min_length=1, max_length=1024, strict=True)
    mime: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    base64: str = Field(max_length=((MAX_IMAGE + 2) // 3) * 4, strict=True)


class TemplateEnvelope(Input):
    schema_version: Literal[SCHEMA] = Field(alias="schema")
    template_id: Literal["handbook"]
    template_version: Literal["1.0.0"]
    documents: list[TemplateDocument] = Field(max_length=MAX_DOCUMENTS)
    images: list[TemplateImage] = Field(default_factory=list, max_length=MAX_FILES)


def invalid(message="本地知识手册的数据格式或路径不合法"):
    raise DomainError("VKS_INVALID_ARCHIVE", message, 422)


def member_path(value):
    try:
        value.encode("utf-8")
        path = safe_path(value)
        uri = urlsplit(path)
        if uri.scheme or uri.netloc or uri.query or uri.fragment or any(c in path for c in "?#%"):
            invalid()
        if any(
            part.casefold() in {".git", "node_modules", ".env", ".env.local", "id_rsa"}
            for part in PurePosixPath(path).parts
        ):
            invalid()
        return path
    except (BundleError, ValueError, UnicodeError):
        invalid()


def read_authoring(raw):
    """Return normalized MD/assets, or None for an unrelated source HTML."""
    if NODE.encode() not in raw:
        return None
    if len(raw) > MAX_HTML:
        invalid("本地知识手册 HTML 超过 80 MiB")
    try:
        scripts = _scripts(raw, {NODE: "application/json"})
        if NODE not in scripts:
            return None
        envelope = TemplateEnvelope.model_validate(_json(scripts[NODE].encode()))
    except (BundleError, ValidationError, UnicodeError, RecursionError):
        invalid()
    if len(envelope.documents) + len(envelope.images) > MAX_FILES:
        invalid("本地知识手册文件数量超过 5000")
    documents, images, names, seen, total = [], [], {}, set(), 0
    for document in envelope.documents:
        path = member_path(document.path)
        if PurePosixPath(path).suffix.lower() not in {".md", ".markdown", ".txt"}:
            invalid("本地知识手册正文支持 MD、Markdown 和 TXT 文件")
        try:
            content = document.markdown.encode("utf-8")
            if document.name is not None:
                document.name.encode("utf-8")
                if not document.name.strip():
                    invalid()
        except UnicodeError:
            invalid("本地知识手册文本包含无效 Unicode")
        if len(content) > MAX_MARKDOWN:
            invalid("单篇 MD/TXT 正文超过 2 MiB")
        if path.casefold() in seen:
            invalid("本地知识手册含重复文档或附件路径")
        seen.add(path.casefold())
        total += len(content)
        if total > MAX_CONTENT:
            invalid("本地知识手册正文与图片合计超过 64 MiB")
        documents.append((path, document.markdown))
        if document.name is not None:
            names[path] = document.name
    for image in envelope.images:
        path = member_path(image.path)
        if PurePosixPath(path).suffix.lower() not in IMAGE_SUFFIXES[image.mime]:
            invalid("图片扩展名与声明格式不一致")
        if path.casefold() in seen:
            invalid("本地知识手册含重复文档或附件路径")
        seen.add(path.casefold())
        try:
            content = base64.b64decode(image.base64, validate=True)
            if len(content) > MAX_IMAGE:
                invalid("单张图片超过 12 MiB")
            raster_media(content, image.mime)
        except (ValueError, DomainError):
            invalid("图片内容与声明格式不一致，支持 PNG、JPEG、WebP、GIF")
        total += len(content)
        if total > MAX_CONTENT:
            invalid("本地知识手册正文与图片合计超过 64 MiB")
        images.append((path, content))
    if total > MAX_CONTENT:
        invalid("本地知识手册正文与图片合计超过 64 MiB")
    return {
        "documents": documents,
        "files": images,
        "document_names": names,
        "template": {"template_id": envelope.template_id, "template_version": envelope.template_version},
    }
