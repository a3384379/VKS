"""Keep document presentation versioned with its analysis workspace."""

import sqlalchemy as sa
from alembic import op

revision = "p59cae138576"
down_revision = "o48bfd027465"
branch_labels = None
depends_on = None


def upgrade():
    if "document_presentation" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "document_presentation",
            sa.Column("analysis_set_id", sa.String(), sa.ForeignKey("analysis_set.id"), primary_key=True),
            sa.Column("settings", sa.JSON(), nullable=False),
        )


def downgrade():
    op.drop_table("document_presentation")
