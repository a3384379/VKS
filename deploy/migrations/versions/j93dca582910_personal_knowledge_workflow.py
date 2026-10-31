"""Remove approvals and content masking metadata for personal workflows.

Revision history/materials/manifests are preserved; old rejected edges stay
excluded. Restore the pre-upgrade DB/object backup to roll back this removal.
"""

from alembic import op
import sqlalchemy as sa

revision = "j93dca582910"
down_revision = "i81cbb118908"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        from vks_api.personal_migration import migrate_sqlite

        migrate_sqlite(connection)
        return
    tables = set(sa.inspect(connection).get_table_names())
    columns = {row["name"] for row in sa.inspect(connection).get_columns("relation")}
    if "excluded" not in columns:
        op.add_column("relation", sa.Column("excluded", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "review_status" in columns:
        connection.exec_driver_sql("UPDATE relation SET excluded=(review_status='rejected')")
    if "capture_session" in tables:
        capture = {row["name"] for row in sa.inspect(connection).get_columns("capture_session")}
        if "input_sha256" not in capture:
            op.add_column(
                "capture_session", sa.Column("input_sha256", sa.String(64), nullable=False, server_default="")
            )
        if "approval_id" in capture:
            if "capture_approval" in tables:
                connection.exec_driver_sql(
                    "UPDATE capture_session SET input_sha256=COALESCE((SELECT manifest_sha256 FROM capture_approval WHERE id=capture_session.approval_id),'')"
                )
            for foreign_key in sa.inspect(connection).get_foreign_keys("capture_session"):
                if foreign_key["constrained_columns"] == ["approval_id"]:
                    op.drop_constraint(foreign_key["name"], "capture_session", type_="foreignkey")
            op.drop_column("capture_session", "approval_id")
        if "redaction_version" in capture:
            op.drop_column("capture_session", "redaction_version")
    from vks_api.personal_migration import RETIRED, migrate_task_states

    for table in RETIRED:
        if table in tables:
            op.drop_table(table)
    if "review_status" in columns:
        op.drop_column("relation", "review_status")
    migrate_task_states(connection)
    connection.exec_driver_sql("UPDATE project_member SET role='editor' WHERE role='reviewer'")


def downgrade():
    raise RuntimeError(
        "Restore the pre-removal database/object backup; removed approval history cannot be reconstructed."
    )
