"""Persist local team identities, independent grants and credential ownership.

Revision ID: b19d44aa1201
Revises: ad8c03345211
"""

from alembic import op
import sqlalchemy as sa

revision = "b19d44aa1201"
down_revision = "ad8c03345211"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_identity",
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("password_hash", sa.String(300), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "project_member_grant",
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_identity.id"]),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_table(
        "machine_credential_owner",
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["credential_id"], ["credential.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_identity.id"]),
        sa.PrimaryKeyConstraint("credential_id"),
    )
    op.create_index("ix_machine_credential_owner_user_id", "machine_credential_owner", ["user_id"])
    op.create_table(
        "identity_login_throttle",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.String(40), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade():
    op.drop_table("identity_login_throttle")
    op.drop_index("ix_machine_credential_owner_user_id", table_name="machine_credential_owner")
    op.drop_table("machine_credential_owner")
    op.drop_table("project_member_grant")
    op.drop_table("user_identity")
