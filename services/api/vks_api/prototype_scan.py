"""Durable browser-only scan coordination; imported scripts never run here."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, ClassVar, Literal

from fastapi import Depends, Header, Request
from pydantic import Field
from sqlalchemy import JSON, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .materials import MaterialService, StudioWorkspace
from .models import Analysis, Base, Job, now, uid
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref, version
from .service import DomainError, canonical, sha


class PrototypeScanRun(Base):
    __tablename__ = "prototype_scan_run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(96), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    composition: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="running")
    client_id: Mapped[str] = mapped_column(String(96))
    lease_until: Mapped[str] = mapped_column(String(40))
    plan: Mapped[dict] = mapped_column(JSON)
    completed: Mapped[dict] = mapped_column(JSON, default=dict)
    current: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    batch_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    __mapper_args__: ClassVar[dict] = {"version_id_col": row_version}


class ScanClient(Input):
    client_id: str = Field(min_length=8, max_length=96, pattern=r"^[A-Za-z0-9._:-]+$")
    pipeline: bool = Field(default=False, strict=True)


class ScanInput(ScanClient):
    analysis_set_id: Ref
    mode: Literal["missing", "changed", "all"] = "missing"
    concurrency: int = Field(default=3, ge=1, le=4, strict=True)
    pipeline: bool = Field(default=True, strict=True)


class ScanPage(Input):
    package_id: Ref
    route: str = Field(min_length=1, max_length=1000)
    frame_path: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(max_length=8)


class ScanHeartbeat(ScanClient):
    current: ScanPage | None = None


def page_key(package, page):
    return sha(canonical([package, page["route"], page["frame_path"]]))


def lease():
    return (datetime.now(UTC) + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")


class PrototypeScanService:
    def __init__(self, service):
        self.service = service

    def _get(self, s, actor, pid, run_id, *, lock=False):
        self.service.require(s, actor, pid)
        query = select(PrototypeScanRun).where(
            PrototypeScanRun.id == run_id, PrototypeScanRun.project_id == pid, PrototypeScanRun.created_by == actor.id
        )
        row = s.scalar(query.with_for_update() if lock else query)
        if not row or row.project_id != pid or row.created_by != actor.id:
            raise DomainError("VKS_NOT_FOUND", "未找到预扫描任务", 404)
        return row

    def _manifest(self, actor, pid, aid, *, session=None):
        from .bulk_association import BulkAssociationService

        return BulkAssociationService(self.service).inventory_status(actor, pid, aid, session=session)

    def _scope(self, s, actor, pid, row):
        self.service.require(s, actor, pid, "knowledge:edit")
        analysis = MaterialService(self.service)._draft(s, actor, pid, row.analysis_set_id)
        workspace = s.get(StudioWorkspace, pid)
        if workspace and workspace.analysis_set_id != analysis.id:
            raise DomainError("VKS_VERSION_CONFLICT", "项目工作区已切换，旧扫描不能继续", 412)
        current = self._manifest(actor, pid, analysis.id, session=s)
        visible = {(p["package_id"], p["sha256"]) for p in current["packages"]}
        wanted = {(p["package_id"], p["sha256"]) for p in row.plan["packages"]}
        if current["composition"] != row.composition or not wanted <= visible:
            raise DomainError("VKS_VERSION_CONFLICT", "资料版本或权限已变化，旧扫描不能继续", 412)
        return analysis, current

    def fence(self, s, actor, pid, run_id, client_id):
        row = self._get(s, actor, pid, run_id, lock=True)
        self._scope(s, actor, pid, row)
        if row.state != "running" or row.client_id != client_id or row.lease_until <= now():
            raise DomainError("VKS_STATE_CONFLICT", "预扫描已停止、已被接管或连接已中断，请明确继续", 409)
        return row

    def _restore_legacy_attempts(self, s, actor, row):
        """Recover only failed history removed by the early preview's resume."""
        if "_round_pending" in row.plan:
            return
        from .bulk_association import PrototypeInventoryPage

        packages = {p["package_id"]: p for p in row.plan["packages"]}
        planned = {page_key(p["package_id"], t) for p in packages.values() for t in p["pages"]}
        completed, restored = dict(row.completed), 0
        records = s.scalars(
            select(PrototypeInventoryPage).where(
                PrototypeInventoryPage.project_id == row.project_id,
                PrototypeInventoryPage.analysis_set_id == row.analysis_set_id,
                PrototypeInventoryPage.composition == row.composition,
                PrototypeInventoryPage.observed_by == row.created_by,
                PrototypeInventoryPage.updated_at >= row.created_at,
            )
        )
        for item in records:
            package = packages.get(item.package_id)
            key = page_key(item.package_id, {"route": item.route, "frame_path": item.frame_path})
            if (
                package
                and package["sha256"] == item.artifact_sha256
                and key in planned
                and key not in completed
                and item.report.get("status") == "failed"
            ):
                completed[key] = {"status": "failed", "controls": 0, "cached": False}
                restored += 1
        row.completed = completed
        captured = {k for k, v in completed.items() if v["status"] == "captured"}
        row.plan = {**row.plan, "_round_pending": sorted(planned - captured)}
        if restored:
            self.service.audit(
                s,
                actor,
                row.project_id,
                "prototype.scan.history.restore",
                row.id,
                metadata={"recovered_count": restored, "source": "same_source_failed_inventory"},
            )

    def dto(self, s, actor, pid, row):
        from .scan_pipeline import enabled, source_version, summary

        valid, current = True, None
        try:
            _, current = self._scope(s, actor, pid, row)
        except DomainError:
            valid = False
        state = row.state if valid else "invalidated"
        if (
            state == "completed"
            and not row.batch_job_id
            and any(v["status"] == "failed" for v in row.completed.values())
        ):
            state = "partial"
        if state == "running" and row.lease_until <= now():
            state = "interrupted"
        remaining = []
        if valid:
            for package in row.plan["packages"]:
                pages = [
                    deepcopy(p)
                    for p in package["pages"]
                    if row.completed.get(page_key(package["package_id"], p), {}).get("status") != "captured"
                ]
                if pages:
                    remaining.append({**deepcopy(package), "pages": pages})
        results = list(row.completed.values()) if valid else []
        manifest = {
            "analysis_set_id": row.analysis_set_id,
            "selection_version": source_version(row) if valid else 0,
            "source_selection_version": source_version(row) if valid else 0,
            "composition": row.composition,
            "editable": valid,
            "packages": remaining,
            "pages_total": sum(len(p["pages"]) for p in remaining),
            "concurrency": row.plan.get("concurrency", 3),
        }
        return {
            "id": row.id,
            "project_id": row.project_id,
            "analysis_set_id": row.analysis_set_id,
            "mode": row.mode,
            "concurrency": manifest["concurrency"],
            "state": state,
            "current_scope": valid,
            "selection_version": current["selection_version"] if current else 0,
            "source_selection_version": manifest["selection_version"],
            "pipeline": enabled(row),
            "ai": summary(s, row) if valid and enabled(row) else None,
            "composition": row.composition,
            "pages_total": sum(len(p["pages"]) for p in row.plan["packages"]) if valid else 0,
            "pages_completed": sum(r["status"] == "captured" for r in results),
            "pages_attempted": len(results),
            "pages_failed": sum(r["status"] == "failed" for r in results),
            "controls": sum(r.get("controls", 0) for r in results),
            "current": row.current if valid else None,
            "batch_job_id": row.batch_job_id,
            "client_id": row.client_id,
            "lease_until": row.lease_until,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "row_version": row.row_version,
            "manifest": manifest,
            "diagnostics_restricted": not valid,
        }

    def status(self, actor, pid, aid):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            # Author and requested analysis are filtered before any stored label/count.
            row = s.scalar(
                select(PrototypeScanRun)
                .where(
                    PrototypeScanRun.project_id == pid,
                    PrototypeScanRun.analysis_set_id == aid,
                    PrototypeScanRun.created_by == actor.id,
                )
                .order_by(PrototypeScanRun.created_at.desc())
                .limit(1)
            )
            if not row:
                current = self._manifest(actor, pid, aid)
                return {
                    "run": None,
                    "stored_progress": {
                        "pages_total": current["pages_total"],
                        "pages_captured": current["pages_captured"],
                        "pages_failed": current["pages_failed"],
                        "controls": sum(
                            page.get("current_controls", max(0, page.get("current_count", 1) - 1))
                            for package in current["packages"]
                            for page in package["pages"]
                            if page.get("status") == "captured"
                        ),
                    },
                }
            return {"run": self.dto(s, actor, pid, row)}

    def start(self, actor, pid, form, expected, key):
        from .scan_pipeline import produce

        current = self._manifest(actor, pid, form.analysis_set_id)
        with self.service.db() as s:
            analysis = MaterialService(self.service)._draft(s, actor, pid, form.analysis_set_id)

            def create():
                version(analysis.selection_version, expected)
                if current["selection_version"] != analysis.selection_version:
                    raise DomainError("VKS_VERSION_CONFLICT", "资料版本已变化，请重新扫描", 412)
                workspace = s.get(StudioWorkspace, pid)
                if workspace and workspace.analysis_set_id != analysis.id:
                    raise DomainError("VKS_VERSION_CONFLICT", "项目工作区已切换，请刷新", 412)
                active = s.scalar(
                    select(PrototypeScanRun)
                    .where(
                        PrototypeScanRun.project_id == pid,
                        PrototypeScanRun.analysis_set_id == analysis.id,
                        PrototypeScanRun.created_by == actor.id,
                        PrototypeScanRun.composition == current["composition"],
                        PrototypeScanRun.state == "running",
                        PrototypeScanRun.batch_job_id.is_(None),
                    )
                    .order_by(PrototypeScanRun.created_at.desc())
                    .with_for_update()
                )
                if active:
                    if active.mode != form.mode:
                        raise DomainError("VKS_STATE_CONFLICT", "已有其他模式的扫描，请先继续或停止该扫描", 409)
                    active.client_id, active.lease_until, active.updated_at = form.client_id, lease(), now()
                    active.plan = {
                        **active.plan,
                        "pipeline": bool(active.plan.get("pipeline") or form.pipeline),
                        "_pipeline_scopes": sorted(
                            scope for scope in actor.scopes if self.service._can(s, actor, pid, scope)
                        ),
                    }
                    produce(self.service, s, actor, active, admission_limit=8)
                    return {"id": active.id}
                completed = {}
                if form.mode != "all":
                    for package in current["packages"]:
                        for page in package["pages"]:
                            if page.get("status") == "captured":
                                completed[page_key(package["package_id"], page)] = {
                                    "status": "captured",
                                    "controls": page.get("current_controls", max(0, page.get("current_count", 1) - 1)),
                                    "cached": True,
                                }
                plan = deepcopy(current)
                plan["concurrency"] = form.concurrency
                plan["pipeline"] = form.pipeline
                plan["_pipeline_scopes"] = sorted(
                    scope for scope in actor.scopes if self.service._can(s, actor, pid, scope)
                )
                plan["source_selection_version"] = analysis.selection_version
                plan["_round_pending"] = [
                    page_key(p["package_id"], page)
                    for p in plan["packages"]
                    for page in p["pages"]
                    if page_key(p["package_id"], page) not in completed
                ]
                row = PrototypeScanRun(
                    project_id=pid,
                    analysis_set_id=analysis.id,
                    created_by=actor.id,
                    mode=form.mode,
                    composition=current["composition"],
                    client_id=form.client_id,
                    lease_until=lease(),
                    plan=plan,
                    completed=completed,
                    current=None,
                )
                if len(completed) == sum(len(p["pages"]) for p in current["packages"]):
                    row.state = "completed"
                s.add(row)
                s.flush()
                produce(self.service, s, actor, row, admission_limit=8)
                self.service.audit(s, actor, pid, "prototype.scan.start", row.id)
                return {"id": row.id}

            result = self.service.idem(
                s, actor, pid, "prototype.scan.start", key, {**form.model_dump(), "expected": expected}, create
            )
            return self.dto(s, actor, pid, self._get(s, actor, pid, result["id"]))

    def resume(self, actor, pid, run_id, form, expected, key):
        from .scan_pipeline import check_version, produce

        with self.service.db() as s:
            row = self._get(s, actor, pid, run_id)
            self.service.require(s, actor, pid, "knowledge:edit")
            # Match the same analysis->run lock order used by inventory writes.
            MaterialService(self.service)._draft(s, actor, pid, row.analysis_set_id)
            s.refresh(row, with_for_update=True)
            self._scope(s, actor, pid, row)

            def apply():
                check_version(row, expected)
                other = s.scalar(
                    select(PrototypeScanRun.id)
                    .where(
                        PrototypeScanRun.id != row.id,
                        PrototypeScanRun.project_id == pid,
                        PrototypeScanRun.analysis_set_id == row.analysis_set_id,
                        PrototypeScanRun.created_by == actor.id,
                        PrototypeScanRun.composition == row.composition,
                        PrototypeScanRun.state == "running",
                        PrototypeScanRun.lease_until > now(),
                    )
                    .limit(1)
                )
                if other:
                    raise DomainError("VKS_STATE_CONFLICT", "另有正在运行的扫描，请先停止或继续当前扫描", 409)
                if row.state == "cancelled" or row.batch_job_id:
                    raise DomainError("VKS_STATE_CONFLICT", "已取消或已启动关联的扫描不能继续，请新建任务", 409)
                self._restore_legacy_attempts(s, actor, row)
                total = sum(len(p["pages"]) for p in row.plan["packages"])
                captured = {k for k, v in row.completed.items() if v["status"] == "captured"}
                row.plan = {
                    **row.plan,
                    "pipeline": bool(row.plan.get("pipeline") or form.pipeline),
                    "_pipeline_scopes": sorted(
                        scope for scope in actor.scopes if self.service._can(s, actor, pid, scope)
                    ),
                    "source_selection_version": row.plan.get("source_selection_version", row.plan["selection_version"]),
                    "concurrency": row.plan.get("concurrency", 3),
                    "_round_pending": [
                        page_key(p["package_id"], page)
                        for p in row.plan["packages"]
                        for page in p["pages"]
                        if page_key(p["package_id"], page) not in captured
                    ],
                }
                row.state = "completed" if len(captured) == total else "running"
                row.client_id, row.lease_until, row.updated_at, row.current = form.client_id, lease(), now(), None
                produce(self.service, s, actor, row, admission_limit=8)
                return {"id": row.id}

            self.service.idem(
                s,
                actor,
                pid,
                "prototype.scan.resume",
                key,
                {"id": run_id, **form.model_dump(), "expected": expected},
                apply,
            )
            return self.dto(s, actor, pid, row)

    def heartbeat(self, actor, pid, run_id, form):
        with self.service.db() as s:
            row = self._get(s, actor, pid, run_id)
            MaterialService(self.service)._draft(s, actor, pid, row.analysis_set_id)
            s.refresh(row, with_for_update=True)
            if row.state in ("completed", "partial") and row.client_id == form.client_id:
                self._scope(s, actor, pid, row)
                return self.dto(s, actor, pid, row)
            row = self.fence(s, actor, pid, run_id, form.client_id)
            if form.current:
                page = form.current.model_dump()
                if not any(
                    page_key(p["package_id"], t) == page_key(page["package_id"], page)
                    for p in row.plan["packages"]
                    for t in p["pages"]
                ):
                    raise DomainError("VKS_INVALID_INPUT", "当前页不属于预扫描计划", 422)
                row.current = page
            row.lease_until, row.updated_at = lease(), now()
            return self.dto(s, actor, pid, row)

    def finish_page(self, row, form):
        page = {"route": form.route, "frame_path": form.frame_path}
        key = page_key(form.package_id, page)
        if not any(page_key(p["package_id"], t) == key for p in row.plan["packages"] for t in p["pages"]):
            raise DomainError("VKS_INVALID_INPUT", "当前页不属于预扫描计划", 422)
        planned = {page_key(p["package_id"], t) for p in row.plan["packages"] for t in p["pages"]}
        pending = set(row.plan.get("_round_pending", planned - set(row.completed)))
        pending.discard(key)
        row.plan = {**row.plan, "_round_pending": sorted(pending)}
        row.completed = {
            **row.completed,
            key: {"status": form.status, "controls": sum(t.selector != "body" for t in form.targets), "cached": False},
        }
        row.current = {"package_id": form.package_id, **page}
        row.lease_until, row.updated_at = lease(), now()
        if not pending:
            row.state = "completed" if all(v["status"] == "captured" for v in row.completed.values()) else "partial"
            row.current = None

    def pause(self, actor, pid, run_id, client_id):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            row = self._get(s, actor, pid, run_id)
            MaterialService(self.service)._draft(s, actor, pid, row.analysis_set_id)
            s.refresh(row, with_for_update=True)
            if row.client_id != client_id or row.batch_job_id:
                raise DomainError("VKS_STATE_CONFLICT", "任务已被接管或已进入模型关联，请刷新任务状态", 409)
            self._scope(s, actor, pid, row)
            if row.state == "interrupted":
                self._restore_legacy_attempts(s, actor, row)
                return self.dto(s, actor, pid, row)
            if row.state != "running":
                raise DomainError("VKS_STATE_CONFLICT", "仅运行中或已中断的预扫描可以暂停", 409)
            self._restore_legacy_attempts(s, actor, row)
            row.state, row.lease_until, row.updated_at = "interrupted", now(), now()
            self.service.audit(s, actor, pid, "prototype.scan.pause", row.id)
            return self.dto(s, actor, pid, row)

    def cancel(self, actor, pid, run_id, client_id):
        from .scan_pipeline import cancel

        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            row = self._get(s, actor, pid, run_id)
            s.scalar(
                select(Analysis).where(Analysis.id == row.analysis_set_id, Analysis.project_id == pid).with_for_update()
            )
            s.refresh(row, with_for_update=True)
            if row.client_id != client_id:
                raise DomainError("VKS_STATE_CONFLICT", "扫描已被新的客户端接管，旧请求不能停止它", 409)
            row.state, row.current, row.updated_at, row.lease_until = "cancelled", None, now(), now()
            cancel(s, row)
            if row.batch_job_id and (job := s.get(Job, row.batch_job_id)):
                s.refresh(job, with_for_update=True)
                if job.state in ("queued", "retry_wait", "paused"):
                    job.state, job.stage, job.cancel_requested = "cancelled", "CANCELLED", True
                elif job.state == "running":
                    job.state, job.cancel_requested = "cancelling", True
            self.service.audit(s, actor, pid, "prototype.scan.cancel", row.id)
            return self.dto(s, actor, pid, row)

    def retry_ai(self, actor, pid, run_id, expected, key):
        from .scan_pipeline import check_version, enabled, retry

        with self.service.db() as s:
            row = self._get(s, actor, pid, run_id)
            MaterialService(self.service)._draft(s, actor, pid, row.analysis_set_id)
            s.refresh(row, with_for_update=True)
            self._scope(s, actor, pid, row)

            def apply():
                check_version(row, expected)
                if not enabled(row):
                    raise DomainError("VKS_STATE_CONFLICT", "请明确继续扫描并启用逐页关联", 409)
                retry(self.service, s, actor, row)
                return {"id": row.id}

            self.service.idem(
                s, actor, pid, "prototype.scan.retry-ai", key, {"id": run_id, "expected": expected}, apply
            )
            return self.dto(s, actor, pid, row)


