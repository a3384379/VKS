"""Accurate references constructed from authorized, frozen selections."""

from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentSectionLocator(BaseModel):
    locator_version: Literal[1] = 1
    kind: Literal["document_section"] = "document_section"
    project_id: UUID
    release_id: UUID
    document_id: UUID
    document_revision_id: UUID
    section_revision_id: UUID
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrototypeLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator_version: Literal[1]
    project_id: UUID
    release_id: UUID
    package_artifact_id: UUID
    package_key: UUID | None = None
    package_revision_id: UUID | None = None
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PrototypeBindingLocator(PrototypeLocator):
    kind: Literal["prototype_binding"]
    binding_id: UUID
    point_revision_id: UUID
    binding_version: int = Field(ge=1)


class PrototypePageLocator(PrototypeLocator):
    kind: Literal["prototype_page"]
    page_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["default"]


def studio_base_url(value: str) -> str:
    if not value:
        return ""
    if any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in value):
        raise ValueError("VKS_STUDIO_BASE_URL 必须是无查询或片段的 Studio 来源")
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not (parsed.scheme == "https" or parsed.scheme == "http" and loopback)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.port == 0
    ):
        raise ValueError("VKS_STUDIO_BASE_URL 需要 HTTPS 来源或本机 HTTP 来源")
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def saved_scope(release: Mapping) -> dict:
    return {
        "project_id": release["project_id"],
        "release_id": release["id"],
        "manifest_sha256": release["manifest_sha256"],
        "saved_at": release["published_at"],
        "release_label": release["release_no"],
    }


def document_directory_url(base, pid, rid):
    return base + "#" + urlencode({"project": pid, "release": rid, "view": "documents"}) if base else None


def section_reference(base, pid, rid, document, section):
    locator = DocumentSectionLocator(
        project_id=pid,
        release_id=rid,
        document_id=document.entity_id,
        document_revision_id=document.id,
        section_revision_id=section.id,
        content_sha256=section.content_sha256,
    ).model_dump(mode="json")
    route = {
        "project": pid,
        "release": rid,
        "view": "documents",
        "doc": document.id,
        "section": section.id,
        "reader": "standard",
    }
    return _reference(base, locator, route)


def binding_reference(base, pid, rid, package, binding):
    locator = PrototypeBindingLocator(
        **prototype_identity(pid, rid, package),
        kind="prototype_binding",
        binding_id=binding["id"],
        point_revision_id=binding["point_revision_id"],
        binding_version=binding["binding_version"],
    ).model_dump(mode="json", exclude_none=True)
    route = {
        "project": pid,
        "release": rid,
        "view": "prototype",
        "package": package.get("package_key") or package["id"],
        "binding": binding["id"],
    }
    return _reference(base, locator, route)


def prototype_identity(pid, rid, package):
    return {
        "locator_version": 1,
        "project_id": pid,
        "release_id": rid,
        "package_artifact_id": package["id"],
        "package_key": package.get("package_key") or None,
        "package_revision_id": package.get("revision_id") or None,
        "package_sha256": package["sha256"],
    }


def page_reference(base, pid, rid, package, page_ref, *, raster=False):
    locator = PrototypePageLocator(
        **prototype_identity(pid, rid, package),
        kind="prototype_page",
        page_ref=page_ref,
        state="default",
    ).model_dump(mode="json", exclude_none=True)
    return _reference(
        "" if raster else base,
        locator,
        {
            "project": pid,
            "release": rid,
            "view": "prototype",
            "package": package.get("package_key") or package["id"],
            "page_ref": page_ref,
        },
    )


def _reference(base, locator, route):
    result = {"locator": locator}
    if base:
        result["studio_url"] = base + "#" + urlencode(route)
    return result
