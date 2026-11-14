"""Persist immutable per-target consumption of captured pages."""

import sqlalchemy as sa
from alembic import op

revision = "s82fda46b8099"
down_revision = "r71eca35a7988"
branch_labels = None
depends_on = None


def upgrade():
    if "prototype_scan_target" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "prototype_scan_target",
            sa.Column("run_id", sa.String(), sa.ForeignKey("prototype_scan_run.id"), primary_key=True),
            sa.Column("target_id", sa.String(64), primary_key=True),
            sa.Column("group_key", sa.String(64), nullable=False),
            sa.Column("item", sa.JSON(), nullable=False),
            sa.Column("job_id", sa.String(), sa.ForeignKey("job.id"), nullable=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("outcome", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
        op.create_index("ix_scan_target_group", "prototype_scan_target", ["run_id", "group_key"])
        op.create_index("ix_scan_target_work", "prototype_scan_target", ["state", "job_id"])


def downgrade():
    op.drop_table("prototype_scan_target")
