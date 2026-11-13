"""Public capability flags are implementation/configuration facts, not acceptance claims."""

import os

from sqlalchemy import select


def status(service, session=None, project_id=None):
    if session is None:
        with service.db() as s:
            return status(service, s, project_id)
    from .ai_gateway import ModelPolicy, endpoint_policy
    from .oauth import OAuthIssuer

    policy = session.scalar(select(ModelPolicy).where(ModelPolicy.project_id == project_id)) if project_id else None
    model_enabled = False
    if policy and policy.settings.get("enabled"):
        try:
            endpoint_policy(policy.settings)
            from .model_credentials import key_for_ref

            model_enabled = not policy.settings.get("secret_ref") or bool(
                key_for_ref(policy.settings.get("secret_ref"), policy.settings["endpoint"])
            )
        except Exception:
            model_enabled = False
    return {
        "runner_enabled": os.getenv("VKS_RUNNER_ENABLED") == "1",
        "model_enabled": bool(model_enabled),
        "oauth_enabled": bool(session.scalar(select(OAuthIssuer.id).where(OAuthIssuer.active == True).limit(1))),
        "task_mode": os.getenv("VKS_TASK_MODE", "local"),
        "source_collection": "client-readonly",
    }


def backup_status(service):
    """Read only the local status fields needed by the instance settings view."""
    import json
    import re
    from datetime import datetime

    result = {}
    for name, key, time_key, hash_key in (
        ("backup-status.json", "backup", "created_at", "archive_sha256"),
        ("restore-report.json", "restore", "restored_at", "source_sha256"),
    ):
        path = service.data_dir / name
        if path.is_symlink():
            continue
        try:
            with path.open("rb") as report:
                payload = json.loads(report.read(1024 * 1024))
            timestamp = payload[time_key]
            digest = payload[hash_key]
            database = payload["database_kind"]
            if (
                datetime.fromisoformat(timestamp).tzinfo
                and re.fullmatch(r"[a-f0-9]{64}", digest)
                and database in ("sqlite", "postgres")
            ):
                result[key] = {"timestamp": timestamp, "database_kind": database, "sha256": digest}
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            continue
    return result
