"""Local consent profiles and resumable upload receipts. Credentials stay in RAM."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4, UUID
import re
import httpx

from .snapshots import CollectionError, bundle_bytes
from .sync import atomic_private, collect_authorized, inventory, snapshot_delta


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SyncProfile:
    id: str
    root: str
    provider: str
    revision: str | None
    project_id: str
    project_key: str
    source_key: str
    base_url: str
    consent_sha256: str
    analysis_set_id: str | None = None
    interval_seconds: int = 0
    detect_changes: bool = False

    def scope(self):
        return dict(
            root=self.root,
            provider=self.provider,
            project_id=self.project_id,
            project_key=self.project_key,
            source_key=self.source_key,
            base_url=self.base_url,
            analysis_set_id=self.analysis_set_id,
            **({"detect_changes": True} if self.detect_changes else {}),
        )

    def validate(self):
        try:
            UUID(self.id)
            UUID(self.project_id)
        except (ValueError, TypeError):
            raise CollectionError("VKS_INVALID_INPUT", "配置和项目标识需要准确 UUID") from None
        if not all(re.fullmatch(r"[a-z][a-z0-9._-]{0,95}", key or "") for key in (self.project_key, self.source_key)):
            raise CollectionError("VKS_INVALID_INPUT", "项目 key 和来源 key 无效")
        if self.consent_sha256 != digest(self.scope()):
            raise CollectionError("VKS_SYNC_SCOPE_CHANGED", "同步配置范围改变，请重新授权")
        parts = urlsplit(self.base_url)
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise CollectionError("VKS_INVALID_INPUT", "VKS 地址只能是无凭据的站点地址")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise CollectionError("VKS_FORBIDDEN", "远端 VKS 地址需要 HTTPS")
        if (
            self.provider not in ("directory", "git", "svn")
            or self.interval_seconds
            and not 30 <= self.interval_seconds <= 86400
        ):
            raise CollectionError("VKS_INVALID_INPUT", "来源或同步间隔不合法")
        if self.provider in ("git", "svn") and not self.revision:
            raise CollectionError("VKS_INVALID_INPUT", "Git/SVN 必须明确填写 ref 或固定 revision")


def authorize_profile(
    *,
    root,
    provider,
    project_id,
    project_key,
    source_key,
    base_url,
    consent,
    revision=None,
    analysis_set_id=None,
    interval_seconds=0,
    detect_changes=False,
):
    if not consent:
        raise CollectionError("VKS_FORBIDDEN", "必须在本机明确授权来源范围")
    if not root or not str(root).strip():
        raise CollectionError("VKS_INVALID_INPUT", "请选择明确的来源目录或仓库")
    if provider != "svn":
        path = Path(root).absolute()
        if not path.is_dir() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise CollectionError("VKS_FORBIDDEN", "选择真实的本机目录，不允许链接或目录联接")
        root = str(path)
    scope = dict(
        root=root,
        provider=provider,
        project_id=project_id,
        project_key=project_key,
        source_key=source_key,
        base_url=base_url.rstrip("/"),
        analysis_set_id=analysis_set_id,
        **({"detect_changes": True} if detect_changes else {}),
    )
    profile = SyncProfile(
        id=str(uuid4()),
        revision=revision or None,
        consent_sha256=digest(scope),
        interval_seconds=interval_seconds,
        **scope,
    )
    profile.validate()
    return profile


class AgentClient:
    def __init__(self, base_url, token, *, transport=None):
        if not token:
            raise CollectionError("VKS_UNAUTHENTICATED", "请输入有当前项目导入权限的专用令牌")
        # Validate transport independently of profiles too.
        parts = urlsplit(base_url)
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
            or parts.scheme != "https"
            and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"))
        ):
            raise CollectionError("VKS_FORBIDDEN", "VKS 地址不符合 HTTPS/本机隔离策略")
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": "Bearer " + token},
            transport=transport,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    def request(self, method, path, *, envelope=False, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            if response.is_redirect:
                raise CollectionError("VKS_FORBIDDEN", "VKS 返回重定向，未向其他站点转发令牌")
            if response.is_error:
                value = response.json().get("error", {})
                raise CollectionError(
                    value.get("code", "VKS_UPLOAD_FAILED"), "VKS 未接受操作，请核对权限、版本或服务状态"
                )
            body = response.json()
            return body if envelope else body["data"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if isinstance(exc, CollectionError):
                raise
            raise CollectionError("VKS_DEPENDENCY_UNAVAILABLE", "VKS 连接或响应失败；保留本机回执以便重试") from None

    def close(self):
        self.client.close()


class LocalCollectorAgent:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        if ".vks" not in [p.casefold() for p in self.directory.parts]:
            raise CollectionError("VKS_FORBIDDEN", "本机配置、原始快照和回执必须位于私有 .vks/ 目录")
        self.lock = Lock()

    def paths(self, profile):
        profile.validate()
        if not profile.id or any(c not in "0123456789abcdef-" for c in profile.id):
            raise CollectionError("VKS_INVALID_INPUT", "配置标识无效")
        return self.directory / profile.id

    def save_profile(self, profile):
        base = self.paths(profile)
        atomic_private(
            base / "profile.json", (json.dumps(asdict(profile), ensure_ascii=False, indent=2) + "\n").encode()
        )

    def load_profiles(self):
        rows = []
        for path in sorted(self.directory.glob("*/profile.json")):
            profile = SyncProfile(**json.loads(path.read_bytes()))
            profile.validate()
            rows.append(profile)
        return rows

    def preview(self, profile):
        base = self.paths(profile)
        if (base / "pending.json").exists():
            return json.loads((base / "pending.json").read_bytes())
        previous = (
            json.loads((base / "cursor.json").read_bytes())
            if (base / "cursor.json").exists()
            else {"files": {}, "consent_sha256": profile.consent_sha256}
        )
        snapshot = collect_authorized(profile.root, profile.root, profile.provider, profile.revision)
        current = inventory(snapshot)
        delta = snapshot_delta(
            previous["files"], current, scope_before=previous["consent_sha256"], scope_after=profile.consent_sha256
        )
        if delta["scope_anomaly"]:
            raise CollectionError("VKS_SYNC_SCOPE_ANOMALY", "大量文件缺失；未上传或推进游标，请核对范围")
        if current == previous["files"] and previous.get("revision") == snapshot.revision:
            return dict(state="unchanged", delta=delta, revision=snapshot.revision)
        baseline = "sync-" + profile.id[:8] + "-" + digest([current, snapshot.revision])[:24]
        raw = bundle_bytes(snapshot, profile.project_key, baseline, source_key=profile.source_key)
        from packages.analyzers.vks_ingest import BundleError, parse_bundle

        try:
            parse_bundle(raw, profile.project_key)
        except BundleError as exc:
            raise CollectionError(exc.code, "本机预检未通过，未上传资料；请检查秘密、材料格式或来源范围") from None
        pending = dict(
            state="collected",
            consent_sha256=profile.consent_sha256,
            files=current,
            revision=snapshot.revision,
            snapshot_sha256=sha256(raw).hexdigest(),
            previous_snapshot_sha256=previous.get("snapshot_sha256"),
            delta=delta,
            idempotency_key="sync-" + str(uuid4()),
        )
        atomic_private(base / "snapshot.zip", raw)
        atomic_private(base / "pending.json", (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode())
        return pending

    def sync_once(self, profile, client, *, retry_failed=False):
        if not self.lock.acquire(blocking=False):
            raise CollectionError("VKS_STATE_CONFLICT", "本机已有同步操作，请等待完成")
        try:
            return self._sync(profile, client, retry_failed)
        finally:
            self.lock.release()

    def _sync(self, profile, client, retry_failed):
        pending = self.preview(profile)
        if pending["state"] == "unchanged":
            return pending
        if pending["consent_sha256"] != profile.consent_sha256:
            raise CollectionError("VKS_SYNC_SCOPE_CHANGED", "待同步快照的授权范围发生变化")
        base = self.paths(profile)

        def save():
            atomic_private(base / "pending.json", (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode())

        raw = (base / "snapshot.zip").read_bytes()
        if sha256(raw).hexdigest() != pending["snapshot_sha256"]:
            raise CollectionError("VKS_HASH_MISMATCH", "本机待同步包摘要不一致，未上传")
        prefix = "/api/v1/projects/" + profile.project_id
        if "upload" not in pending:
            pending["upload"] = client.request(
                "POST",
                prefix + "/upload-sessions",
                json=dict(
                    filename="source.vksbundle.zip",
                    size_bytes=len(raw),
                    sha256=pending["snapshot_sha256"],
                    media_type="application/zip",
                ),
            )
            save()
        if pending["state"] == "collected":
            upload = pending["upload"]["upload_session_id"]
            if not isinstance(upload, str) or any(c not in "0123456789abcdef-" for c in upload):
                raise CollectionError("VKS_INVALID_INPUT", "上传会话标识不合法")
            client.request("PUT", prefix + "/uploads/" + upload, content=raw)
            pending["state"] = "uploaded"
            save()
        if "request" not in pending:
            preflight = client.request(
                "POST",
                prefix + "/imports/preflight",
                json=dict(kind="bundle", artifact_ids=[pending["upload"]["artifact_id"]]),
            )
            if not preflight.get("valid"):
                raise CollectionError("VKS_INVALID_INPUT", "资料未通过 VKS 格式预检")
            pending["request"] = dict(
                profile_id=profile.id,
                consent_sha256=profile.consent_sha256,
                artifact_id=pending["upload"]["artifact_id"],
                snapshot_sha256=pending["snapshot_sha256"],
                previous_snapshot_sha256=pending["previous_snapshot_sha256"],
                analysis_set_id=profile.analysis_set_id,
                detect_changes=profile.detect_changes,
            )
            pending["state"] = "import_intent"
            save()
        if "job_id" not in pending:
            accepted = client.request(
                "POST",
                "/api/v2/projects/" + profile.project_id + "/source-syncs",
                json=pending["request"],
                headers={"Idempotency-Key": pending["idempotency_key"]},
            )
            pending["job_id"] = accepted["job"]["id"]
            pending["state"] = "queued"
            save()
        job = client.request("GET", "/api/v1/jobs/" + pending["job_id"])
        if job["state"] in ("failed", "cancelled"):
            if retry_failed:
                client.request("POST", "/api/v1/jobs/" + pending["job_id"] + "/retry", json={})
                pending["state"] = "queued"
            else:
                pending["state"] = job["state"]
            save()
            return pending
        if job["state"] != "succeeded":
            pending["state"] = job["state"]
            save()
            return pending
        cursor = dict(
            consent_sha256=profile.consent_sha256,
            files=pending["files"],
            revision=pending["revision"],
            snapshot_sha256=pending["snapshot_sha256"],
            job_id=pending["job_id"],
            analysis_set_id=job["result"]["analysis_set_id"],
            **({"impact_job_id": job["result"]["impact_job_id"]} if job.get("result", {}).get("impact_job_id") else {}),
        )
        atomic_private(base / "cursor.json", (json.dumps(cursor, ensure_ascii=False, indent=2) + "\n").encode())
        (base / "pending.json").unlink()
        return dict(state="succeeded", delta=pending["delta"], **cursor)
