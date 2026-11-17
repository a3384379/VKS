"""Cover exact scope validation without reading full revision bodies."""

import sqlalchemy as sa
from alembic import op

revision = "t93aeb57c910"
down_revision = "s82fda46b8099"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_entity_scope_facts", "entity", ["id", "project_id", "type", "stable_key"]),
    (
        "ix_revision_scope_inputs",
        "entity_revision",
        ["id", "project_id", "entity_id", "artifact_id", "content_sha256", "semantic_content_sha256"],
    ),
    ("ix_selection_revision_inputs", "analysis_set_entity", ["analysis_set_id", "entity_revision_id", "entity_id"]),
)


def upgrade():
    connection = op.get_bind()
    created = False
    for name, table, columns in INDEXES:
        if name not in {index["name"] for index in sa.inspect(connection).get_indexes(table)}:
            op.create_index(name, table, columns)
            created = True
    if created:
        for table in ("entity", "entity_revision", "analysis_set_entity"):
            op.execute(sa.text(f'ANALYZE "{table}"'))


def downgrade():
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
