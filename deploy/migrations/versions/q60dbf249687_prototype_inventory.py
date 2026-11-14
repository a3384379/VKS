"""Persist source-bound browser observations of declared prototype pages."""

import sqlalchemy as sa
from alembic import op

revision = "q60dbf249687"
down_revision = "p59cae138576"
branch_labels = None
depends_on = None


def upgrade():
    if "prototype_inventory_page" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "prototype_inventory_page",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("project.id"), nullable=False),
            sa.Column("analysis_set_id", sa.String(), sa.ForeignKey("analysis_set.id"), nullable=False),
            sa.Column("package_id", sa.String(), sa.ForeignKey("artifact.id"), nullable=False),
            sa.Column("composition", sa.String(64), nullable=False),
            sa.Column("artifact_sha256", sa.String(64), nullable=False),
            sa.Column("route", sa.String(1000), nullable=False),
            sa.Column("frame_path", sa.JSON(), nullable=False),
            sa.Column("report", sa.JSON(), nullable=False),
            sa.Column("observed_by", sa.String(96), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
        for column in ("project_id", "analysis_set_id", "package_id"):
            op.create_index(f"ix_prototype_inventory_page_{column}", "prototype_inventory_page", [column])


def downgrade():
    op.drop_table("prototype_inventory_page")
