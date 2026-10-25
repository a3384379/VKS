from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    bootstrap_token: str = Field(min_length=16, max_length=256)


class ProjectInput(Input):
    key: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)


class ProjectPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "archived"] | None = None
    current_release_id: str | None = None


class UploadInput(Input):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=134217728)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str = Field(min_length=3, max_length=128)


class ImportInput(Input):
    kind: Literal["bundle", "documents", "prototype"]
    artifact_ids: list[str] = Field(min_length=1, max_length=100)
    analysis_set_id: str | None = None
    prototype_mode: Literal["axure_html", "preview_bundle", "screenshot_hotspots"] | None = None


class ReviewInput(Input):
    decision: Literal["confirm", "reject", "reopen"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class DraftInput(Input):
    analysis_set_id: str
    base_revision_id: str
    markdown: str = Field(max_length=1000000)
    locked_section_ids: list[str] = Field(default_factory=list)


class CandidateInput(Input):
    analysis_set_id: str
    review_batch_id: str | None = None


class PublishInput(Input):
    expected_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    human_confirmation_ref: str
    change_note: str = Field(min_length=1, max_length=2000)


class TokenInput(Input):
    name: str = Field(min_length=1, max_length=160)
    scopes: list[str] = Field(default_factory=lambda: ["knowledge:read"])
    expires_in_days: int = Field(default=30, ge=1, le=365)


class SearchInput(Input):
    release_id: str | None = None
    analysis_set_id: str | None = None
    module_id: str | None = None
    include_descendants: bool = True
    service_key: str | None = Field(default=None, max_length=96)
    query: str = Field(min_length=1, max_length=2000)
    kinds: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=50)


class QuestionInput(Input):
    use_model: bool = False
    release_id: str | None = None
    analysis_set_id: str | None = None
    question: str = Field(min_length=1, max_length=8000)
    action_id: str | None = None


class ExportInput(Input):
    release_id: str
    format: Literal["markdown_bundle", "pdf", "vksbundle"] = "markdown_bundle"
    include_source: bool = False


class AnalysisInput(Input):
    name: str = Field(min_length=1, max_length=160)
    sources: list[dict] = Field(default_factory=list, max_length=64)
    ruleset_version: str = "explicit-binding-v1"


class JobInput(Input):
    kind: Literal["index", "generate_documents", "suggest_links", "detect_drift"]
    analyzer_profile: str | None = None
    action_id: str | None = None
    instruction: str | None = Field(default=None, max_length=8000)
    from_release_id: str | None = None


class ProposalItem(Input):
    from_revision_id: str
    to_revision_id: str
    relation_type: str
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
    causal_basis: Literal["not_established", "temporal_association", "static_potential", "correlated_runtime"] = (
        "not_established"
    )


class ProposalInput(Input):
    analysis_set_id: str
    candidates: list[ProposalItem] = Field(min_length=1, max_length=100)


class MemberInput(Input):
    user_id: str
    role: Literal["viewer", "editor", "reviewer", "maintainer", "admin"]
    status: Literal["active", "disabled"]
