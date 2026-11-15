"""Durable, immutable AI work produced by captured pages, never by browser waits."""

from copy import deepcopy
from dataclasses import replace

from sqlalchemy import JSON, ForeignKey, Index, String, case, func, select, text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Analysis, Base, Job, Outbox, now
from .service import DomainError

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"queued", "running", "retry_wait", "cancelling"}


class PrototypeScanTarget(Base):
    __tablename__ = "prototype_scan_target"
    run_id: Mapped[str] = mapped_column(ForeignKey("prototype_scan_run.id"), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64))
    item: Mapped[dict] = mapped_column(JSON)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = (
        Index("ix_scan_target_group", "run_id", "group_key"),
        Index("ix_scan_target_work", "state", "job_id"),
    )


def enabled(run):
    return bool(run.plan.get("pipeline"))


def source_version(run):
    return run.plan.get("source_selection_version", run.plan["selection_version"])


def set_parallelism(service, s, actor, run, requested):
    """Change scheduling on explicit continuation, without changing target snapshots."""
    if service.engine.dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(851392741)"))
    previous = run.plan.get("concurrency", 3)
    legacy = s.get(Job, run.batch_job_id) if run.batch_job_id else None
    live_legacy = legacy and legacy.state in ACTIVE
    lanes = requested if requested is not None else legacy.input_json.get("concurrency", 3) if live_legacy else previous
    if live_legacy and lanes != legacy.input_json.get("concurrency", 3):
        raise DomainError("VKS_STATE_CONFLICT", "请先停止关联阶段并等待在途请求退出，再调整并行路数", 409)
    if lanes != previous:
        running = s.scalar(
            select(Job.id)
            .where(
                Job.input_json["scan_run_id"].as_string() == run.id,
                Job.input_json["scan_pipeline"].as_boolean().is_(True),
                Job.state.in_(("running", "cancelling")),
            )
            .limit(1)
        )
        if running:
            raise DomainError("VKS_STATE_CONFLICT", "请等待在途关联退出后再调整并行路数", 409)
        run.plan = {**run.plan, "concurrency": lanes}
        service.audit(
            s, actor, run.project_id, "prototype.scan.parallelism", run.id, metadata={"from": previous, "to": lanes}
        )
    return lanes


def check_version(run, expected):
    from .prototype_edit import version

    version(source_version(run), expected)


def authorize(service, s, actor, run):
    """Deferred admissions retain the initiating request's permission ceiling."""
    scopes = run.plan.get("_pipeline_scopes")
    if not isinstance(scopes, list) or any(not isinstance(value, str) for value in scopes):
        raise DomainError("VKS_UNAUTHENTICATED", "流水线授权快照无效，请明确继续以重新授权", 401)
    return replace(
        actor,
        scopes=frozenset(
            scope for scope in actor.scopes if scope in scopes and service._can(s, actor, run.project_id, scope)
        ),
    )


def _rows(s, run_id, group=None):
    query = select(PrototypeScanTarget).where(PrototypeScanTarget.run_id == run_id)
    if group is not None:
        query = query.where(PrototypeScanTarget.group_key == group)
    return list(s.scalars(query.order_by(PrototypeScanTarget.created_at, PrototypeScanTarget.target_id)))


