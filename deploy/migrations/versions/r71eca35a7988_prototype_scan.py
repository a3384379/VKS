"""Persist browser scan progress independently of routes and model jobs."""

import sqlalchemy as sa
from alembic import op

revision = "r71eca35a7988"
down_revision = "q60dbf249687"
branch_labels = None
depends_on = None


def upgrade():
    if "prototype_scan_run" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "prototype_scan_run",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("project.id"), nullable=False),
            sa.Column("analysis_set_id", sa.String(), sa.ForeignKey("analysis_set.id"), nullable=False),
            sa.Column("created_by", sa.String(96), nullable=False),
            sa.Column("mode", sa.String(16), nullable=False),
            sa.Column("composition", sa.String(64), nullable=False),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("client_id", sa.String(96), nullable=False),
            sa.Column("lease_until", sa.String(40), nullable=False),
            sa.Column("plan", sa.JSON(), nullable=False),
            sa.Column("completed", sa.JSON(), nullable=False),
            sa.Column("current", sa.JSON(), nullable=True),
            sa.Column("batch_job_id", sa.String(36), nullable=True),
            sa.Column("row_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
        for column in ("project_id", "analysis_set_id", "created_by"):
            op.create_index(f"ix_prototype_scan_run_{column}", "prototype_scan_run", [column])


def downgrade():
    op.drop_table("prototype_scan_run")
