import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]


def test_document_and_proposal_migration_upgrades_existing_source_schema(tmp_path):
    url = "sqlite:///" + str(tmp_path / "migration.sqlite")
    env = dict(os.environ, VKS_DATABASE_URL=url, PYTHONPATH=str(ROOT / "services/api") + os.pathsep + str(ROOT))

    def migrate(direction, revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", direction, revision],
            cwd=ROOT,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "d31f66cc3403")
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO project(id,key,name,description,status,owner_id,row_version,created_at) VALUES('11111111-1111-4111-8111-111111111111','preserved','Preserved','','active','22222222-2222-4222-8222-222222222222',1,'2026-10-25T00:00:00Z')"
            )
        )
    engine.dispose()
    migrate("upgrade", "head")
    engine = create_engine(url)
    assert not {
        "document_workflow",
        "document_review",
        "update_proposal",
        "capture_approval",
        "release_candidate",
        "review_decision",
    } & set(inspect(engine).get_table_names())
    assert "excluded" in {row["name"] for row in inspect(engine).get_columns("relation")}
    assert "review_status" not in {row["name"] for row in inspect(engine).get_columns("relation")}
    assert "input_sha256" in {row["name"] for row in inspect(engine).get_columns("capture_session")}
    with engine.connect() as db:
        assert db.scalar(text("SELECT name FROM project")) == "Preserved"
        assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
    migrate("upgrade", "head")