def summary(s, run):
    # Polling must not deserialize every target, model input and explanation.
    # Detailed outcomes are available separately through the paged job API.
    target = PrototypeScanTarget
    legacy_scope = target.job_id == run.batch_job_id if run.batch_job_id else False
    legacy_conditions = {
        "targets": target.target_id.is_not(None),
        "completed": target.state == "complete",
        "failed": target.state == "failed",
        "saved": target.outcome["saved"].as_boolean().is_(True),
        "queued": (target.state == "queued") & Job.state.in_(("queued", "retry_wait")),
        "running": (target.state == "queued") & Job.state.in_(("running", "cancelling")),
        "cancelled": target.state == "cancelled",
    }

    def count(condition, name):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(name)

    result = dict(
        s.execute(
            select(
                func.count(target.target_id).label("targets"),
                count(target.state == "complete", "completed"),
                count(target.state == "failed", "failed"),
                count(target.outcome["saved"].as_boolean().is_(True), "saved"),
                count(target.state == "waiting", "waiting"),
                count((target.state == "waiting") & target.job_id.is_(None), "pending_admission"),
                count((target.state == "queued") & Job.state.in_(("queued", "retry_wait")), "queued"),
                count((target.state == "queued") & Job.state.in_(("running", "cancelling")), "running"),
                count(target.state == "cancelled", "cancelled"),
                func.count(func.distinct(Job.id)).label("jobs"),
                *(count(condition & legacy_scope, "legacy_" + name) for name, condition in legacy_conditions.items()),
            )
            .select_from(target)
            .outerjoin(Job, Job.id == target.job_id)
            .where(target.run_id == run.id)
        )
        .one()
        ._mapping
    )
    legacy_counts = {name: result.pop("legacy_" + name) for name in legacy_conditions}
    if run.batch_job_id and legacy_counts["targets"]:
        # The local dispatcher may still be inside this long legacy batch.
        # Read its durable counters directly; marker reconciliation need not wait.
        legacy = s.execute(
            select(
                Job.state,
                Job.input_json["concurrency"].as_integer().label("lanes"),
                Job.result_json["processed"].as_integer().label("processed"),
                Job.result_json["pending"].as_integer().label("pending"),
                Job.result_json["saved"].as_integer().label("saved"),
                Job.result_json["total"].as_integer().label("total"),
            ).where(Job.id == run.batch_job_id)
        ).first()
        if legacy and (legacy.state in ACTIVE or legacy.total is None or legacy.total == legacy_counts["targets"]):
            total = legacy_counts["targets"]
            processed = min(
                total,
                legacy.processed
                if legacy.processed is not None
                else legacy_counts["completed"] + legacy_counts["failed"],
            )
            failed = min(processed, legacy.pending if legacy.pending is not None else legacy_counts["failed"])
            remaining = total - processed
            running = min(remaining, legacy.lanes or 3) if legacy.state in ("running", "cancelling") else 0
            adjusted = {
                "targets": total,
                "completed": processed - failed,
                "failed": failed,
                "saved": legacy.saved if legacy.saved is not None else legacy_counts["saved"],
                "running": running,
                "queued": remaining - running if legacy.state in ACTIVE else 0,
                "cancelled": remaining if legacy.state == "cancelled" else 0,
            }
            for name, value in adjusted.items():
                result[name] += value - legacy_counts[name]
    result["processed"] = result["completed"] + result["failed"]
    error = s.scalar(
        select(target.outcome["message"].as_string())
        .where(target.run_id == run.id, target.state == "failed")
        .order_by(target.created_at, target.target_id)
        .limit(1)
    )
    result["error"] = error or ("模型任务暂未完成" if result["failed"] else "")
    return result


def _sync(s, run, group):
    from .bulk_association import PROCESSOR
    from .jobs import Checkpoint

    rows = _rows(s, run.id, group)
    ids = {r.job_id for r in rows if r.job_id}
    jobs = {j.id: j for j in s.scalars(select(Job).where(Job.id.in_(ids)))} if ids else {}
    for row in rows:
        job = jobs.get(row.job_id)
        if row.state != "queued" or not job:
            continue
        checkpoint = s.scalar(
            select(Checkpoint).where(
                Checkpoint.job_id == job.id,
                Checkpoint.stage == "ASSOCIATE",
                Checkpoint.processor_version == PROCESSOR,
                Checkpoint.input_hash == row.target_id,
            )
        )
        if (
            checkpoint
            and job.state in ACTIVE
            and checkpoint.result_json.get("attempt", 1) < job.result_json.get("processing_epoch", 1)
            and checkpoint.result_json.get("status") not in {"linked", "reused", "manual", "excluded", "skipped"}
        ):
            checkpoint = None
        if not checkpoint and job.state not in TERMINAL:
            continue
        outcome = (
            deepcopy(checkpoint.result_json)
            if checkpoint
            else {
                "status": "cancelled" if job.state == "cancelled" else "failed",
                "saved": False,
                "message": job.error_json.get("message", "模型任务尚未完成，可独立重试"),
            }
        )
        row.outcome = outcome
        row.state = (
            "complete"
            if outcome["status"] in {"linked", "reused", "manual", "excluded", "skipped"}
            else "cancelled"
            if outcome["status"] == "cancelled"
            else "failed"
        )
        row.updated_at = now()
    return rows, jobs


