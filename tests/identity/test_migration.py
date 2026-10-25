"""Upgrade the real prior schema; do not use create_all as a migration test."""

import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]


def test_team_identity_migration_preserves_existing_projects_and_roundtrips(tmp_path):
    database_url = "sqlite:///" + str(tmp_path / "migration.sqlite")
    env = {
        **os.environ,
        "VKS_DATABASE_URL": database_url,
        "PYTHONPATH": str(ROOT / "services/api") + os.pathsep + str(ROOT),
    }

    def migrate(direction, revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", direction, revision],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "ad8c03345211")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project(id,key,name,description,status,owner_id,row_version,created_at) VALUES('11111111-1111-4111-8111-111111111111','preserved','Preserved project','','active','22222222-2222-4222-8222-222222222222',1,'2026-10-25T00:00:00Z')"
            )
        )
    engine.dispose()
    migrate("upgrade", "b19d44aa1201")
    engine = create_engine(database_url)
    assert {"user_identity", "project_member_grant", "machine_credential_owner", "identity_login_throttle"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT name FROM project")) == "Preserved project"
    engine.dispose()
    migrate("downgrade", "ad8c03345211")
    migrate("upgrade", "b19d44aa1201")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT name FROM project")) == "Preserved project"
    engine.dispose()
