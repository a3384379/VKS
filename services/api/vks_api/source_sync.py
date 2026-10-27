"""Opaque client consent + verified upload -> durable import event. No client paths."""

from typing import Any
from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .models import Analysis, Audit, Job, Project
from .service import DomainError, sha
from .v2_contracts import Envelope, JobOutput
from packages.collector.sync import snapshot_delta
from packages.analyzers.vks_ingest import _read_zip, _json, _schema, safe_path, BundleError


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(pattern=r"^[a-z0-9-]{8,64}$")
    consent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_id: str = Field(min_length=1, max_length=36)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_snapshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    analysis_set_id: str | None = None
    detect_changes: bool = False


class SyncAccepted(BaseModel):
    profile_id: str
    snapshot_sha256: str
    delta: dict[str, Any]
    job: JobOutput


class SourceSyncService:
    def __init__(self, service):
        self.service = service

    def create(self, actor, pid, payload, key):
        service = self.service
        with service.db() as s:
            service.require(s, actor, pid, "source:import")
            s.scalar(select(Project).where(Project.id == pid).with_for_update())

            def save():
                if payload.get("detect_changes"):
                    service.require(s, actor, pid, "analysis:run")
                if payload.get("analysis_set_id"):
                    analysis = s.get(Analysis, payload["analysis_set_id"])
                    if not analysis or analysis.project_id != pid:
                        raise DomainError("VKS_NOT_FOUND", "未找到同步目标草稿", 404)
                    if analysis.state == "frozen":
                        raise DomainError("VKS_STATE_CONFLICT", "已发布基线不能同步写入，请选择草稿", 409)
                base = (
                    select(Job)
                    .join(Audit, Audit.resource_id == Job.id)
                    .where(
                        Audit.project_id == pid,
                        Audit.action == "source_sync.enqueue",
                        Audit.metadata_json["profile_id"].as_string() == payload["profile_id"],
                    )
                )
                latest = s.scalar(base.order_by(Audit.created_at.desc(), Audit.id.desc()).limit(1))
                if latest and latest.created_by != actor.id:
                    raise DomainError("VKS_NOT_FOUND", "未找到当前身份的同步范围", 404)
                if s.scalar(base.where(Job.state.in_(("queued", "running", "retry_wait", "cancelling"))).limit(1)):
                    raise DomainError("VKS_STATE_CONFLICT", "此同步范围已有未完成任务，请继续查询原任务", 409)
                before = {}
                previous = s.scalar(
                    base.where(Job.state == "succeeded").order_by(Audit.created_at.desc(), Audit.id.desc()).limit(1)
                )
                if previous:
                    metadata = previous.input_json["_source_sync"]
                    if metadata["consent_sha256"] != payload["consent_sha256"]:
                        raise DomainError("VKS_SYNC_SCOPE_CHANGED", "授权范围改变，请重新建立同步配置", 409)
                    if metadata["snapshot_sha256"] != payload.get("previous_snapshot_sha256"):
                        raise DomainError("VKS_VERSION_CONFLICT", "同步游标已变化，请查询原任务或重新核对", 412)
                    before = metadata["manifest"]
                elif payload.get("previous_snapshot_sha256"):
                    raise DomainError("VKS_VERSION_CONFLICT", "同步游标没有可验证的成功记录", 412)
                raw, _ = service._upload_data(s, actor, pid, payload["artifact_id"])
                if sha(raw) != payload["snapshot_sha256"]:
                    raise DomainError("VKS_HASH_MISMATCH", "同步包摘要不一致", 422)
                # Only verify the byte manifest here. Semantic/AST work belongs
                # to the durable worker, outside this cursor transaction.
                try:
                    files = _read_zip(raw)
                    manifest = _json(files["manifest.json"])
                    _schema(manifest, "import_manifest.schema.json")
                    if manifest["project_key"] != s.get(Project, pid).key:
                        raise DomainError("VKS_PROJECT_MISMATCH", "同步包不属于当前项目", 422)
                    current = {}
                    for row in manifest["artifacts"]:
                        path = safe_path(row["path"])
                        content = files.get(path)
                        if (
                            path in current
                            or content is None
                            or len(content) != row["size_bytes"]
                            or sha(content) != row["sha256"]
                        ):
                            raise DomainError("VKS_HASH_MISMATCH", "同步文件清单或实际字节不一致", 422)
                        current[path] = row["sha256"]
                except BundleError as exc:
                    raise DomainError(exc.code, exc.message, 422) from None
                delta = snapshot_delta(
                    before, current, scope_before=payload["consent_sha256"], scope_after=payload["consent_sha256"]
                )
                if delta["scope_anomaly"]:
                    raise DomainError("VKS_SYNC_SCOPE_ANOMALY", "大量材料缺失，核对授权范围后再同步", 409)
                body = dict(
                    kind="bundle",
                    artifact_ids=[payload["artifact_id"]],
                    _source_sync=dict(
                        profile_id=payload["profile_id"],
                        consent_sha256=payload["consent_sha256"],
                        snapshot_sha256=payload["snapshot_sha256"],
                        previous_snapshot_sha256=payload.get("previous_snapshot_sha256"),
                        manifest=current,
                        delta=delta,
                        detect_changes=payload.get("detect_changes", False),
                        from_release_id=s.get(Project, pid).current_release_id
                        if payload.get("detect_changes")
                        else None,
                    ),
                )
                if payload.get("analysis_set_id"):
                    body["analysis_set_id"] = payload["analysis_set_id"]
                job = service._new_job(s, actor, pid, "import", body)
                service.audit(
                    s,
                    actor,
                    pid,
                    "source_sync.enqueue",
                    job.id,
                    metadata={k: payload[k] for k in ("profile_id", "consent_sha256", "snapshot_sha256")},
                )
                return dict(
                    profile_id=payload["profile_id"],
                    snapshot_sha256=payload["snapshot_sha256"],
                    delta=delta,
                    job=service.job_dto(job),
                )

            return service.idem(s, actor, pid, "source_sync.create", key, payload, save)

    def finalize(self, s, job, actor, result):
        context = job.input_json.get("_source_sync", {})
        if not context.get("detect_changes"):
            return result
        self.service.require(s, actor, job.project_id, "analysis:run")
        if not context.get("from_release_id"):
            return dict(result, change_detection={"state": "not_applicable", "reason": "no_release_baseline"})
        drift = self.service._new_job(
            s,
            actor,
            job.project_id,
            "detect_drift",
            dict(analysis_set_id=result["analysis_set_id"], from_release_id=context["from_release_id"]),
        )
        self.service.audit(
            s,
            actor,
            job.project_id,
            "source_sync.impact_enqueued",
            drift.id,
            metadata={"source_job_id": job.id, "profile_id": context["profile_id"]},
        )
        return dict(
            result,
            impact_job_id=drift.id,
            change_detection={"state": "queued", "from_release_id": context["from_release_id"]},
        )


def install_source_sync(app, service, actor, envelope):
    sync = SourceSyncService(service)

    @app.get(
        "/api/v2/collector-agent",
        response_class=Response,
        responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    def download(a=Depends(actor)):
        from packages.collector.distribution import distribution

        raw = distribution()
        return Response(
            raw,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="vks-collector-2.0.zip"',
                "X-Content-SHA256": sha(raw),
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/v2/projects/{pid}/source-syncs", status_code=202, response_model=Envelope[SyncAccepted])
    def create(pid: str, payload: SyncInput, request: Request, a=Depends(actor), idempotency_key: str = Header(...)):
        return envelope(request, sync.create(a, pid, payload.model_dump(), idempotency_key))