def adopt_legacy(s, run):
    """Keep an already running fixed batch; never enqueue its targets twice."""
    from .bulk_association import target_id

    if not run.batch_job_id or run.plan.get("_adopted_batch") == run.batch_job_id:
        return
    job = s.get(Job, run.batch_job_id)
    if (
        not job
        or job.kind != "associate_all"
        or job.project_id != run.project_id
        or job.created_by != run.created_by
        or job.input_json.get("analysis_set_id") != run.analysis_set_id
        or job.input_json.get("composition") != run.composition
    ):
        raise DomainError("VKS_INTEGRITY_ERROR", "原关联任务与扫描范围不一致", 409)
    existing = set(s.scalars(select(PrototypeScanTarget.target_id).where(PrototypeScanTarget.run_id == run.id)))
    for item in job.input_json["items"]:
        if item["id"] != target_id(item["package_id"], item["target"]):
            raise DomainError("VKS_INTEGRITY_ERROR", "原关联任务的目标身份不一致", 409)
        if item["id"] not in existing:
            s.add(
                PrototypeScanTarget(
                    run_id=run.id,
                    target_id=item["id"],
                    group_key=item.get("operation_group", item["id"]),
                    item=deepcopy(item),
                    job_id=job.id,
                    state="queued",
                )
            )
    run.plan = {**run.plan, "_adopted_batch": job.id}
    s.flush()


def _schedule(service, s, actor, run, group, coverage=None):
    actor = authorize(service, s, actor, run)
    rows, jobs = _sync(s, run, group)
    pending = [row for row in rows if row.state == "waiting"]
    if not pending:
        return
    active = next((job for job in jobs.values() if job.state in ACTIVE), None)
    if active:
        for row in pending:
            row.job_id = active.id
        return
    canonical_row = next(
        (row for row in rows if row.state == "complete" and row.outcome.get("status") in {"linked", "reused"}), None
    )
    if canonical_row is None and any(row.state == "failed" for row in rows):
        for row in pending:
            row.state = "failed"
            row.outcome = {
                "status": "insufficient",
                "saved": False,
                "message": "同操作的依据或模型任务尚未完成，可独立重试 AI",
            }
        return
    items = [deepcopy(row.item) for row in pending]
    needs_model = any(
        item["state"] == "pending"
        and (run.mode != "changed" or item["existing"])
        or run.mode == "all"
        and item["state"] == "linked"
        for item in items
    )
    payload = {
        "analysis_set_id": run.analysis_set_id,
        "composition": run.composition,
        "mode": run.mode,
        "items": items,
        "groups": [[item["id"] for item in items]],
        "coverage": coverage or {"limitations": []},
        "processor": "source-controls-v2",
        "concurrency": 1,
        "needs_model": needs_model,
        "scan_pipeline": True,
        "scan_run_id": run.id,
        "pipeline_concurrency": run.plan.get("concurrency", 3),
        "pipeline_group_key": group,
    }
    if canonical_row:
        payload["reuse_from_item"] = deepcopy(canonical_row.item)
    job = service._new_job(s, actor, run.project_id, "associate_all", payload)
    for row in pending:
        row.job_id, row.state, row.outcome, row.updated_at = job.id, "queued", {}, now()
    run.updated_at = now()


