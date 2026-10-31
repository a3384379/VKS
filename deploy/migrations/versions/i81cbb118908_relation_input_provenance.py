"""Separate model input provenance from business evidence on relations."""

from alembic import op
import sqlalchemy as sa

revision = "i81cbb118908"
down_revision = "h71baa007807"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite development startup may already have added this compatible column.
    if "metadata_json" not in {row["name"] for row in sa.inspect(op.get_bind()).get_columns("relation")}:
        op.add_column("relation", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("relation", "metadata_json")
