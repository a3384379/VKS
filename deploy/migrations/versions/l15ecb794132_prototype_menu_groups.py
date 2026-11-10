"""Persist quick-menu grouping for existing prototype projects."""

import sqlalchemy as sa
from alembic import op

revision = "l15ecb794132"
down_revision = "k04dba683021"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("prototype_settings")}
    if "menu_groups" not in columns:
        op.add_column(
            "prototype_settings", sa.Column("menu_groups", sa.JSON(), server_default=sa.text("'{}'"), nullable=False)
        )


def downgrade():
    op.drop_column("prototype_settings", "menu_groups")
