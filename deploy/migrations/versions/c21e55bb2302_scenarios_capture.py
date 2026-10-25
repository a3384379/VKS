"""Immutable scenarios and controlled capture records.

Revision ID: c21e55bb2302
Revises: b19d44aa1201
"""

from alembic import op
from vks_api.scenarios import ScenarioRevision, CaptureEnvironment, CaptureDataset, CaptureApproval, CaptureRecord

revision = "c21e55bb2302"
down_revision = "b19d44aa1201"
branch_labels = None
depends_on = None
TABLES = [
    ScenarioRevision.__table__,
    CaptureEnvironment.__table__,
    CaptureDataset.__table__,
    CaptureApproval.__table__,
    CaptureRecord.__table__,
]


def upgrade():
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    for table in reversed(TABLES):
        table.drop(op.get_bind(), checkfirst=True)
