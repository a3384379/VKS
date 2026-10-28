"""Additive V2 response contracts; legacy entity/evidence DTOs remain extensible."""

from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict
from packages.journey_dsl import Journey

T = TypeVar("T")


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    request_id: str


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Metadata


class JourneyOutput(BaseModel):
    journey_id: str
    revision_id: str
    content_sha256: str
    created_at: str
    dsl: Journey


class JourneyValidation(BaseModel):
    dsl: Journey
    sha256: str
    requires_review: bool


class JourneyDecision(BaseModel):
    revision_id: str
    decision: Literal["approve", "reject"]


class JobOutput(BaseModel):
    id: str
    project_id: str
    kind: str
    state: str
    stage: str
    completeness: str
    progress: int
    attempt: int
    created_at: str
    created_by: str
    updated_at: str
    issue_count: int | None = None
    result_refs: list[dict[str, Any]]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    diagnostics_restricted: bool = False


class TaskOutput(JobOutput):
    task_kind: str
    analysis_set_id: str
    stages: list[str]
    stage_history: list[dict[str, str]]
    budget: dict[str, int]
    report: dict[str, Any] | None
    stage_artifacts: list[dict[str, Any]]
    module_decisions: dict[str, str]
    pause_requested: bool = False
    plan_confirmation_required: bool = False
    plan_preview: dict[str, Any] | None = None
    plan_preview_unavailable: bool = False


class ModuleAccepted(BaseModel):
    module_id: str
    key: str
    analysis_set_id: str


class CoveragePair(BaseModel):
    confirmed: int
    total: int


class RelationCoverage(CoveragePair):
    candidate: int
    stale: int
    broken: int


class CoverageOutput(BaseModel):
    project_id: str
    analysis_set_id: str | None
    release_id: str | None
    source_vector: list[dict[str, Any]]
    scope: str
    actions: CoveragePair
    relations: RelationCoverage
    journeys: dict[str, int]
    knowledge_health: dict[str, int]
    source_alignment: str
    unknowns: list[str]


class KnowledgePath(BaseModel):
    revision_ids: list[str]
    relation_ids: list[str]
    causal_basis: list[str]
    review_status: list[str]
    evidence_ids: list[str]
    interpretation: str


class KnowledgeMapOutput(BaseModel):
    analysis_set_id: str
    release_id: str | None
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    truncated: bool
    unknowns: list[str]


class ExplanationOutput(BaseModel):
    project_id: str
    analysis_set_id: str
    release_id: str | None
    action_id: str
    action_revision_id: str
    source_vector: list[dict[str, Any]]
    source_alignment: str
    entities: list[dict[str, Any]]
    paths: list[KnowledgePath]
    journeys: list[JourneyOutput]
    relations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    is_truncated: bool
    unknowns: list[str]


class BusinessReferencesOutput(BaseModel):
    project_id: str
    analysis_set_id: str
    release_id: str | None
    entity_id: str
    actions: list[dict[str, Any]]
    is_truncated: bool
    unknowns: list[str]


class OnboardingOutput(BaseModel):
    project_id: str
    analysis_set_id: str | None
    source_vector: list[dict[str, Any]]
    allow_platform_llm: bool
    requires_model_policy: bool
    source_alignment: str
    issues: list[str]
    capabilities: list[str]
    requires_review: bool
    server_filesystem_access: bool
    preflight_sha256: str


class ReplayScenario(BaseModel):
    entity_id: str
    revision_id: str
    name: str
    states: list[dict[str, Any]]
    transitions: list[dict[str, Any]]


class ReplayOutput(BaseModel):
    project_id: str
    analysis_set_id: str | None
    release_id: str | None
    source_vector: list[dict[str, Any]]
    scenarios: list[ReplayScenario]
    mode: Literal["passive"]
    unknowns: list[str]


class InboxItem(BaseModel):
    kind: Literal["relation", "journey", "migration", "document"]
    subject_id: str
    data: dict[str, Any]
