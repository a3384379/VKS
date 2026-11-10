"""Add project defaults for the versioned prototype explorer."""

from alembic import op
import sqlalchemy as sa

revision = "k04dba683021"
down_revision = "j93dca582910"
branch_labels = None
depends_on = None


def upgrade():
    if "prototype_settings" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "prototype_settings",
            sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), primary_key=True),
            sa.Column("entry_page_key", sa.String(96), nullable=True),
            sa.Column("menu_order", sa.JSON(), nullable=False),
            sa.Column("row_version", sa.Integer(), nullable=False),
        )


def downgrade():
    op.drop_table("prototype_settings")
