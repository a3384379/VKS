"""Durable dispatch helpers; database state is authoritative, never the broker."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Credential, Identified, Job, Outbox, now, uid

PERSONAL_TASK = ContextVar("vks_personal_task", default=None)


class DispatchState(Identified, Base):
    __tablename__ = "job_dispatch"
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    next_attempt_at: Mapped[str] = mapped_column(String(40), default=now)
    last_error_code: Mapped[str] = mapped_column(String(80), default="")


class Checkpoint(Identified, Base):
    __tablename__ = "job_checkpoint"
    __table_args__ = (UniqueConstraint("job_id", "stage", "input_hash", "processor_version"),)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    processor_version: Mapped[str] = mapped_column(String(96))
    result_json: Mapped[dict] = mapped_column(JSON)


def current_actor(service, actor_id, pid, session=None, *, job=None):
    from dataclasses import replace

    from .identity import UserIdentity, ensure_owner, human_scopes
    from .service import Actor, DomainError

    if service.personal_mode and job is not None and job.kind in {"associate_all", "material_migrate"}:
        from .service import ALL_SCOPES

        return Actor(service.owner_id, frozenset(ALL_SCOPES), True)

    if session is None:
        with service.db() as s:
            return current_actor(service, actor_id, pid, s, job=job)
    authorization = job.input_json.get("_authorization") if job else None
    if authorization is not None and (
        not isinstance(authorization, dict)
        or authorization.get("version") != 1
        or authorization.get("actor_id") != actor_id
        or authorization.get("project_id") != pid
        or authorization.get("kind") != job.kind
        or not isinstance(authorization.get("scopes"), list)
        or any(not isinstance(scope, str) for scope in authorization["scopes"])
    ):
        raise DomainError("VKS_UNAUTHENTICATED", "任务授权快照无效", 401)

    def bounded(actor):
        if authorization is None:
            return actor  # Legacy human/platform-token jobs retain their existing checks.
        scopes = set(actor.scopes) & set(authorization["scopes"])
        if scopes:
            # All requested scopes belong to the same current principal/project.
            # Evaluate that grant set once inside this call, instead of querying
            # identity, membership and project separately for every scope. This
            # is not retained across calls/transactions or grant mutations.
            from .identity import project_grants

            try:
                _project, granted = project_grants(service, session, actor, pid)
                scopes &= granted
            except DomainError:
                scopes = set()
        return replace(actor, scopes=frozenset(scopes))

    ensure_owner(service, session)
    identity = session.get(UserIdentity, actor_id)
    if identity:
        if identity.status != "active":
            raise DomainError("VKS_FORBIDDEN", "任务发起人已停用", 403)
        return bounded(Actor(actor_id, frozenset(human_scopes(service, session, identity)), True))
    from .oauth import OAuthPrincipal, actor_for_job

    if session.get(OAuthPrincipal, actor_id) is not None:
        return bounded(actor_for_job(service, session, actor_id, pid, authorization))
    credential = session.scalar(
        select(Credential).where(
            Credential.actor_id == actor_id,
            Credential.kind == "token",
            Credential.revoked.is_(False),
            Credential.expires_at > now(),
        )
    )
    if not credential:
        raise DomainError("VKS_UNAUTHENTICATED", "任务客户端授权已失效", 401)
    actor = Actor(actor_id, frozenset(credential.scopes), False, credential.project_id, credential.id)
    service.require(session, actor, pid)
    return bounded(actor)


@contextmanager
def heartbeat(service, jid, attempt):
    """Refresh only the same active attempt. Cancellation never renews scheduling."""
    from .service import future

    done = Event()

    def pulse():
        while not done.wait(10):
            try:
                with service.db() as s:
                    job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
                    if not job or job.attempt != attempt or job.state != "running":
                        return
                    job.lease_until = future(60)
            except Exception:
                # A failed lease refresh cannot publish; final attempt CAS still applies.
                continue

    worker = Thread(target=pulse, daemon=True, name="vks-job-lease")
    worker.start()
    try:
        yield
    finally:
        done.set()
        worker.join(timeout=1)


def dispatch_mode():
    mode = os.getenv("VKS_TASK_MODE", "local")
    if mode not in ("local", "celery", "external"):
        raise ValueError("VKS_TASK_MODE must be local, celery, or external")
    return mode


def reconcile(service):
    """Recover DB-authoritative jobs after worker loss; never replay a write capture."""

    with service.db() as s:
        for job in s.scalars(
            select(Job)
            .where(Job.state.in_(["running", "cancelling", "retry_wait"]), Job.lease_until < now())
            .with_for_update(skip_locked=True)
        ):
            if job.cancel_requested:
                job.state = "cancelled"
                job.stage = "CANCELLED"
            elif job.kind == "capture":
                job.state = "failed"
                job.stage = "INTERRUPTED"
                job.error_json = {
                    "code": "VKS_STATE_CONFLICT",
                    "message": "采集进程中断，请重新启动录制；不会自动重复写操作",
                    "retryable": False,
                }
            else:
                job.state = "queued"
                job.stage = "RECOVERED"
                out = s.scalar(select(Outbox).where(Outbox.job_id == job.id))
                if out:
                    out.dispatched = False
                dispatch = s.scalar(select(DispatchState).where(DispatchState.job_id == job.id))
                if dispatch:
                    dispatch.state = "pending"
                    dispatch.next_attempt_at = now()
            job.lease_until = None
            job.updated_at = now()


def run_pending(service):
    reconcile(service)
    from .scan_pipeline import reconcile as reconcile_pipeline

    reconcile_pipeline(service)
    if dispatch_mode() == "external":
        return
    if dispatch_mode() == "celery":
        dispatch_pending(service)
        return
    # Browser-produced page jobs must not block the dispatcher while a model
    # responds. Keep a bounded local pool; the database claim still fences
    # duplicate workers and enforces the per-run limit in every dispatch mode.
    from .association_limits import GLOBAL_REQUEST_LIMIT, request_concurrency
    from .prototype_scan import PrototypeScanRun

    with service.pipeline_lock:
        if service.stop.is_set():
            return
        service.pipeline_futures = {
            jid: future for jid, future in service.pipeline_futures.items() if not future.done()
        }
        if len(service.pipeline_futures) < GLOBAL_REQUEST_LIMIT:
            with service.db() as s:
                candidates = list(
                    s.execute(
                        select(
                            Job.id,
                            func.coalesce(
                                PrototypeScanRun.plan["concurrency"].as_integer(),
                                Job.input_json["pipeline_concurrency"].as_integer(),
                            ),
                        )
                        .outerjoin(PrototypeScanRun, PrototypeScanRun.id == Job.input_json["scan_run_id"].as_string())
                        .where(
                            Job.state == "queued",
                            ((Job.kind == "associate_all") & Job.input_json["scan_pipeline"].as_boolean().is_(True))
                            | (Job.kind == "reassociation_preview"),
                            Job.id.not_in(list(service.pipeline_futures)),
                        )
                        .order_by(Job.created_at, Job.id)
                        .limit(GLOBAL_REQUEST_LIMIT)
                    )
                )
            configured = max((request_concurrency(lanes) for _, lanes in candidates), default=1)
            capacity = max(0, configured - len(service.pipeline_futures))
            pipeline_ids = [jid for jid, _ in candidates[:capacity]]
            if pipeline_ids and service.pipeline_executor is None:
                service.pipeline_executor = ThreadPoolExecutor(
                    max_workers=GLOBAL_REQUEST_LIMIT, thread_name_prefix="vks-page-association"
                )
            for jid in pipeline_ids:
                service.pipeline_futures[jid] = service.pipeline_executor.submit(execute_job, service, jid)
    with service.db() as s:
        ids = list(
            s.scalars(
                select(Job.id)
                .where(
                    Job.state == "queued",
                    Job.kind != "reassociation_preview",
                    (Job.kind != "associate_all") | Job.input_json["scan_pipeline"].as_boolean().is_not(True),
                )
                .order_by(Job.created_at)
                .limit(1)
            )
        )
    for jid in ids:
        execute_job(service, jid)


def publish_job(job_id, kind):
    """Only a UUID is sent to the broker; content/identity remain in the database."""
    from services.worker.celery_app import celery

    queue = (
        "vks-browser"
        if kind == "capture"
        else "vks-model"
        if kind in ("generate_documents", "knowledge_task", "material_migrate", "associate_all", "reassociation_preview")
        else "vks-index"
    )
    celery.send_task("vks.execute_job", args=[job_id], queue=queue, serializer="json", retry=False)


def dispatch_pending(service, publisher=None, limit=10):
    from .models import Outbox
    from .service import future

    publisher = publisher or publish_job
    due = []
    with service.db() as s:
        jobs = list(
            s.scalars(
                select(Job)
                .where(Job.state == "queued")
                .order_by(Job.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for job in jobs:
            entry = s.scalar(select(DispatchState).where(DispatchState.job_id == job.id).with_for_update())
            if not entry:
                entry = DispatchState(
                    id=uid(), job_id=job.id, attempt=0, state="pending", next_attempt_at=now(), last_error_code=""
                )
                s.add(entry)
                s.flush()
            if entry.state == "failed":
                # A queued job after terminal dispatch failure was explicitly retried by its owner.
                entry.state = "pending"
                entry.attempt = 0
                entry.next_attempt_at = now()
            if entry.next_attempt_at > now():
                continue
            entry.state = "sending"
            entry.attempt += 1
            entry.next_attempt_at = future(30)
            due.append((job.id, job.kind, entry.attempt))
    for jid, kind, attempt in due:
        try:
            publisher(jid, kind)
            success = True
        except Exception:
            success = False
        with service.db() as s:
            entry = s.scalar(select(DispatchState).where(DispatchState.job_id == jid).with_for_update())
            job = s.get(Job, jid)
            if not entry or entry.attempt != attempt or entry.state != "sending":
                # A fast worker can defer an admitted broker delivery before
                # publisher() returns. Preserve its pending/short retry state.
                continue
            out = s.scalar(select(Outbox).where(Outbox.job_id == jid))
            if success:
                entry.state = "published"
                entry.last_error_code = ""
                entry.next_attempt_at = future(30)
                if out:
                    out.dispatched = True
            else:
                entry.state = "failed" if attempt >= 8 else "pending"
                entry.last_error_code = "BROKER_UNAVAILABLE"
                entry.next_attempt_at = future(min(60, 2**attempt))
                if out:
                    out.dispatched = False
                if entry.state == "failed" and job and job.state == "queued":
                    job.state = "failed"
                    job.stage = "DISPATCH_FAILED"
                    job.error_json = {
                        "code": "VKS_DEPENDENCY_UNAVAILABLE",
                        "message": "消息队列不可用，投递已达重试上限",
                        "retryable": True,
                    }
    return len(due)


def _processor_version():
    import hashlib
    from pathlib import Path

    from packages.analyzers.vks_ingest import ANALYZER_VERSION

    root = Path(__file__).resolve().parents[3]
    files = [
        root / "packages/analyzers/vks_ingest.py",
        root / "services/api/vks_api/knowledge.py",
        root / "services/api/vks_api/jobs.py",
        root / "packages/analyzers/java_spring.py",
        root / "packages/analyzers/async_topology.py",
        root / "packages/analyzers/ui_discovery.py",
        root / "packages/contracts/import_manifest.schema.json",
        root / "packages/contracts/binding_manifest.schema.json",
        root / "packages/analyzers/document_links.py",
        root / "packages/analyzers/cancellation.py",
        root / "packages/analyzers/syntax_cache.py",
        root / "packages/analyzers/incremental.py",
        root / "packages/analyzers/offline_prototype.py",
        root / "packages/analyzers/prototype_manifest.py",
        root / "packages/analyzers/business_points.py",
        root / "packages/contracts/prototype_manifest.schema.json",
        root / "packages/contracts/business_points_manifest.schema.json",
        root / "services/api/vks_api/source_cache.py",
    ]
    h = hashlib.sha256()
    for path in files:
        if path.exists():
            h.update(path.read_bytes())
    return f"{ANALYZER_VERSION}:{h.hexdigest()[:32]}"


def load_parse_checkpoint(service, job_id, input_hash):
    import hashlib
    import json

    from packages.analyzers.vks_ingest import ParsedBundle

    with service.db() as s:
        point = s.scalar(
            select(Checkpoint).where(
                Checkpoint.job_id == job_id,
                Checkpoint.stage == "PARSE",
                Checkpoint.input_hash == input_hash,
                Checkpoint.processor_version == _processor_version(),
            )
        )
        if not point:
            return None
        key = point.result_json["object_key"]
    try:
        raw = (service.data_dir / "objects" / key).read_bytes()
        if hashlib.sha256(raw).hexdigest() != key:
            return None
        payload = json.loads(raw)
        artifacts = []
        for item in payload["artifacts"]:
            artifact = dict(item)
            content_key = artifact.pop("content_ref")
            content = (service.data_dir / "objects" / content_key).read_bytes()
            if hashlib.sha256(content).hexdigest() != content_key:
                return None
            artifact["content"] = content
            artifacts.append(artifact)
        return ParsedBundle(
            manifest=payload["manifest"],
            sources=payload["sources"],
            artifacts=artifacts,
            entities=payload["entities"],
            evidence=payload["evidence"],
            relations=payload["relations"],
            issues=payload["issues"],
            analysis_metrics=payload.get("analysis_metrics", {}),
        )
    except (OSError, KeyError, ValueError, TypeError):
        return None


def save_parse_checkpoint(service, job_id, attempt, input_hash, parsed):
    import json

    from .service import JOB_SCOPES

    # JSON+content-addressed bytes only: a corrupted cache can never execute pickle.
    artifacts = []
    for item in parsed.artifacts:
        artifact = {k: v for k, v in item.items() if k != "content"}
        artifact["content_ref"] = service._put_object(item["content"])
        artifacts.append(artifact)
    payload = {
        name: getattr(parsed, name)
        for name in ("manifest", "sources", "entities", "evidence", "relations", "issues", "analysis_metrics")
    }
    payload["artifacts"] = artifacts
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    key = service._put_object(raw)
    with service.db() as s:
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.attempt != attempt or job.cancel_requested or job.state != "running":
            return
        service.require(
            s, current_actor(service, job.created_by, job.project_id, s, job=job), job.project_id, JOB_SCOPES[job.kind]
        )
        existing = s.scalar(
            select(Checkpoint).where(
                Checkpoint.job_id == job_id,
                Checkpoint.stage == "PARSE",
                Checkpoint.input_hash == input_hash,
                Checkpoint.processor_version == _processor_version(),
            )
        )
        if not existing:
            s.add(
                Checkpoint(
                    id=uid(),
                    job_id=job_id,
                    stage="PARSE",
                    input_hash=input_hash,
                    processor_version=_processor_version(),
                    result_json={"object_key": key},
                )
            )


def execute_job(service, jid):
    if service.personal_mode:
        from .reading import ReadingService

        with ReadingService(service).db() as session:
            row = session.execute(select(Job.kind, Job.project_id).where(Job.id == jid)).first()
        if row and row.kind in {"associate_all", "material_migrate"}:
            token = PERSONAL_TASK.set((service, row.project_id))
            try:
                return _execute_job(service, jid)
            finally:
                PERSONAL_TASK.reset(token)
    return _execute_job(service, jid)


def _execute_job(service, jid):
    from .knowledge import ImportService, persist_bundle
    from .models import Outbox, Project, Upload
    from .prototype_import import finish_batch, finish_preview, prepare_batch
    from .scenarios import ScenarioService
    from .service import JOB_SCOPES, DomainError, canonical, future, sha

    with service.db() as s:
        job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
        if not job or job.state != "queued":
            return
        if job.input_json.get("scan_pipeline"):
            from .scan_pipeline import claim

            if not claim(service, s, job):
                return
        elif job.kind in ("associate_all", "reassociation_preview"):
            from .association_limits import claim_full_association

            if not claim_full_association(service, s, job):
                return
        job.attempt += 1
        attempt = job.attempt
        try:
            principal = current_actor(service, job.created_by, job.project_id, s, job=job)
            service.require(s, principal, job.project_id, JOB_SCOPES[job.kind])
        except DomainError as error:
            job.state = "failed"
            job.stage = "AUTHORIZATION_FAILED"
            job.error_json = {"code": error.code, "message": error.message, "retryable": False}
            job.updated_at = now()
            return
        recovered = job.stage == "RECOVERED"
        job.state = "running"
        job.stage = "PARSE"
        job.lease_until = future(60)
        job.updated_at = now()
        payload = job.input_json
        pid = job.project_id
        kind = job.kind
        owner = job.created_by
        project = s.get(Project, pid)
        project_key = project.key
        upload = (
            s.scalar(
                select(Upload).where(
                    Upload.artifact_id == payload["artifact_ids"][0],
                    Upload.project_id == pid,
                    Upload.created_by == owner,
                    Upload.complete.is_(True),
                )
            )
            if kind == "import" and payload["kind"] == "bundle"
            else None
        )
        object_key = upload.sha256 if upload else None
        upload_size = upload.size_bytes if upload else None
        input_hash = sha(canonical(payload))

    def processing_cancelled():
        from .reading import ReadingService

        # Source inflation can checkpoint inside an export projection. This is
        # a current permission/cancellation read, so taking a second SQLite
        # writer transaction here would deadlock the outer projection.
        with ReadingService(service).db() as session:
            from types import SimpleNamespace

            from sqlalchemy.orm import defer

            row = session.execute(
                select(Job, Job.input_json["_authorization"])
                .options(defer(Job.input_json), defer(Job.result_json))
                .where(Job.id == jid)
            ).first()
            active, authorization = row if row else (None, None)
            if not active or active.attempt != attempt or active.state not in ("running", "cancelling"):
                raise DomainError("VKS_CANCELLED", "任务 attempt 已失效，停止解析", 409)
            if active.cancel_requested or (active.lease_until and active.lease_until <= now()):
                raise DomainError("VKS_CANCELLED", "任务取消或租约失效，停止解析", 409)
            principal = current_actor(
                service,
                owner,
                pid,
                session,
                job=SimpleNamespace(kind=active.kind, input_json={"_authorization": authorization}),
            )
            service.require(session, principal, pid, JOB_SCOPES[kind])
            if kind in ("index", "suggest_links"):
                service.require(session, principal, pid, "source:read")
        return False

    from packages.analyzers.cancellation import cancellation_scope
    from packages.analyzers.syntax_cache import cache_scope

    from .source_cache import SourceUnitCache

    try:
        with heartbeat(service, jid, attempt):
            with cancellation_scope(processing_cancelled), cache_scope(SourceUnitCache(service, jid, attempt)):
                if kind == "capture":
                    capture_output = ScenarioService(service).execute_capture(jid)
                if kind in ("index", "suggest_links"):
                    from .static_index import prepare

                    index_output = prepare(service, jid)
                if kind == "generate_documents":
                    from .ai_gateway import AiService

                    document_output = AiService(service).prepare_document(jid)
                if kind == "material_migrate":
                    from .materials import migrate_job

                    material_output = migrate_job(service, jid)
                if kind == "associate_all":
                    from .bulk_association import run_batch

                    association_output = run_batch(service, jid, preserve_processed=recovered)
                if kind == "reassociation_preview":
                    from .reassociation_batches import run_reassociation_batch

                    reassociation_output = run_reassociation_batch(service, jid)
                if kind == "export" and payload["format"] == "readonly_html":
                    from .readonly_export import prepare_job

                    readonly_output = prepare_job(service, jid, processing_cancelled)
                if kind == "knowledge_task":
                    from .knowledge_tasks import KnowledgeTaskService

                    task_output = KnowledgeTaskService(service).prepare(jid)
                if kind == "import_preview" or payload.get("_prototype_batch"):
                    prepared = prepare_batch(service, principal, pid, payload, preview=kind == "import_preview")
                elif kind == "import":
                    parsed = load_parse_checkpoint(service, jid, input_hash)
                    if parsed is None:
                        if payload["kind"] != "bundle":
                            parsed = ImportService(service).prepare(jid)
                        else:
                            if not object_key:
                                raise DomainError("VKS_INVALID_INPUT", "上传对象不可用")
                            try:
                                data = (service.data_dir / "objects" / object_key).read_bytes()
                            except OSError as e:
                                raise DomainError(
                                    "VKS_DEPENDENCY_UNAVAILABLE", "上传对象存储暂不可用", 503, retryable=True
                                ) from e
                            if len(data) != upload_size or sha(data) != object_key:
                                raise DomainError("VKS_INTEGRITY_ERROR", "上传对象完整性校验失败", 409)
                            parsed = service._parse(data, project_key)
                        save_parse_checkpoint(service, jid, attempt, input_hash, parsed)
            with service.db() as s:
                s.scalar(select(Project).where(Project.id == pid).with_for_update())
                job = s.scalar(
                    select(Job).where(Job.id == jid).with_for_update().execution_options(populate_existing=True)
                )
                if not job or job.attempt != attempt or job.state not in ("running", "cancelling"):
                    return
                if job.cancel_requested:
                    job.state = "cancelled"
                    job.stage = "CANCELLED"
                    job.lease_until = None
                    job.updated_at = now()
                    return
                if job.lease_until and job.lease_until <= now():
                    return
                principal = current_actor(service, owner, pid, s, job=job)
                service.require(s, principal, pid, JOB_SCOPES[kind])
                if kind == "import_preview":
                    result = finish_preview(service, s, principal, pid, payload, prepared)
                elif kind == "import" and payload.get("_prototype_batch"):
                    result = finish_batch(service, s, principal, pid, payload, prepared)
                elif kind == "import":
                    if receipt := payload.get("_prototype_preview"):
                        from .prototype import preview_fingerprint
                        from .prototype_edit import draft, version

                        analysis, _ = draft(s, service, principal, pid, payload["analysis_set_id"], "source:import")
                        version(analysis.selection_version, receipt["selection_version"])
                        if preview_fingerprint(parsed, payload, analysis.selection_version) != receipt["digest"]:
                            raise DomainError("VKS_VERSION_CONFLICT", "导入资料与预检摘要不一致，请重新预检", 409)
                    if payload.get("_source_sync"):
                        from .source_sync import SourceSyncService

                        result = SourceSyncService(service).persist(s, job, parsed, principal)
                    elif receipt:
                        result = persist_bundle(
                            service, s, pid, owner, parsed, analysis.id, actor=principal, reuse_imported_manifest=True
                        )
                    else:
                        result = service._commit_bundle(
                            s,
                            pid,
                            owner,
                            parsed,
                            payload.get("analysis_set_id") if payload["kind"] != "bundle" else None,
                            actor=principal,
                        )
                    if parsed.analysis_metrics:
                        result["analysis_metrics"] = parsed.analysis_metrics
                    if payload.get("_source_sync"):
                        from .source_sync import SourceSyncService

                        actor = current_actor(service, owner, pid, s, job=job)
                        result = SourceSyncService(service).finalize(s, job, actor, result)
                elif kind == "capture":
                    result = ScenarioService(service).finalize_capture(s, job, capture_output)
                elif kind == "generate_documents":
                    from .ai_gateway import AiService

                    result = AiService(service).finalize_document(s, job, document_output)
                elif kind == "knowledge_task":
                    from .knowledge_tasks import KnowledgeTaskService

                    result = KnowledgeTaskService(service).finalize(s, job, task_output)
                elif kind == "material_migrate":
                    result = material_output
                elif kind == "associate_all":
                    result = association_output
                elif kind == "reassociation_preview":
                    result = reassociation_output
                elif kind == "export":
                    result = (
                        service._perform_export(s, job, readonly_output)
                        if payload["format"] == "readonly_html"
                        else service._perform_export(s, job)
                    )
                elif kind == "detect_drift":
                    from .versions import VersionService

                    result = VersionService(service).detect_drift(
                        s, pid, payload["from_release_id"], payload["analysis_set_id"], owner
                    )
                elif kind in ("index", "suggest_links"):
                    from .static_index import finalize

                    result = finalize(service, s, job, index_output)
                else:
                    raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "平台模型尚未配置，不能生成文档", 503)
                job.result_json = result
                job.state = "succeeded"

                job.stage = "COMPLETE"
                job.progress = 100
                job.issue_count = len(result.get("issues", []))
                job.completeness = "partial" if job.issue_count else "complete"
                job.lease_until = None
                job.updated_at = now()
                out = s.scalar(select(Outbox).where(Outbox.job_id == jid))
                if out:
                    out.dispatched = True
                service.audit(s, owner, pid, "job.succeeded", jid)
    except Exception as e:
        from .knowledge_tasks import TaskPause

        if isinstance(e, TaskPause):
            with service.db() as s:
                job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
                if job and job.attempt == attempt and job.state in ("running", "cancelling"):
                    job.state = "cancelled" if job.cancel_requested else "paused"
                    job.stage = "CANCELLED" if job.cancel_requested else e.stage
                    job.lease_until = None
                    job.completeness = "partial"
                    job.input_json = dict(job.input_json, _pause_requested=False)
                    job.updated_at = now()
                    service.audit(s, owner, pid, "job.paused", jid, metadata={"stage": job.stage})
            return
        if not isinstance(e, DomainError):
            # Keep unexpected failures diagnosable without logging exception
            # messages, source/model payloads, credentials, or local variables.
            import logging
            import traceback
            from pathlib import Path

            frames = ">".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(e.__traceback__)
            )
            logging.getLogger(__name__).error("Job %s failed (%s) at %s", jid, type(e).__name__, frames)
        error = (
            e
            if isinstance(e, DomainError)
            else DomainError("VKS_INTERNAL_ERROR", "任务处理失败；可查看诊断后重试", 500)
        )
        with service.db() as s:
            job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
            if job and job.attempt == attempt and job.state in ("running", "cancelling"):
                retry = error.retryable and kind != "capture" and attempt < 3 and dispatch_mode() == "celery"
                job.state = "cancelled" if job.cancel_requested else "retry_wait" if retry else "failed"
                if kind == "knowledge_task":
                    history = [dict(row) for row in job.input_json.get("_stage_history", [])]
                    if history and history[-1]["state"] == "running":
                        history[-1]["state"] = job.state
                    job.input_json = dict(job.input_json, _stage_history=history)
                job.stage = job.state.upper()
                job.error_json = {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                }
                job.lease_until = future(min(60, 2**attempt)) if retry else None
                job.updated_at = now()
                service.audit(
                    s, owner, pid, "job.failed", jid, "failed", {"code": error.code, "retry_scheduled": retry}
                )
