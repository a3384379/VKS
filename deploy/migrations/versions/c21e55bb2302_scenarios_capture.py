"""Immutable scenarios and controlled capture records.

Revision ID: c21e55bb2302
Revises: b19d44aa1201
"""

from alembic import op
from vks_api.scenarios import ScenarioRevision, CaptureEnvironment, CaptureDataset
from vks_api.models import Base, Identified
from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

revision = "c21e55bb2302"
down_revision = "b19d44aa1201"
branch_labels = None
depends_on = None


class LegacyBase(DeclarativeBase):
    pass


class CaptureApproval(Identified, LegacyBase):
    __tablename__ = "capture_approval"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"))
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    environment_id: Mapped[str] = mapped_column(ForeignKey("capture_environment.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_fixture.id"))
    requestor_actor_id: Mapped[str] = mapped_column(String(36))
    approved_by: Mapped[str] = mapped_column(String(36))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40))
    used_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CaptureRecord(Identified, LegacyBase):
    __tablename__ = "capture_session"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"))
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    environment_id: Mapped[str] = mapped_column(ForeignKey("capture_environment.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_fixture.id"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("capture_approval.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    redaction_version: Mapped[str] = mapped_column(String(64), default="vks-runner-redact-1")
    source_alignment: Mapped[str] = mapped_column(String(20), default="unknown")


for _table in Base.metadata.sorted_tables:
    if _table.name not in LegacyBase.metadata.tables:
        _table.to_metadata(LegacyBase.metadata)

TABLES = [
    ScenarioRevision.__table__,
    CaptureEnvironment.__table__,
    CaptureDataset.__table__,
    CaptureApproval.__table__,
    CaptureRecord.__table__,
]


def upgrade():
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    for table in reversed(TABLES):
        table.drop(op.get_bind(), checkfirst=True)
