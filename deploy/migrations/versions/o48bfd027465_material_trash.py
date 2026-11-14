"""Keep project library removals separate from immutable revisions and snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "o48bfd027465"
down_revision = "n37aec916354"
branch_labels = None
depends_on = None


def upgrade():
    if "material_trash" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "material_trash",
            sa.Column("project_id", sa.String(), sa.ForeignKey("project.id"), primary_key=True),
            sa.Column("kind", sa.String(), primary_key=True),
            sa.Column("target_id", sa.String(), primary_key=True),
            sa.Column("deleted_by", sa.String(), nullable=False),
            sa.Column("deleted_at", sa.String(), nullable=False),
        )


def downgrade():
    op.drop_table("material_trash")
