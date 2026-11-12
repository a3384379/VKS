"""Persist each project's selected interactive workspace."""

import sqlalchemy as sa
from alembic import op

revision = "n37aec916354"
down_revision = "m26fdb805243"
branch_labels = None
depends_on = None


def upgrade():
    if "studio_workspace" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "studio_workspace",
            sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), primary_key=True),
            sa.Column("analysis_set_id", sa.String(36), sa.ForeignKey("analysis_set.id"), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False),
        )


def downgrade():
    op.drop_table("studio_workspace")
