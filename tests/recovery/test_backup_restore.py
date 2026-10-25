import hashlib
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from vks_api.service import Service

from scripts.backup_restore import BackupError, backup, restore


def published(service, owner, p):
    job = service.demo_import(owner, p["id"], "demo")
    service.process_job(job["id"])
    w = service.workspace(owner, p["id"])
    for r in w["relations"]:
        service.review_relation(
            owner,
            r["id"],
            {"decision": "confirm", "reason": "核对合成材料", "evidence_ids": r["evidence_ids"]},
            r["row_version"],
        )
    candidate = service.request_release(
        owner, p["id"], {"analysis_set_id": w["selected_analysis_set"]["id"]}, "precheck"
    )
    confirmation = service.confirm_release(owner, candidate["candidate_id"])
    release = service.publish(
        owner,
        candidate["candidate_id"],
        {k: confirmation[k] for k in ("expected_manifest_sha256", "human_confirmation_ref")}
        | {"change_note": "备份恢复样例"},
        "publish",
    )
    return release, w


def test_restore_preserves_release_objects_permissions_but_rotates_bootstrap_and_revokes_tokens(tmp_path):
    root = tmp_path / "original"
    service = Service(root)
    old_bootstrap = service.bootstrap_token
    _, owner = service.login(old_bootstrap)
    p = service.create_project(owner, {"key": "charge-demo", "name": "恢复演练"}, "project")
    release, w = published(service, owner, p)
    token = service.create_token(owner, p["id"], {"name": "old-client", "scopes": ["knowledge:read"]})
    (root / ".env").write_text("SUPER_SECRET=excluded")
    report = backup(root, tmp_path / "backup.zip")
    with ZipFile(report["path"]) as z:
        assert "bootstrap-token" not in z.namelist() and ".env" not in z.namelist()
        assert old_bootstrap.encode() not in z.read("manifest.json")
    restored = tmp_path / "restored"
    restore(report["path"], restored, expected_sha256=report["sha256"])
    assert not (restored / "bootstrap-token").exists()
    rebuilt = Service(restored)
    assert rebuilt.bootstrap_token != old_bootstrap
    _, new_owner = rebuilt.login(rebuilt.bootstrap_token)
    assert new_owner.id == owner.id
    new_w = rebuilt.workspace(new_owner, p["id"], release_id=release["id"])
    assert new_w["selected_release"]["manifest_sha256"] == release["manifest_sha256"]
    assert len(new_w["relations"]) == len(w["relations"])
    raw, _ = rebuilt.artifact_bytes(new_owner, p["id"], new_w["documents"][0]["artifact_id"])
    assert raw == (Path(__file__).resolve().parents[2] / "fixtures/charge-demo/docs/invoice-rules.md").read_bytes()
    with pytest.raises(Exception):
        rebuilt.authenticate(token["token"], "token")
    assert os.stat(report["path"]).st_mode & 0o077 == 0
    service.engine.dispose()
    rebuilt.engine.dispose()


def test_backup_rejects_missing_objects_and_never_overwrites(tmp_path):
    root = tmp_path / "original"
    service = Service(root)
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "charge-demo", "name": "x"}, "project")
    release, w = published(service, owner, p)
    target = tmp_path / "backup.zip"
    backup(root, target)
    with pytest.raises(BackupError):
        backup(root, target)
    artifact = w["documents"][0]["artifact_id"]
    with service.db() as s:
        from vks_api.models import Artifact

        key = s.get(Artifact, artifact).object_key
    (root / "objects" / key).unlink()
    with pytest.raises(BackupError):
        backup(root, tmp_path / "incomplete.zip")
    assert not (tmp_path / "incomplete.zip").exists()
    service.engine.dispose()


def test_restore_rejects_external_digest_mismatch_traversal_and_existing_destination(tmp_path):
    malicious = tmp_path / "bad.zip"
    with ZipFile(malicious, "w", ZIP_DEFLATED) as z:
        z.writestr("../outside", "forbidden")
    expected = hashlib.sha256(malicious.read_bytes()).hexdigest()
    with pytest.raises(BackupError):
        restore(malicious, tmp_path / "restored", expected_sha256="0" * 64)
    with pytest.raises(BackupError):
        restore(malicious, tmp_path / "restored", expected_sha256=expected)
    assert not (tmp_path / "restored").exists() and not (tmp_path / "outside").exists()
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("unrelated")
    with pytest.raises(BackupError):
        restore(malicious, existing, expected_sha256=expected)
    assert (existing / "keep").read_text() == "unrelated"


def test_backup_restore_cli_runs_without_pythonpath(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "original-cli"
    service = Service(root)
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "charge-demo", "name": "CLI 验收"}, "project")
    published(service, owner, p)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith(("VKS_DB_", "VKS_DATABASE_"))}
    script = Path(__file__).resolve().parents[2] / "scripts/backup_restore.py"
    archive = tmp_path / "cli-backup.zip"
    result = subprocess.run(
        [sys.executable, str(script), "backup", "--data-dir", str(root), "--destination", str(archive)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "restore",
            "--archive",
            str(archive),
            "--destination",
            str(tmp_path / "restored-cli"),
            "--expected-sha256",
            report["sha256"],
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert (tmp_path / "restored-cli/vks.db").exists()
    service.engine.dispose()
