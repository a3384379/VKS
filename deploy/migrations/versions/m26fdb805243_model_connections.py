"""Persist named provider connections without credential values."""

import sqlalchemy as sa
from alembic import op

revision = "m26fdb805243"
down_revision = "l15ecb794132"
branch_labels = None
depends_on = None


def upgrade():
    if "model_connection" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "model_connection",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False),
            sa.Column("provider", sa.String(30), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("settings", sa.JSON(), nullable=False),
        )
        op.create_index("ix_model_connection_project_id", "model_connection", ["project_id"])


def downgrade():
    op.drop_table("model_connection")
