"""Public responses for the versioned prototype API and MCP tools."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from .prototype_edit import Anchor, Viewport

T = TypeVar("T")


class PrototypeEnvelope(BaseModel, Generic[T]):
    data: T
    meta: dict


class PrototypeScope(BaseModel):
    analysis_set_id: str
    release_id: str | None = None


class PrototypeSource(BaseModel):
    section_key: str
    section_id: str
    section_revision_id: str
    title: str
    excerpt: str
    evidence_ids: list[str]
    validity: str


class PrototypePoint(BaseModel):
    point_key: str
    point_revision_id: str
    title: str
    summary: str
    applicability: str
    outcome: str
    exceptions: str
    sources: list[PrototypeSource]
    binding_id: str
    binding_version: int
    state_keys: list[str]
    priority: int
    validity: str


class PrototypeContext(BaseModel):
    scope: PrototypeScope
    page_key: str
    page_revision_id: str
    action_key: str
    action_id: str
    action_revision_id: str
    trigger_state_key: str
    display_state_key: str
    missing_target_surface: bool
    points: list[PrototypePoint]
    documents: list[PrototypeSource]
    availability: Literal["available", "no_visible_explanation"]


class PrototypePageEntry(BaseModel):
    page_key: str
    name: str
    page_revision_id: str
    routes: list[str]
    has_surface: bool
    group_name: str | None = None


class PrototypeMenu(BaseModel):
    entry_page_key: str | None
    pages: list[PrototypePageEntry]
    total: int
    next_cursor: str | None


class PrototypeSettingsResult(BaseModel):
    row_version: int
    entry_page_key: str | None
    menu_order: list[str]
    menu_groups: dict[str, str]


class PrototypeAnchor(Anchor):
    action_id: str
    action_revision_id: str


class PrototypePage(BaseModel):
    page_key: str
    page_revision_id: str
    name: str
    state_key: str
    state_name: str
    origin: str
    viewport: Viewport | None
    image_sha256: str | None
    anchors: list[PrototypeAnchor]


class PrototypeReference(BaseModel):
    page_key: str
    page_id: str
    page_name: str
    state_key: str
    state_name: str
    action_key: str
    action_id: str
    action_name: str


class PrototypeReferences(BaseModel):
    section_revision_id: str
    references: list[PrototypeReference]
    total: int
    next_cursor: str | None


class PrototypeBindingResult(BaseModel):
    point_key: str
    point_revision_id: str
    binding_id: str
    binding_version: int
    selection_version: int


class PrototypeAnchorsResult(BaseModel):
    page_revision_id: str
    selection_version: int