def install_prototype_scan(app, service, actor, envelope):
    shared = PrototypeScanService(service)
    ActorDep = Annotated[object, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/associations/scans"

    @app.get(prefix, response_model=PrototypeEnvelope[dict])
    def status(pid: str, analysis_set_id: str, request: Request, a: ActorDep):
        return envelope(request, shared.status(a, pid, analysis_set_id))

    @app.post(prefix, response_model=PrototypeEnvelope[dict])
    def start(
        pid: str,
        form: ScanInput,
        request: Request,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.start(a, pid, form, if_match, idempotency_key))

    @app.post(prefix + "/{run_id}/resume", response_model=PrototypeEnvelope[dict])
    def resume(
        pid: str,
        run_id: str,
        form: ScanClient,
        request: Request,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.resume(a, pid, run_id, form, if_match, idempotency_key))

    @app.post(prefix + "/{run_id}/heartbeat", response_model=PrototypeEnvelope[dict])
    def heartbeat(pid: str, run_id: str, form: ScanHeartbeat, request: Request, a: ActorDep):
        return envelope(request, shared.heartbeat(a, pid, run_id, form))

    @app.post(prefix + "/{run_id}/retry-ai", response_model=PrototypeEnvelope[dict])
    def retry_ai(
        pid: str,
        run_id: str,
        request: Request,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.retry_ai(a, pid, run_id, if_match, idempotency_key))

    @app.post(prefix + "/{run_id}/pause", response_model=PrototypeEnvelope[dict])
    def pause(pid: str, run_id: str, form: ScanClient, request: Request, a: ActorDep):
        return envelope(request, shared.pause(a, pid, run_id, form.client_id))

    @app.post(prefix + "/{run_id}/cancel", response_model=PrototypeEnvelope[dict])
    def cancel(pid: str, run_id: str, form: ScanClient, request: Request, a: ActorDep):
        return envelope(request, shared.cancel(a, pid, run_id, form.client_id))
