"""Private, content-verified VKS backup/restore. Never overwrite a live instance.

SQLite snapshots use SQLite's online backup API. PostgreSQL snapshots use
pg_export_snapshot + pg_dump; client credentials travel only through environment.
Restore requires an externally recorded archive hash and an empty destination.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

HASH = re.compile(r"^[a-f0-9]{64}$")
MAX_BYTES = 2 * 1024 * 1024 * 1024


class BackupError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _reference_keys(rows):
    keys = set()
    for table, records in rows.items():
        for row in records:
            if table == "artifact" and row.get("object_key"):
                keys.add(row["object_key"])
            if table == "upload_session" and row.get("complete") and row.get("sha256"):
                keys.add(row["sha256"])
            if table in ("job", "job_checkpoint"):
                value = row.get("result_json") or {}
                if isinstance(value, str):
                    value = json.loads(value)
                if value.get("object_key"):
                    keys.add(value["object_key"])
    if any(not HASH.fullmatch(k) for k in keys):
        raise BackupError("数据库包含非法对象键")
    return keys


def _sqlite_rows(connection):
    names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    connection.row_factory = sqlite3.Row
    for table in ("artifact", "upload_session", "job", "job_checkpoint", "knowledge_release"):
        result[table] = [dict(r) for r in connection.execute(f"SELECT * FROM {table}")] if table in names else []
    return result


def _postgres_rows(connection):
    names = set(inspect(connection).get_table_names())
    return {
        table: [dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {table}"))]
        if table in names
        else []
        for table in ("artifact", "upload_session", "job", "job_checkpoint", "knowledge_release")
    }


def _read_object(root, key):
    path = root / "objects" / key
    if not HASH.fullmatch(key) or path.is_symlink() or not path.is_file():
        raise BackupError("缺少引用对象或检测到符号链接")
    raw = path.read_bytes()
    if digest(raw) != key:
        raise BackupError("对象 SHA-256 校验失败")
    return raw


def _pg_command(action, database_url, *, container=None, snapshot=None):
    url = make_url(database_url)
    if not url.database or url.database in ("postgres", "template0", "template1"):
        raise BackupError("请使用明确的应用数据库")
    environment = {**os.environ, "PGPASSWORD": url.password or ""}
    command = []
    if container:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise BackupError("PostgreSQL 客户端容器名称无效")
        command = ["docker", "exec", "-i", "--env", "PGPASSWORD", container]
        host, port = "127.0.0.1", 5432
    else:
        if shutil.which(action) is None:
            raise BackupError("需要匹配版本的 PostgreSQL 客户端，或指定 pg-tools-container")
        host, port = url.host or "127.0.0.1", url.port or 5432
    command += [
        action,
        "--host",
        host,
        "--port",
        str(port),
        "--username",
        url.username or "vks",
        "--dbname",
        url.database,
        "--no-owner",
        "--no-acl",
    ]
    if action == "pg_dump":
        command += ["--format=custom", *(["--snapshot", snapshot] if snapshot else [])]
    else:
        command += ["--exit-on-error", "--single-transaction"]
    return command, environment


def backup(data_dir, destination, *, database_url=None, pg_tools_container=None):
    root = Path(data_dir).resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise BackupError("备份目标已存在，不会覆盖")
    if not (root / "owner-id").is_file() or (root / "owner-id").is_symlink():
        raise BackupError("实例身份文件缺失或不安全")
    owner = (root / "owner-id").read_text().strip()
    UUID(owner)
    target.parent.mkdir(parents=True, exist_ok=True)
    files = {"owner-id": owner.encode()}
    with tempfile.TemporaryDirectory(prefix="vks-backup-", dir=target.parent) as temp:
        os.chmod(temp, 0o700)
        kind = "postgres" if database_url and make_url(database_url).get_backend_name() == "postgresql" else "sqlite"
        if kind == "sqlite":
            source = root / "vks.db" if not database_url else Path(make_url(database_url).database)
            if source.is_symlink() or not source.is_file():
                raise BackupError("SQLite 数据库不存在或不安全")
            snapshot = Path(temp) / "database.sqlite"
            with sqlite3.connect(source) as db, sqlite3.connect(snapshot) as copied:
                db.backup(copied)
            with sqlite3.connect(snapshot) as copied:
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("数据库完整性检查失败")
                rows = _sqlite_rows(copied)
            files["database.sqlite"] = snapshot.read_bytes()
        else:
            engine = create_engine(database_url)
            try:
                with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                    with conn.begin():
                        snapshot = conn.scalar(text("SELECT pg_export_snapshot()"))
                        rows = _postgres_rows(conn)
                        command, environment = _pg_command(
                            "pg_dump", database_url, container=pg_tools_container, snapshot=snapshot
                        )
                        result = subprocess.run(command, env=environment, capture_output=True, timeout=600)
                        if result.returncode:
                            raise BackupError("PostgreSQL 一致性备份失败；未生成可用备份")
                        files["database.dump"] = result.stdout
            finally:
                engine.dispose()
        keys = _reference_keys(rows)
        checkpoint_keys = set()
        for record in rows.get("job_checkpoint", []):
            value = record.get("result_json") or {}
            if isinstance(value, str):
                value = json.loads(value)
            if value.get("object_key"):
                checkpoint_keys.add(value["object_key"])
        for key in checkpoint_keys:
            raw = _read_object(root, key)
            # PARSE checkpoints can reference separately stored raw artifact bytes.
            try:
                payload = json.loads(raw)
            except (UnicodeError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("artifacts"), list):
                for artifact in payload["artifacts"]:
                    ref = artifact.get("content_ref") if isinstance(artifact, dict) else None
                    if ref:
                        if not HASH.fullmatch(ref):
                            raise BackupError("缓存引用对象键无效")
                        keys.add(ref)
        for key in sorted(keys):
            files["objects/" + key] = _read_object(root, key)
        if sum(map(len, files.values())) > MAX_BYTES:
            raise BackupError("本地备份超过2GiB预算，请使用企业对象存储备份流程")
        manifest = {
            "format": "vks-private-backup-1",
            "database_kind": kind,
            "created_at": datetime.now(UTC).isoformat(),
            "files": [
                {"path": name, "size_bytes": len(raw), "sha256": digest(raw)} for name, raw in sorted(files.items())
            ],
            "release_manifests": [
                {"id": r["id"], "manifest_sha256": r["manifest_sha256"]} for r in rows["knowledge_release"]
            ],
            "excluded": ["bootstrap-token", "environment variables", "credential secret plaintext"],
            "credentials_policy": "restore revokes sessions, machine tokens and OAuth project grants",
        }
        staging = Path(temp) / "backup.zip"
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
            archive.writestr("manifest.json", canonical(manifest))
        staging.chmod(0o600)
        try:
            os.link(staging, target)
        except FileExistsError:
            raise BackupError("备份目标在执行期间已被创建，未覆盖") from None
    archive_hash = digest(target.read_bytes())
    recorded = False
    try:
        with tempfile.TemporaryDirectory(prefix=".backup-status-", dir=root) as directory:
            status = Path(directory) / "status.json"
            status.write_bytes(
                canonical(
                    {
                        "created_at": manifest["created_at"],
                        "database_kind": kind,
                        "archive_sha256": archive_hash,
                    }
                )
            )
            status.chmod(0o600)
            os.replace(status, root / "backup-status.json")
            recorded = True
    except OSError:
        # The archive is still usable when the source is mounted read-only.
        pass
    return {
        "path": str(target),
        "sha256": archive_hash,
        "status_recorded": recorded,
        "object_count": len(keys),
        "release_count": len(manifest["release_manifests"]),
    }


def _validated_archive(path, expected_sha256):
    raw = Path(path).read_bytes()
    if not expected_sha256 or not HASH.fullmatch(expected_sha256) or digest(raw) != expected_sha256:
        raise BackupError("备份的外部 SHA-256 不匹配")
    files = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 100_000:
                raise BackupError("备份条目过多")
            for entry in archive.infolist():
                name = entry.filename
                allowed = name in ("manifest.json", "owner-id", "database.sqlite", "database.dump") or (
                    name.startswith("objects/") and HASH.fullmatch(name[8:])
                )
                mode = (entry.external_attr >> 16) & 0xFFFF
                if not allowed or name in files or entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise BackupError("备份包含非法、重复或不安全路径")
                total += entry.file_size
                if (
                    total > MAX_BYTES
                    or entry.file_size > MAX_BYTES
                    or entry.file_size > max(entry.compress_size, 1) * 1000
                ):
                    raise BackupError("备份超出恢复资源预算")
                files[name] = archive.read(entry)
    except (zipfile.BadZipFile, OSError):
        raise BackupError("备份 ZIP 无效") from None
    try:
        manifest = json.loads(files.pop("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("备份清单无效") from None
    if manifest.get("format") != "vks-private-backup-1" or manifest.get("database_kind") not in ("sqlite", "postgres"):
        raise BackupError("备份格式不受支持")
    declared = manifest.get("files", [])
    if len({x["path"] for x in declared}) != len(declared) or set(files) != {x["path"] for x in declared}:
        raise BackupError("备份清单条目不闭合")
    for item in declared:
        content = files[item["path"]]
        if len(content) != item["size_bytes"] or digest(content) != item["sha256"]:
            raise BackupError("备份条目摘要不匹配")
        if item["path"].startswith("objects/") and digest(content) != item["path"][8:]:
            raise BackupError("对象名和摘要不一致")
    UUID(files["owner-id"].decode().strip())
    if ("database.sqlite" if manifest["database_kind"] == "sqlite" else "database.dump") not in files:
        raise BackupError("数据库备份缺失")
    return manifest, files


def _invalidate_credentials(connection, dialect):
    if dialect == "sqlite":
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "credential" in names:
            connection.execute("UPDATE credential SET revoked=1")
        if "client_project_grant" in names:
            connection.execute("UPDATE client_project_grant SET active=0")
        if "job" in names:
            connection.execute(
                "UPDATE job SET state='failed',stage='RESTORED_REAUTH_REQUIRED',lease_until=NULL,error_json=? WHERE state IN ('running','queued','retry_wait','cancelling')",
                (
                    json.dumps(
                        {"code": "VKS_APPROVAL_REQUIRED", "message": "恢复后需要重新授权任务", "retryable": False}
                    ),
                ),
            )
        connection.commit()
    else:
        names = set(inspect(connection).get_table_names())
        if "credential" in names:
            connection.execute(text("UPDATE credential SET revoked=true"))
        if "client_project_grant" in names:
            connection.execute(text("UPDATE client_project_grant SET active=false"))
        if "job" in names:
            connection.execute(
                text(
                    "UPDATE job SET state='failed',stage='RESTORED_REAUTH_REQUIRED',lease_until=NULL,error_json=:error WHERE state IN ('running','queued','retry_wait','cancelling')"
                ),
                {
                    "error": json.dumps(
                        {"code": "VKS_APPROVAL_REQUIRED", "message": "恢复后需要重新授权任务", "retryable": False}
                    )
                },
            )


def restore(archive, destination, *, expected_sha256, database_url=None, pg_tools_container=None):
    target = Path(destination).resolve()
    if target.exists():
        raise BackupError("恢复只允许全新的实例目录，不会覆盖现有文件")
    manifest, files = _validated_archive(archive, expected_sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".vks-restore-", dir=target.parent))
    staging.chmod(0o700)
    engine = None
    try:
        for name, raw in files.items():
            relative = "vks.db" if name == "database.sqlite" else name
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o600)
        if manifest["database_kind"] == "sqlite":
            if database_url and make_url(database_url).get_backend_name() != "sqlite":
                raise BackupError("数据库类型不匹配")
            with sqlite3.connect(staging / "vks.db") as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("恢复数据库完整性检查失败")
                required = _reference_keys(_sqlite_rows(db))
                if any("objects/" + key not in files for key in required):
                    raise BackupError("恢复对象引用缺失")
                _invalidate_credentials(db, "sqlite")
        else:
            if not database_url or make_url(database_url).get_backend_name() != "postgresql":
                raise BackupError("PostgreSQL 恢复需显式指定空应用数据库")
            engine = create_engine(database_url)
            with engine.connect() as conn:
                if inspect(conn).get_table_names():
                    raise BackupError("目标数据库非空，不会覆盖")
            command, environment = _pg_command("pg_restore", database_url, container=pg_tools_container)
            result = subprocess.run(
                command, input=files["database.dump"], env=environment, capture_output=True, timeout=600
            )
            if result.returncode:
                raise BackupError("PostgreSQL 事务恢复失败")
            with engine.begin() as conn:
                required = _reference_keys(_postgres_rows(conn))
                if any("objects/" + key not in files for key in required):
                    raise BackupError("恢复数据库对象引用缺失")
                _invalidate_credentials(conn, "postgres")
            (staging / "database.dump").unlink()
        report = {
            "source_sha256": expected_sha256,
            "restored_at": datetime.now(UTC).isoformat(),
            "database_kind": manifest["database_kind"],
            "credentials_revoked": True,
            "release_manifests": manifest["release_manifests"],
        }
        (staging / "restore-report.json").write_bytes(canonical(report))
        (staging / "restore-report.json").chmod(0o600)
        if target.exists():
            raise BackupError("恢复目标在执行期间已存在，未覆盖")
        os.rename(staging, target)
        return report
    finally:
        if engine:
            engine.dispose()
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--data-dir", required=True)
    b.add_argument("--destination", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--archive", required=True)
    r.add_argument("--destination", required=True)
    r.add_argument("--expected-sha256", required=True)
    for item in (b, r):
        item.add_argument("--pg-tools-container")
    args = parser.parse_args()
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
    from vks_api.connection import database_url

    url = database_url()
    try:
        result = (
            backup(args.data_dir, args.destination, database_url=url, pg_tools_container=args.pg_tools_container)
            if args.action == "backup"
            else restore(
                args.archive,
                args.destination,
                expected_sha256=args.expected_sha256,
                database_url=url,
                pg_tools_container=args.pg_tools_container,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (
        BackupError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        SQLAlchemyError,
        subprocess.TimeoutExpired,
    ):
        raise SystemExit("VKS_BACKUP_RESTORE_FAILED: 操作未完成，请检查目标为空、受信摘要、数据库客户端及对象完整性")


if __name__ == "__main__":
    main()