def produce(service, s, actor, run, *, targets=None, admission_limit=None):
    """Called in the page transaction; source inventory and child jobs commit together."""
    from .bulk_association import BulkAssociationService, target_id
    from .prototype_scan import page_key

    if not enabled(run) or run.state == "cancelled":
        return
    adopt_legacy(s, run)
    wanted = {target_id(package, target) for package, target in targets} if targets is not None else None
    _, digest, items, _groups, coverage = BulkAssociationService(service).scan(
        s, actor, run.project_id, run.analysis_set_id, target_keys=wanted
    )
    if digest != run.composition:
        raise DomainError("VKS_VERSION_CONFLICT", "资料已变化，不能生成旧版本关联任务", 412)
    captured = [
        (package["package_id"], page)
        for package in run.plan["packages"]
        for page in package["pages"]
        if run.completed.get(page_key(package["package_id"], page), {}).get("status") == "captured"
    ]
    query = select(PrototypeScanTarget.target_id).where(PrototypeScanTarget.run_id == run.id)
    if wanted is not None:
        query = query.where(PrototypeScanTarget.target_id.in_(wanted))
    existing = set(s.scalars(query))
    changed = set()
    for item in items:
        if item["id"] in existing or wanted is not None and item["id"] not in wanted:
            continue
        if not any(
            item["package_id"] == package
            and item["target"]["route"] == page["route"]
            and item["target"]["frame_path"][: len(page["frame_path"])] == page["frame_path"]
            for package, page in captured
        ):
            continue
        group = item.get("operation_group", item["id"])
        s.add(PrototypeScanTarget(run_id=run.id, target_id=item["id"], group_key=group, item=deepcopy(item)))
        changed.add(group)
    s.flush()
    for group in sorted(changed)[:admission_limit]:
        _schedule(service, s, actor, run, group, coverage)
    s.flush()


def reconcile(service):
    """Bounded terminal-job drain; no network and no scanning waits."""
    from .bulk_association import PROCESSOR
    from .jobs import Checkpoint, current_actor
    from .prototype_scan import PrototypeScanRun, PrototypeScanService

    with service.db() as s:
        pairs = list(
            s.execute(
                select(PrototypeScanTarget.run_id, PrototypeScanTarget.group_key)
                .outerjoin(Job, Job.id == PrototypeScanTarget.job_id)
                .where(
                    PrototypeScanTarget.state.in_(("queued", "waiting")),
                    (Job.state.in_(TERMINAL))
                    | (PrototypeScanTarget.job_id.is_(None))
                    | (
                        Job.state.in_(("running", "cancelling"))
                        & select(Checkpoint.id)
                        .where(
                            Checkpoint.job_id == PrototypeScanTarget.job_id,
                            Checkpoint.input_hash == PrototypeScanTarget.target_id,
                            Checkpoint.stage == "ASSOCIATE",
                            Checkpoint.processor_version == PROCESSOR,
                        )
                        .exists()
                    ),
                )
                .distinct()
                .limit(8)
            )
        )
    for run_id, group in pairs:
        with service.db() as s:
            run = s.get(PrototypeScanRun, run_id)
            if not run:
                continue
            try:
                actor = authorize(service, s, current_actor(service, run.created_by, run.project_id, s), run)
                PrototypeScanService(service)._scope(s, actor, run.project_id, run)
                s.refresh(run, with_for_update=True)
                rows = _rows(s, run_id, group)
                job = s.get(Job, next((row.job_id for row in rows if row.job_id), ""))
                actor = current_actor(service, run.created_by, run.project_id, s, job=job)
                if run.state == "cancelled":
                    raise DomainError("VKS_CANCELLED", "扫描关联已取消", 409)
                _schedule(service, s, actor, run, group)
            except DomainError as error:
                rows = _rows(s, run_id, group)
                for row in rows:
                    if row.state in ("queued", "waiting"):
                        row.state = "cancelled" if error.code == "VKS_CANCELLED" else "failed"
                        row.outcome = {"status": row.state, "saved": False, "message": "资料、权限或扫描状态已变化"}


