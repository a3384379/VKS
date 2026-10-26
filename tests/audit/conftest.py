"""Audit fixtures use only private temporary SQLite and synthetic materials."""
from types import SimpleNamespace

import pytest

from vks_api.service import Service


@pytest.fixture
def audit_store(tmp_path):
    service = Service(tmp_path / "store", database_url=f"sqlite:///{tmp_path / 'audit.db'}")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Synthetic audit"}, "project")
    job = service.demo_import(owner, project["id"], "demo")
    service.process_job(job["id"])
    workspace = service.workspace(owner, project["id"])
    yield SimpleNamespace(s=service, owner=owner, p=project["id"], aid=workspace["selected_analysis_set"]["id"], w=workspace)
    service.stop_worker()
    service.engine.dispose()
