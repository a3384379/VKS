"""Opt-in isolated PostgreSQL dump/snapshot/restore exercise."""

import os
from pathlib import Path

import pytest
from test_backup_restore import published
from vks_api.service import DomainError, Service

from scripts.backup_restore import BackupError, backup, restore


@pytest.mark.skipif(not os.getenv("VKS_TEST_RECOVERY_SOURCE_URL"), reason="未配置隔离 PostgreSQL 恢复演练数据库")
def test_real_postgres_snapshot_dump_restore_permissions_and_objects(tmp_path, monkeypatch):
    source = os.environ["VKS_TEST_RECOVERY_SOURCE_URL"]
    target = os.environ["VKS_TEST_RECOVERY_TARGET_URL"]
    container = os.environ["VKS_TEST_PG_TOOLS_CONTAINER"]
    monkeypatch.setenv("VKS_AUTO_MIGRATE", "1")
    root = tmp_path / "original"
    service = Service(root, source)
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "charge-demo", "name": "PostgreSQL恢复"}, "project")
    release, w = published(service, owner, p)
    old_token = service.create_token(owner, p["id"], {"name": "old-token", "scopes": ["knowledge:read"]})
    report = backup(root, tmp_path / "postgres.zip", database_url=source, pg_tools_container=container)
    restored = tmp_path / "restored"
    restore(
        report["path"], restored, expected_sha256=report["sha256"], database_url=target, pg_tools_container=container
    )
    rebuilt = Service(restored, target)
    _, recovered = rebuilt.login(rebuilt.bootstrap_token)
    assert recovered.id == owner.id
    knowledge = rebuilt.workspace(recovered, p["id"], release_id=release["id"])
    assert knowledge["selected_release"]["manifest_sha256"] == release["manifest_sha256"]
    assert len(knowledge["relations"]) == len(w["relations"])
    assert rebuilt.members(recovered, p["id"])[0]["role"] == "admin"
    with pytest.raises(DomainError):
        rebuilt.authenticate(old_token["token"], "token")
    raw, _ = rebuilt.artifact_bytes(recovered, p["id"], knowledge["documents"][0]["artifact_id"])
    assert raw == (Path(__file__).resolve().parents[2] / "fixtures/charge-demo/docs/invoice-rules.md").read_bytes()
    with pytest.raises(BackupError):
        restore(
            report["path"],
            tmp_path / "another",
            expected_sha256=report["sha256"],
            database_url=target,
            pg_tools_container=container,
        )
    service.engine.dispose()
    rebuilt.engine.dispose()
