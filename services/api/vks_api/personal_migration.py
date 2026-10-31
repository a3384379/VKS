"""One-way removal of obsolete governance storage; source history stays intact.

Historical snapshot JSON is kept byte-for-byte. Legacy relation decisions become
ordinary user exclusions; capture input digests replace approval-ticket FKs.
"""

from sqlalchemy import inspect
from sqlalchemy.schema import CreateIndex, CreateTable

RETIRED = (
    "review_decision",
    "document_review",
    "document_workflow",
    "release_candidate",
    "update_proposal",
    "capture_approval",
)


def _rebuild(connection, name, values):
    from .models import Base

    original = Base.metadata.tables[name]
    temporary = original.to_metadata(__import__("sqlalchemy").MetaData(), name=name + "_personal")
    # Resolve referenced table names without creating them again.
    for table in Base.metadata.sorted_tables:
        if table.name != name:
            table.to_metadata(temporary.metadata)
    connection.execute(CreateTable(temporary))
    columns = {row["name"] for row in inspect(connection).get_columns(name)}
    dest, expressions = [], []
    for column in original.columns:
        if column.name in values or column.name in columns:
            dest.append('"' + column.name + '"')
            expressions.append(values.get(column.name, '"' + column.name + '"'))
    connection.exec_driver_sql(
        f'INSERT INTO "{temporary.name}" ({",".join(dest)}) SELECT {",".join(expressions)} FROM "{name}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{name}"')
    connection.exec_driver_sql(f'ALTER TABLE "{temporary.name}" RENAME TO "{name}"')
    for index in original.indexes:
        connection.execute(CreateIndex(index))


def migrate_task_states(connection):
    tables = set(inspect(connection).get_table_names())
    if "analysis_set" in tables:
        connection.exec_driver_sql("UPDATE analysis_set SET state='draft' WHERE state='ready_for_review'")
    if "job" in tables:
        connection.exec_driver_sql("UPDATE job SET state='succeeded', stage='COMPLETE' WHERE state='ready_for_review'")
        connection.exec_driver_sql(
            "UPDATE job SET state='queued', stage='QUEUED' WHERE state='paused' AND stage='PLAN_REVIEW'"
        )


def migrate_sqlite(connection):
    tables = set(inspect(connection).get_table_names())
    if not tables.intersection(RETIRED) and (
        "relation" not in tables
        or "review_status" not in {row["name"] for row in inspect(connection).get_columns("relation")}
    ):
        connection.commit()
        return
    connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        if "capture_session" in tables and "approval_id" in {
            row["name"] for row in inspect(connection).get_columns("capture_session")
        }:
            values = {}
            if "capture_approval" in tables:
                values["input_sha256"] = (
                    "COALESCE((SELECT manifest_sha256 FROM capture_approval WHERE capture_approval.id=capture_session.approval_id),'')"
                )
            _rebuild(connection, "capture_session", values)
        for table in RETIRED:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
        if "relation" in tables and "review_status" in {
            row["name"] for row in inspect(connection).get_columns("relation")
        }:
            _rebuild(connection, "relation", {"excluded": "CASE WHEN review_status='rejected' THEN 1 ELSE 0 END"})
        if "project_member" in tables:
            connection.exec_driver_sql("UPDATE project_member SET role='editor' WHERE role='reviewer'")
        migrate_task_states(connection)
        problems = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if problems:
            raise RuntimeError("Personal-workflow migration left inconsistent references")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
