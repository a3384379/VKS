import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[2]


def test_module_membership_schema_upgrade_and_roundtrip(tmp_path):
    url = "sqlite:///" + str(tmp_path / "migration.sqlite")
    env = dict(os.environ, VKS_DATABASE_URL=url, PYTHONPATH=str(ROOT / "services/api") + os.pathsep + str(ROOT))

    def migrate(direction, target):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", direction, target],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "e41f77dd4504")
    migrate("upgrade", "f51f88ee5605")
    engine = create_engine(url)
    assert {"business_module", "module_entity", "module_source", "module_connector"} <= set(
        inspect(engine).get_table_names()
    )
    engine.dispose()
    migrate("downgrade", "e41f77dd4504")
    migrate("upgrade", "f51f88ee5605")
