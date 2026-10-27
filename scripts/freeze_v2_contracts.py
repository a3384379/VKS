"""Freeze the additive V2 API on an isolated empty SQLite database."""

import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
sys.path.insert(0, str(ROOT))


def document():
    from vks_api.app import create_app

    with tempfile.TemporaryDirectory(prefix="vks-contract-") as directory:
        app = create_app(directory, database_url="sqlite:///:memory:", enable_worker=False)
        try:
            spec = app.openapi()
            return dict(
                openapi=spec["openapi"],
                info={"title": "VKS additive V2 API", "version": "2.0-dev"},
                paths={key: value for key, value in spec["paths"].items() if key.startswith("/api/v2/")},
                components=spec["components"],
            )
        finally:
            app.state.service.engine.dispose()


if __name__ == "__main__":
    destination = ROOT / "packages/contracts/openapi_2.0.json"
    # app.py owns a module-level app too. Isolate that first import, even when
    # the caller has a live-instance database URL in its environment.
    with tempfile.TemporaryDirectory(prefix="vks-contract-bootstrap-") as bootstrap:
        original = {key: os.environ.get(key) for key in ("VKS_DATA_DIR", "VKS_DATABASE_URL", "VKS_TASK_MODE")}
        try:
            os.environ.update(VKS_DATA_DIR=bootstrap, VKS_DATABASE_URL="sqlite:///:memory:", VKS_TASK_MODE="external")
            destination.write_text(json.dumps(document(), ensure_ascii=False, indent=2) + "\n")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