def retry(service, s, actor, run):
    if run.state == "cancelled":
        raise DomainError("VKS_STATE_CONFLICT", "已取消的流水线不能重试，请新建任务", 409)
    groups = set(
        s.scalars(
            select(PrototypeScanTarget.group_key)
            .join(Job, Job.id == PrototypeScanTarget.job_id)
            .where(PrototypeScanTarget.run_id == run.id, PrototypeScanTarget.state == "queued", Job.state.in_(TERMINAL))
            .distinct()
        )
    )
    for group in groups:
        _sync(s, run, group)
    groups = set()
    for row in s.scalars(
        select(PrototypeScanTarget).where(
            PrototypeScanTarget.run_id == run.id, PrototypeScanTarget.state.in_(("failed", "cancelled"))
        )
    ):
        row.state, row.job_id, row.outcome = "waiting", None, {}
        groups.add(row.group_key)
    for group in sorted(groups)[:8]:
        _schedule(service, s, actor, run, group)


def cancel(s, run):
    ids = {row.job_id for row in _rows(s, run.id) if row.job_id}
    for job in s.scalars(select(Job).where(Job.id.in_(ids)).order_by(Job.id).with_for_update()) if ids else []:
        if job.state in ("queued", "retry_wait", "paused"):
            job.state, job.stage, job.cancel_requested = "cancelled", "CANCELLED", True
        elif job.state == "running":
            job.state, job.cancel_requested = "cancelling", True
    for row in _rows(s, run.id):
        if row.state in ("queued", "waiting"):
            row.state = "cancelled"
            row.outcome = {"status": "cancelled", "saved": False, "message": "用户已取消流水线"}


def claim(service, s, job):
    """Persistent global pipeline cap, shared by local and distributed workers."""
    from .bulk_association import composition
    from .materials import StudioWorkspace
    from .prototype_scan import PrototypeScanRun

    if service.engine.dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(851392741)"))
    run = s.get(PrototypeScanRun, job.input_json["scan_run_id"])
    analysis = s.get(Analysis, job.input_json["analysis_set_id"])
    workspace = s.get(StudioWorkspace, job.project_id)
    if not run or run.state == "cancelled":
        job.state, job.stage, job.cancel_requested = "cancelled", "CANCELLED", True
        return False
    if (
        not enabled(run)
        or run.project_id != job.project_id
        or run.analysis_set_id != job.input_json["analysis_set_id"]
        or run.created_by != job.created_by
        or run.composition != job.input_json["composition"]
        or not analysis
        or analysis.state == "frozen"
        or workspace
        and workspace.analysis_set_id != analysis.id
        or composition(s, job.project_id, analysis.id) != run.composition
    ):
        job.state, job.stage = "failed", "SCOPE_CHANGED"
        job.error_json = {"code": "VKS_VERSION_CONFLICT", "message": "流水线资料版本已变化", "retryable": False}
        return False
    active = list(
        s.execute(
            select(
                Job.id,
                Job.input_json["scan_pipeline"].as_boolean(),
                Job.input_json["concurrency"].as_integer(),
                Job.input_json["scan_run_id"].as_string(),
            ).where(
                Job.kind == "associate_all",
                Job.state.in_(("running", "cancelling")),
                Job.lease_until > now(),
            )
        )
    )
    total_used, run_used = 0, 0
    for jid, pipeline, lanes, run_id in active:
        used = 1 if pipeline else min(4, max(1, lanes or 3))
        total_used += used
        if run_id == run.id or jid == run.batch_job_id:
            run_used += used
    limit = min(4, max(1, run.plan.get("concurrency", 3)))
    if total_used >= 4 or run_used >= limit:
        out = s.scalar(select(Outbox).where(Outbox.job_id == job.id))
        if out:
            out.dispatched = False
        from .jobs import DispatchState
        from .service import future

        dispatch = s.scalar(select(DispatchState).where(DispatchState.job_id == job.id))
        if dispatch:
            dispatch.state, dispatch.next_attempt_at = "pending", future(1)
        return False
    return True
