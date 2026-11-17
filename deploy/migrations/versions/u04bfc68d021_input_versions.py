"""Track source mutations independently of generated association progress."""

import sqlalchemy as sa
from alembic import op

revision = "u04bfc68d021"
down_revision = "t93aeb57c910"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if "input_epoch" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "input_epoch",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("token", sa.String(32), nullable=False),
        )
    if connection.dialect.name == "sqlite":
        from vks_api.input_versions import install_sqlite

        install_sqlite(connection)


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in ("entity", "entity_revision", "analysis_set_entity", "artifact", "source_snapshot"):
            for operation in ("insert", "update", "delete"):
                op.execute(sa.text(f'DROP TRIGGER IF EXISTS "input_epoch_{table}_{operation}"'))
    op.drop_table("input_epoch")
