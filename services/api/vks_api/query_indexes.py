"""Additive covering indexes for exact revision/selection validation."""

from sqlalchemy import inspect

INDEXES = {
    "entity": ("ix_entity_scope_facts", ["id", "project_id", "type", "stable_key"]),
    "entity_revision": (
        "ix_revision_scope_inputs",
        ["id", "project_id", "entity_id", "artifact_id", "content_sha256", "semantic_content_sha256"],
    ),
    "analysis_set_entity": (
        "ix_selection_revision_inputs",
        ["analysis_set_id", "entity_revision_id", "entity_id"],
    ),
}


def ensure_sqlite_query_indexes(connection):
    """Upgrade existing local tables too; create_all skips their new indexes."""
    from .models import Base

    tables = set(inspect(connection).get_table_names())
    created = False
    for table, (name, _columns) in INDEXES.items():
        if table not in tables:
            continue
        existing = {entry["name"] for entry in inspect(connection).get_indexes(table)}
        if name not in existing:
            index = next(index for index in Base.metadata.tables[table].indexes if index.name == name)
            index.create(connection, checkfirst=True)
            created = True
    if created:
        # Statistics make the planner choose the covering path over a small
        # project-prefix estimate followed by repeated full selection scans.
        for table in INDEXES:
            if table in tables:
                connection.exec_driver_sql(f'ANALYZE "{table}"')
