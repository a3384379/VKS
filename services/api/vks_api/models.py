from __future__ import annotations
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, Integer, Text, JSON, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Project(Identified, Base):
    __tablename__ = "project"
    key: Mapped[str] = mapped_column(String(96), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    owner_id: Mapped[str] = mapped_column(String(36))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    current_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __mapper_args__ = {"version_id_col": row_version}


class PrototypeSettings(Base):
    __tablename__ = "prototype_settings"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    entry_page_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    menu_order: Mapped[list] = mapped_column(JSON, default=list)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": row_version}


class Member(Base):
    __tablename__ = "project_member"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="active")


class Credential(Identified, Base):
    __tablename__ = "credential"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(36))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    csrf_token: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[str] = mapped_column(String(40))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Analysis(Identified, Base):
    __tablename__ = "analysis_set"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    key: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(30), default="draft")
    selection_version: Mapped[int] = mapped_column(Integer, default=1)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    ruleset_version: Mapped[str] = mapped_column(String(96), default="explicit-binding-v1")
    runtime_alignment: Mapped[str] = mapped_column(String(20), default="unknown")
    created_by: Mapped[str] = mapped_column(String(36))
    __mapper_args__ = {"version_id_col": selection_version}


class Artifact(Identified, Base):
    __tablename__ = "artifact"
    __table_args__ = (UniqueConstraint("project_id", "snapshot_id", "relative_path"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    media_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    sensitivity: Mapped[str] = mapped_column(String(20), default="internal")
    object_key: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Entity(Identified, Base):
    __tablename__ = "entity"
    __table_args__ = (UniqueConstraint("project_id", "type", "stable_key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    stable_key: Mapped[str] = mapped_column(String(1000))


class Revision(Identified, Base):
    __tablename__ = "entity_revision"
    __table_args__ = (UniqueConstraint("entity_id", "content_sha256"),)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entity.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifact.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(1000))
    content: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64))
    semantic_content_sha256: Mapped[str] = mapped_column(String(64))
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Selection(Base):
    __tablename__ = "analysis_set_entity"
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    entity_revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"))


class Evidence(Identified, Base):
    __tablename__ = "evidence"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"))
    stable_key: Mapped[str] = mapped_column(String(1000))
    evidence_kind: Mapped[str] = mapped_column(String(20))
    content_sha256: Mapped[str] = mapped_column(String(64))
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    claim: Mapped[str] = mapped_column(Text, default="")


class Relation(Identified, Base):
    __tablename__ = "relation"
    __table_args__ = (UniqueConstraint("analysis_set_id", "from_revision_id", "to_revision_id", "relation_type"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    from_revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"))
    to_revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"))
    relation_type: Mapped[str] = mapped_column(String(40))
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    validity_status: Mapped[str] = mapped_column(String(20), default="valid")
    causal_basis: Mapped[str] = mapped_column(String(40), default="not_established")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    manual_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __mapper_args__ = {"version_id_col": row_version}


class Job(Identified, Base):
    __tablename__ = "job"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(96), default="VALIDATE")
    completeness: Mapped[str] = mapped_column(String(20), default="none")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(36))
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class Outbox(Identified, Base):
    __tablename__ = "outbox_event"
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False)


class Release(Identified, Base):
    __tablename__ = "knowledge_release"
    __table_args__ = (
        UniqueConstraint("project_id", "release_no"),
        UniqueConstraint("analysis_set_id"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    release_no: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    published_by: Mapped[str] = mapped_column(String(36))
    change_note: Mapped[str] = mapped_column(Text)


class Upload(Identified, Base):
    __tablename__ = "upload_session"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    artifact_id: Mapped[str] = mapped_column(String(36), unique=True)
    created_by: Mapped[str] = mapped_column(String(36))
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(128))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[str] = mapped_column(String(40))


class Idempotency(Identified, Base):
    __tablename__ = "idempotency"
    __table_args__ = (UniqueConstraint("project_id", "actor_id", "operation", "key"),)
    project_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[dict] = mapped_column(JSON)


class Audit(Identified, Base):
    __tablename__ = "audit_event"
    project_id: Mapped[str | None] = mapped_column(ForeignKey("project.id"), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(20), default="succeeded")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


def relation_payload(value):
    """Read legacy immutable snapshots without rewriting their bytes/hash."""
    row = dict(value)
    legacy = row.pop("review_status", None)
    row.setdefault("excluded", legacy == "rejected")
    return row
