"""Durable, permission-scoped upfront association of source-declared controls.

Inventory extraction treats source HTML as data. It never executes imported
scripts, follows business actions, or claims coverage of unseen dynamic states.
"""

import json
import re
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from contextvars import copy_context
from copy import deepcopy
from pathlib import PurePosixPath
from threading import RLock
from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from pydantic import Field, ValidationError
from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .ai_gateway import ModelPolicy
from .interactive import DOMTarget, InteractiveBinding, InteractiveKnowledgeService
from .interactive_ai import AssistanceInput, InteractiveAssistant
from .materials import ROOT, MaterialService, StudioWorkspace, roots
from .models import Analysis, Artifact, Base, Entity, Job, Revision, Selection, now
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref, version
from .reading import ReadingService
from .service import DomainError, canonical, sha

_CACHE, _CACHE_LOCK = OrderedDict(), RLock()
PROCESSOR = "source-controls-v2"


class PrototypeInventoryPage(Base):
    """Derived browser observations; immutable source bytes remain untouched."""

    __tablename__ = "prototype_inventory_page"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("artifact.id"), index=True)
    composition: Mapped[str] = mapped_column(String(64))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    route: Mapped[str] = mapped_column(String(1000))
    frame_path: Mapped[list] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)
    observed_by: Mapped[str] = mapped_column(String(96))
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class InventoryPageInput(Input):
    analysis_set_id: Ref
    package_id: Ref
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    composition: str = Field(pattern=r"^[a-f0-9]{64}$")
    route: str = Field(min_length=1, max_length=1000, pattern=r"^/")
    frame_path: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(max_length=8)
    targets: list[DOMTarget] = Field(default_factory=list, max_length=1500)
    observed: int = Field(ge=0, le=100000, strict=True)
    truncated: int = Field(ge=0, le=100000, strict=True)
    unverified_frames: int = Field(default=0, ge=0, le=100, strict=True)
    status: Literal["captured", "failed"]
    scan_run_id: Ref | None = None
    client_id: str | None = Field(default=None, min_length=8, max_length=96, pattern=r"^[A-Za-z0-9._:-]+$")


class BatchAssociationInput(Input):
    analysis_set_id: Ref
    mode: Literal["missing", "changed", "all"] = "missing"
    resume_job_id: Ref | None = None
    concurrency: int = Field(default=3, ge=1, le=4, strict=True)
    scan_run_id: Ref | None = None
    allow_partial: bool = Field(default=False, strict=True)


def target_id(package_id, target):
    return sha(canonical({"package_id": package_id, "target": {k: v for k, v in target.items() if k != "open_steps"}}))


def composition(s, pid, aid):
    # Batch-generated actions and knowledge do not invalidate their own job.
    # Manual document/section edits do, even if the material root is unchanged.
    selected = list(
        s.execute(
            select(Revision.id, Revision.content_sha256)
            .join(Selection, Selection.entity_revision_id == Revision.id)
            .join(Entity, Entity.id == Revision.entity_id)
            .where(
                Selection.analysis_set_id == aid,
                Revision.project_id == pid,
                Entity.project_id == pid,
                Entity.type.in_(("document", "section")),
            )
        )
    )
    return sha(
        canonical(
            {"packages": sorted(r.id for r in roots(s, pid, aid)), "documents": sorted(tuple(r) for r in selected)}
        )
    )


def inventory(service, s, package, root):
    from .exports import read_original
    from .material_parser import parse

    art = s.get(Artifact, package["id"])
    key = (art.sha256, PROCESSOR)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return deepcopy(_CACHE[key])
    meta = root.metadata_json[ROOT] if root else {}
    if meta.get("inventory"):
        report = {**deepcopy(meta["inventory"]), "targets": deepcopy(meta.get("targets", []))}
    else:
        inputs = []
        for aid in art.metadata_json.get("prototype_input_artifact_ids", []):
            original = s.get(Artifact, aid)
            if not original or original.project_id != art.project_id:
                raise DomainError("VKS_INTEGRITY_ERROR", "原型输入制品不完整", 409)
            inputs.append((PurePosixPath(original.relative_path).name, read_original(service, original)))
        if not inputs:
            inputs = [("index.html", read_original(service, art))]
        parsed = parse("prototype", inputs, meta.get("import_entry", ""))
        report = {**parsed["inventory"], "targets": parsed["targets"]}
    with _CACHE_LOCK:
        _CACHE[key] = deepcopy(report)
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return report


class BulkAssociationService:
    def __init__(self, service):
        self.service = service
        self.links = InteractiveKnowledgeService(service)

    def _observations(self, s, pid, aid, digest, packages):
        visible = {p["id"]: p["sha256"] for p in packages}
        latest = {}
        # Only the exact prototype bytes determine its DOM. A document-only
        # revision change need not force another browser scan of those bytes.
        # Every new upload still verifies the full current composition below.
        for row in s.scalars(
            select(PrototypeInventoryPage)
            .where(
                PrototypeInventoryPage.project_id == pid,
                PrototypeInventoryPage.analysis_set_id == aid,
            )
            .order_by(PrototypeInventoryPage.updated_at.desc(), PrototypeInventoryPage.id)
        ):
            if visible.get(row.package_id) == row.artifact_sha256:
                latest.setdefault((row.package_id, row.route, tuple(row.frame_path)), row)
        return list(latest.values())

    def inventory_status(self, actor, pid, aid, *, session=None):
        with nullcontext(session) if session is not None else ReadingService(self.service).db() as s:
            analysis, _, choices, _ = self.links.reader.scope(s, actor, pid, aid, None)
            digest = composition(s, pid, analysis.id)
            packages = self.links._packages(s, actor, pid, choices, kind="prototype")
            by_artifact = {r.artifact_id: r for r in roots(s, pid, choices)}
            observations = self._observations(s, pid, analysis.id, digest, packages)
            previous = {(r.package_id, r.route, tuple(r.frame_path)): r for r in observations}
            result = []
            for package in packages:
                report = inventory(self.service, s, package, by_artifact.get(package["id"]))
                pages = []
                for t in report["targets"]:
                    if t["selector"] != "body":
                        continue
                    row = previous.get((package["id"], t["route"], tuple(t["frame_path"])))
                    pages.append(
                        {
                            **{k: t[k] for k in ("route", "frame_path", "label")},
                            "status": row.report["status"] if row else "pending",
                            "current_count": len(row.report["targets"]) if row else 0,
                            "current_controls": sum(t["selector"] != "body" for t in row.report["targets"])
                            if row
                            else 0,
                        }
                    )
                result.append(
                    {
                        "package_id": package["id"],
                        "sha256": package["sha256"],
                        "kind": "prototype",
                        "format": package.get("format", ""),
                        "url": f"/api/v3/projects/{pid}/interactive/packages/{package['id']}/content?analysis_set_id={analysis.id}",
                        "pages": pages,
                    }
                )
            return {
                "analysis_set_id": analysis.id,
                "selection_version": analysis.selection_version,
                "composition": digest,
                "editable": analysis.state != "frozen" and self.service._can(s, actor, pid, "knowledge:edit"),
                "packages": result,
                "pages_total": sum(len(p["pages"]) for p in result),
                "pages_captured": sum(r.report["status"] == "captured" for r in observations),
                "pages_failed": sum(r.report["status"] == "failed" for r in observations),
            }

    def save_inventory(self, actor, pid, form, expected, key):
        with self.service.db() as s:
            analysis = MaterialService(self.service)._draft(s, actor, pid, form.analysis_set_id)
            scanner, scan_run = None, None
            if bool(form.scan_run_id) != bool(form.client_id):
                raise DomainError("VKS_INVALID_INPUT", "扫描任务与客户端必须同时提供", 422)
            if form.scan_run_id:
                from .prototype_scan import PrototypeScanService

                scanner = PrototypeScanService(self.service)
                scan_run = scanner._get(s, actor, pid, form.scan_run_id, lock=True)
                if scan_run.analysis_set_id != analysis.id:
                    raise DomainError("VKS_INVALID_INPUT", "扫描不属于当前工作区", 422)
                scanner._scope(s, actor, pid, scan_run)
                if (
                    scan_run.analysis_set_id != analysis.id
                    or scan_run.client_id != form.client_id
                    or scan_run.state not in ("running", "completed")
                    or scan_run.state == "running"
                    and scan_run.lease_until <= now()
                ):
                    raise DomainError("VKS_STATE_CONFLICT", "扫描已停止、已被接管或连接已中断", 409)
            state = s.get(StudioWorkspace, pid)
            if state and state.analysis_set_id != analysis.id:
                raise DomainError("VKS_VERSION_CONFLICT", "项目工作区已切换，请重新采集", 409)
            _, _, choices, _ = self.links.reader.scope(s, actor, pid, analysis.id, None)
            packages = self.links._packages(s, actor, pid, choices, kind="prototype")
            package = next((p for p in packages if p["id"] == form.package_id), None)
            if not package:
                raise DomainError("VKS_NOT_FOUND", "当前版本中没有此原型", 404)
            digest = composition(s, pid, analysis.id)
            if digest != form.composition or package["sha256"] != form.artifact_sha256:
                raise DomainError("VKS_VERSION_CONFLICT", "原型或文档版本已变化，请重新采集", 412)
            root = next((r for r in roots(s, pid, choices) if r.artifact_id == package["id"]), None)
            report = inventory(self.service, s, package, root)
            if not any(
                t["selector"] == "body" and t["route"] == form.route and t["frame_path"] == form.frame_path
                for t in report["targets"]
            ):
                raise DomainError("VKS_INVALID_INPUT", "采集页面不属于当前原型声明的路径", 422)
            targets = [t.model_dump() for t in form.targets]
            if (
                form.observed < len(targets)
                or form.truncated != form.observed - len(targets)
                or form.status == "failed"
                and targets
                or form.status == "captured"
                and not any(t["selector"] == "body" and t["frame_path"] == form.frame_path for t in targets)
                or any(
                    t["route"] != form.route
                    or t["frame_path"][: len(form.frame_path)] != form.frame_path
                    or t["open_steps"]
                    for t in targets
                )
            ):
                raise DomainError("VKS_INVALID_INPUT", "采集范围、计数或目标格式不一致", 422)
            unique = {target_id(package["id"], t): t for t in targets}
            if len(unique) != len(targets):
                raise DomainError("VKS_INVALID_INPUT", "采集结果含重复目标", 422)
            identity = sha(canonical([pid, analysis.id, digest, package["id"], form.route, form.frame_path]))

            def save():
                if scan_run and scan_run.state != "running":
                    raise DomainError("VKS_STATE_CONFLICT", "扫描已完成，请明确开始新的扫描", 409)
                if scan_run:
                    from .scan_pipeline import check_version

                    check_version(scan_run, expected)
                else:
                    version(analysis.selection_version, expected)
                observations = self._observations(s, pid, analysis.id, digest, packages)
                if (
                    sum(
                        len(r.report.get("targets", []))
                        for r in observations
                        if not (
                            r.package_id == package["id"] and r.route == form.route and r.frame_path == form.frame_path
                        )
                    )
                    + len(targets)
                    > 50000
                ):
                    raise DomainError("VKS_LIMIT_EXCEEDED", "本版本的浏览器控件清单超过 50000 项，请拆分原型", 422)
                row = s.get(PrototypeInventoryPage, identity)
                if not row:
                    row = PrototypeInventoryPage(
                        id=identity,
                        project_id=pid,
                        analysis_set_id=analysis.id,
                        package_id=package["id"],
                        composition=digest,
                        artifact_sha256=package["sha256"],
                        route=form.route,
                        frame_path=form.frame_path,
                        observed_by=actor.id,
                    )
                    s.add(row)
                row.report = {
                    "status": form.status,
                    "targets": targets,
                    "observed": form.observed,
                    "truncated": form.truncated,
                    "unverified_frames": form.unverified_frames,
                }
                row.updated_at, row.observed_by = now(), actor.id
                if scan_run:
                    scanner.finish_page(scan_run, form)
                s.flush()
                if scan_run and form.status == "captured":
                    from .scan_pipeline import produce

                    produce(self.service, s, actor, scan_run, targets=[(package["id"], t) for t in targets])
                observations = self._observations(s, pid, analysis.id, digest, packages)
                return {
                    "analysis_set_id": analysis.id,
                    "selection_version": analysis.selection_version,
                    "route": form.route,
                    "frame_path": form.frame_path,
                    "status": form.status,
                    "target_count": len(targets),
                    "pages_captured": sum(r.report["status"] == "captured" for r in observations),
                    "pages_failed": sum(r.report["status"] == "failed" for r in observations),
                    **({"scan_run": scanner.dto(s, actor, pid, scan_run)} if scan_run else {}),
                }

            return self.service.idem(
                s, actor, pid, "associations.inventory", key, {**form.model_dump(), "expected": expected}, save
            )

    def scan(self, s, actor, pid, aid, *, target_keys=None):
        analysis, _release, choices, _ = self.links.reader.scope(s, actor, pid, aid, None)
        packages = self.links._packages(s, actor, pid, choices, kind="prototype")
        digest = composition(s, pid, analysis.id)
        observations = self._observations(s, pid, analysis.id, digest, packages)
        by_artifact = {r.artifact_id: r for r in roots(s, pid, choices)}
        bindings = self.links.bindings(actor, pid, aid, session=s)
        bound = {target_id(b["package_id"], b["target"]): b for b in bindings}
        items, groups, limitations, page_titles = {}, {}, [], {}
        truncated, pages, dynamic = 0, 0, False
        for package in packages:
            report = inventory(self.service, s, package, by_artifact.get(package["id"]))
            runtime_pages = [r for r in observations if r.package_id == package["id"]]
            captured = {(r.route, tuple(r.frame_path)) for r in runtime_pages if r.report["status"] == "captured"}
            report["targets"] = [t for t in report["targets"] if (t["route"], tuple(t["frame_path"])) not in captured]
            for row in runtime_pages:
                truncated += row.report.get("truncated", 0)
                if row.report["status"] == "captured":
                    report["targets"].extend(row.report["targets"])
                else:
                    limitations.append(
                        {
                            "package_id": package["id"],
                            "code": "PAGE_CAPTURE_FAILED",
                            "message": "部分声明页面未能完成浏览器采集，可重新补齐；未将未见控件标记为已关联。",
                        }
                    )
                if row.report.get("unverified_frames"):
                    limitations.append(
                        {
                            "package_id": package["id"],
                            "code": "FRAMES_UNVERIFIED",
                            "message": "部分子页面无法读取，未将其中控件计为已采集。",
                        }
                    )
            if captured:
                limitations.append(
                    {
                        "package_id": package["id"],
                        "code": "DEFAULT_STATES_ONLY",
                        "message": "浏览器已采集声明页面的默认可见控件；未点击业务按钮，未打开弹窗及条件动态状态仍未覆盖。",
                    }
                )
            pages += report.get("pages", 0)
            truncated += report.get("truncated", 0)
            dynamic = dynamic or report.get("dynamic", False)
            if report.get("dynamic"):
                limitations.append(
                    {
                        "package_id": package["id"],
                        "code": "DYNAMIC_STATES_UNVERIFIED",
                        "message": "脚本动态生成及未打开状态的控件尚未验证；后续实际操作可补充关联。",
                    }
                )
            for raw in report["targets"]:
                try:
                    target = DOMTarget.model_validate(raw).model_dump()
                except ValidationError:
                    truncated += 1
                    continue
                key = target_id(package["id"], target)
                if target_keys is not None and key not in target_keys:
                    continue
                if key in items:
                    continue
                page = (package["id"], target["route"], tuple(target["frame_path"]))
                if target["selector"] == "body":
                    page_titles[page] = target["label"]
                current = bound.get(key)
                state = (
                    "excluded"
                    if current and current["excluded"]
                    else "manual"
                    if current and current.get("origin") == "manual" and current["validity_status"] == "valid"
                    else "linked"
                    if current and current["validity_status"] == "valid" and current["sections"]
                    else "pending"
                )
                item = {
                    "id": key,
                    "package_id": package["id"],
                    "target": target,
                    "state": state,
                    "binding_id": current["id"] if current else None,
                    "existing": bool(current),
                    "context": {
                        "scope": "module" if target["selector"] == "body" else "control",
                        "page_title": page_titles.get(page, target["label"]),
                    },
                }
                items[key] = item
                operation = report.get("row_operations", {}).get(
                    json.dumps([target["route"], target["frame_path"], target["selector"]])
                )
                if not operation and "tr:nth-of-type(" in target["selector"]:
                    operation = [
                        re.sub(r"tr:nth-of-type\(\d+\)", "tr:nth-of-type(*)", target["selector"]),
                        target["state"],
                        target["label"],
                    ]
                group_key = (
                    sha(canonical([package["id"], target["route"], target["frame_path"], operation]))
                    if operation
                    else key
                )
                groups.setdefault(group_key, []).append(key)
                item["operation_group"] = group_key
        if truncated:
            limitations.append(
                {
                    "code": "INVENTORY_TRUNCATED",
                    "message": f"静态清单中另有 {truncated} 项超出当前扫描范围、缺少可用名称或定位格式不支持，未标记为已关联。",
                }
            )
        values = list(items.values())
        limitations = list(
            {(item.get("package_id"), item["code"], item["message"]): item for item in limitations}.values()
        )
        coverage = {
            "discovered": len(values),
            "modules": sum(i["target"]["selector"] == "body" for i in values),
            "controls": sum(i["target"]["selector"] != "body" for i in values),
            "pages": pages,
            "runtime_pages_captured": sum(r.report["status"] == "captured" for r in observations),
            "runtime_pages_failed": sum(r.report["status"] == "failed" for r in observations),
            "linked": sum(i["state"] in ("linked", "manual") for i in values),
            "manual": sum(i["state"] == "manual" for i in values),
            "excluded": sum(i["state"] == "excluded" for i in values),
            "pending": sum(i["state"] == "pending" for i in values),
            "operation_groups": len(groups),
            "duplicate_controls": len(values) - len(groups),
            "uncovered": truncated,
            "dynamic_unverified": dynamic,
            "limitations": limitations,
            "coverage_complete": not truncated
            and not dynamic
            and not any(r.report["status"] == "failed" or r.report.get("unverified_frames") for r in observations),
        }
        return analysis, composition(s, pid, aid), values, list(groups.values()), coverage

    def status(self, actor, pid, aid):
        with ReadingService(self.service).db() as s:
            analysis, digest, _items, _groups, coverage = self.scan(s, actor, pid, aid)
            jobs = []
            for job in s.scalars(
                select(Job)
                .where(Job.project_id == pid, Job.kind == "associate_all", Job.created_by == actor.id)
                .order_by(Job.created_at.desc())
                .limit(8)
            ):
                jobs.append(
                    {
                        **ReadingService(self.service).job_dto(s, actor, job),
                        "mode": job.input_json["mode"],
                        "scan_run_id": job.input_json.get("scan_run_id"),
                        "scan_pipeline": bool(job.input_json.get("scan_pipeline")),
                        "concurrency": job.input_json.get("concurrency", 3),
                        "current_scope": job.input_json["analysis_set_id"] == aid
                        and job.input_json["composition"] == digest,
                    }
                )
            return {
                "analysis_set_id": analysis.id,
                "selection_version": analysis.selection_version,
                "editable": analysis.state != "frozen" and self.service._can(s, actor, pid, "knowledge:edit"),
                "composition": digest,
                "coverage": coverage,
                "jobs": jobs,
            }

    def enqueue(self, actor, pid, form, expected, key):
        with self.service.db() as s:
            analysis = MaterialService(self.service)._draft(s, actor, pid, form.analysis_set_id)
            scan_run = None
            if form.scan_run_id:
                from .prototype_scan import PrototypeScanService

                scanner = PrototypeScanService(self.service)
                scan_run = scanner._get(s, actor, pid, form.scan_run_id, lock=True)
                if scan_run.analysis_set_id != analysis.id:
                    raise DomainError("VKS_INVALID_INPUT", "扫描不属于当前工作区", 422)
                scanner._scope(s, actor, pid, scan_run)
                if scan_run.plan.get("pipeline"):
                    raise DomainError("VKS_STATE_CONFLICT", "此扫描已逐页关联，请查看扫描的 AI 进度或重试 AI", 409)
                if scan_run.analysis_set_id != analysis.id or scan_run.state == "cancelled":
                    raise DomainError("VKS_STATE_CONFLICT", "预扫描未完成或已取消，不能启动关联", 409)
                if scan_run.mode != form.mode or form.resume_job_id:
                    raise DomainError("VKS_INVALID_INPUT", "关联模式必须与预扫描计划一致", 422)
                if scan_run.batch_job_id:
                    existing = s.get(Job, scan_run.batch_job_id)
                    if not existing or existing.project_id != pid or existing.created_by != actor.id:
                        raise DomainError("VKS_INTEGRITY_ERROR", "扫描关联任务记录不完整", 409)
                    if bool(existing.input_json.get("partial_scope")) != form.allow_partial:
                        raise DomainError("VKS_STATE_CONFLICT", "此扫描已按其他范围启动关联，请新建补齐任务", 409)
                    return self.service.job_dto(existing)
                from .prototype_scan import page_key

                all_pages = {page_key(p["package_id"], page) for p in scan_run.plan["packages"] for page in p["pages"]}
                captured = {k for k, value in scan_run.completed.items() if value["status"] == "captured"}
                if not form.allow_partial and (scan_run.state != "completed" or not all_pages <= captured):
                    raise DomainError("VKS_STATE_CONFLICT", "仍有未采集页面，请继续扫描或明确选择仅关联已采集内容", 409)
                if form.allow_partial and (
                    not captured or scan_run.state == "running" and scan_run.lease_until > now()
                ):
                    raise DomainError(
                        "VKS_STATE_CONFLICT", "请先停止正在运行的扫描阶段，或等待扫描结束后选择部分关联", 409
                    )
            elif form.allow_partial:
                raise DomainError("VKS_INVALID_INPUT", "部分关联必须绑定明确的扫描任务", 422)
            state = s.get(StudioWorkspace, pid)
            if state and state.analysis_set_id != analysis.id:
                raise DomainError("VKS_VERSION_CONFLICT", "项目工作区已切换，请刷新后开始预关联", 409)
            _, digest, items, groups, coverage = self.scan(s, actor, pid, analysis.id)
            partial_scope = None
            if scan_run and form.allow_partial:
                pages = [
                    (package["package_id"], page)
                    for package in scan_run.plan["packages"]
                    for page in package["pages"]
                    if page_key(package["package_id"], page) in captured
                ]
                items = [
                    item
                    for item in items
                    if any(
                        item["package_id"] == package
                        and item["target"]["route"] == page["route"]
                        and item["target"]["frame_path"][: len(page["frame_path"])] == page["frame_path"]
                        for package, page in pages
                    )
                ]
                allowed = {item["id"] for item in items}
                groups = [[key for key in group if key in allowed] for group in groups]
                groups = [group for group in groups if group]
                partial_scope = {
                    "scan_run_id": scan_run.id,
                    "pages_total": len(all_pages),
                    "pages_captured": len(captured),
                    "pages_failed": sum(v["status"] == "failed" for v in scan_run.completed.values()),
                    "target_count": len(items),
                }
                coverage = {
                    **coverage,
                    "partial": True,
                    "partial_scope": partial_scope,
                    "discovered": len(items),
                    "modules": sum(i["target"]["selector"] == "body" for i in items),
                    "controls": sum(i["target"]["selector"] != "body" for i in items),
                    "pages": len(pages),
                    "linked": sum(i["state"] in ("linked", "manual") for i in items),
                    "manual": sum(i["state"] == "manual" for i in items),
                    "excluded": sum(i["state"] == "excluded" for i in items),
                    "pending": sum(i["state"] == "pending" for i in items),
                    "operation_groups": len(groups),
                    "duplicate_controls": len(items) - len(groups),
                    "coverage_complete": False,
                }
            active = next(
                (
                    j
                    for j in s.scalars(
                        select(Job).where(
                            Job.project_id == pid,
                            Job.kind == "associate_all",
                            Job.created_by == actor.id,
                            Job.state.in_(("queued", "running", "retry_wait")),
                            Job.cancel_requested.is_(False),
                        )
                    )
                    if j.input_json["analysis_set_id"] == analysis.id and j.input_json["composition"] == digest
                ),
                None,
            )
            request = {**form.model_dump(), "expected": expected}
            if active:
                planned = {item["id"] for item in active.input_json.get("items", [])}
                if (
                    not {item["id"] for item in items} <= planned
                    or (form.mode == "all" and active.input_json["mode"] != "all")
                    or (
                        form.allow_partial
                        and (planned != {item["id"] for item in items} or not active.input_json.get("partial_scope"))
                    )
                    or (not form.allow_partial and active.input_json.get("partial_scope"))
                ):
                    raise DomainError(
                        "VKS_STATE_CONFLICT", "已有预关联任务未包含新扫描目标，请等待该任务结束后继续当前扫描", 409
                    )
                if scan_run:
                    scan_run.batch_job_id = active.id
                return self.service.idem(
                    s, actor, pid, "associations.batch", key, request, lambda: self.service.job_dto(active)
                )

            def create():
                version(analysis.selection_version, expected)
                if form.resume_job_id:
                    old = s.get(Job, form.resume_job_id)
                    if not old or old.project_id != pid or old.kind != "associate_all" or old.created_by != actor.id:
                        raise DomainError("VKS_NOT_FOUND", "未找到可继续的预关联任务", 404)
                    if old.input_json["analysis_set_id"] != analysis.id or old.input_json["composition"] != digest:
                        raise DomainError("VKS_VERSION_CONFLICT", "资料版本已变化，请开始当前版本的新任务", 409)
                    if old.state not in ("failed", "cancelled", "succeeded"):
                        raise DomainError("VKS_STATE_CONFLICT", "此任务当前不能继续", 409)
                    previous_concurrency = old.input_json.get("concurrency", 3)
                    old.input_json = {**old.input_json, "concurrency": form.concurrency}
                    old.state, old.stage, old.cancel_requested, old.error_json = "queued", "RESUME", False, {}
                    old.updated_at = now()
                    # Failed/insufficient targets are retried only after this explicit request.
                    old.result_json = {
                        **old.result_json,
                        "processing_epoch": old.attempt + 1,
                        "completed": {
                            k: v
                            for k, v in old.result_json.get("completed", {}).items()
                            if v.get("status") in ("linked", "reused", "manual", "excluded")
                        },
                    }
                    self.service.audit(
                        s,
                        actor,
                        pid,
                        "associations.resume",
                        old.id,
                        metadata={"concurrency_from": previous_concurrency, "concurrency_to": form.concurrency},
                    )
                    return self.service.job_dto(old)
                if not items:
                    raise DomainError("VKS_INVALID_INPUT", "当前版本没有可扫描的原型控件", 422)
                needs_model = any(
                    item["state"] == "pending"
                    and (form.mode != "changed" or item["existing"])
                    or form.mode == "all"
                    and item["state"] == "linked"
                    for item in items
                )
                if needs_model:
                    policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                    if not policy or not policy.settings.get("enabled") or not policy.settings.get("allow_external"):
                        raise DomainError(
                            "VKS_MODEL_NOT_CONFIGURED", "请先在 AI 设置中启用项目模型并允许使用相关内容", 503
                        )
                    _, _, choices, _ = self.links.reader.scope(s, actor, pid, analysis.id, None)
                    if not self.links._rows(s, actor, pid, choices, types={"document"}):
                        raise DomainError("VKS_INVALID_INPUT", "请先导入可读取的知识文档，再开始预关联", 422)
                payload = {
                    "analysis_set_id": analysis.id,
                    "composition": digest,
                    "mode": form.mode,
                    "items": items,
                    "groups": groups,
                    "coverage": coverage,
                    "processor": PROCESSOR,
                    "concurrency": form.concurrency,
                    "needs_model": needs_model,
                    **({"scan_run_id": scan_run.id} if scan_run else {}),
                    **({"partial_scope": partial_scope} if partial_scope else {}),
                }
                job = self.service._new_job(s, actor, pid, "associate_all", payload)
                if scan_run:
                    scan_run.batch_job_id = job.id
                return self.service.job_dto(job)

            return self.service.idem(s, actor, pid, "associations.batch", key, request, create)

    def details(self, actor, pid, jid, offset=0, limit=100):
        from .jobs import Checkpoint

        with ReadingService(self.service).db() as s:
            self.service.require(s, actor, pid)
            job = s.get(Job, jid)
            if not job or job.project_id != pid or job.kind != "associate_all" or job.created_by != actor.id:
                raise DomainError("VKS_NOT_FOUND", "未找到预关联任务", 404)
            dto = ReadingService(self.service).job_dto(s, actor, job)
            if dto.get("diagnostics_restricted"):
                return {"items": [], "offset": offset, "has_more": False, "diagnostics_restricted": True}
            rows = list(
                s.scalars(
                    select(Checkpoint)
                    .where(
                        Checkpoint.job_id == jid,
                        Checkpoint.stage == "ASSOCIATE",
                        Checkpoint.processor_version == PROCESSOR,
                    )
                    .order_by(Checkpoint.created_at, Checkpoint.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {"items": [r.result_json for r in rows[:limit]], "offset": offset, "has_more": len(rows) > limit}


def run_batch(service, jid, *, preserve_processed=False):
    from packages.analyzers.cancellation import checkpoint

    from .jobs import Checkpoint, current_actor

    assistant = InteractiveAssistant(service)
    with service.db() as s:
        job = s.get(Job, jid)
        payload, attempt, pid = deepcopy(job.input_json), job.attempt, job.project_id
        processing_epoch = job.result_json.get("processing_epoch", 1)
        completed = {
            r.input_hash: deepcopy(r.result_json)
            for r in s.scalars(
                select(Checkpoint).where(
                    Checkpoint.job_id == jid, Checkpoint.stage == "ASSOCIATE", Checkpoint.processor_version == PROCESSOR
                )
            )
            if (preserve_processed and r.result_json.get("attempt", 1) >= processing_epoch)
            or r.result_json.get("status") in ("linked", "reused", "manual", "excluded", "skipped")
        }
    completed_lock = RLock()
    counters = Counter(v["status"] for v in completed.values())
    items = {i["id"]: i for i in payload["items"]}

    def validate(s):
        active = s.get(Job, jid)
        if (
            not active
            or active.attempt != attempt
            or active.state != "running"
            or active.cancel_requested
            or active.lease_until
            and active.lease_until <= now()
        ):
            raise DomainError("VKS_CANCELLED", "预关联任务已停止或执行资格失效", 409)
        actor = current_actor(service, active.created_by, pid, s, job=active)
        service.require(s, actor, pid, "knowledge:edit")
        if payload.get("scan_pipeline"):
            from .prototype_scan import PrototypeScanRun

            run = s.get(PrototypeScanRun, payload["scan_run_id"])
            if (
                not run
                or run.state == "cancelled"
                or not run.plan.get("pipeline")
                or run.project_id != pid
                or run.analysis_set_id != payload["analysis_set_id"]
                or run.created_by != active.created_by
                or run.composition != payload["composition"]
            ):
                raise DomainError("VKS_CANCELLED", "逐页关联已取消或来源已失效", 409)
        analysis = s.get(Analysis, payload["analysis_set_id"])
        state = s.get(StudioWorkspace, pid)
        if (
            not analysis
            or analysis.state == "frozen"
            or state
            and state.analysis_set_id != analysis.id
            or composition(s, pid, analysis.id) != payload["composition"]
        ):
            raise DomainError("VKS_VERSION_CONFLICT", "资料版本已变化，停止旧版本预关联", 409)
        return actor

    def result():
        values = list(completed.values())[-200:]
        pending = counters["insufficient"] + counters["failed"]
        issues = [
            {"label": v["label"], "message": v["message"]} for v in values if v["status"] in ("insufficient", "failed")
        ]
        if pending > len(issues):
            issues.append(
                {"label": "其他待处理项", "message": f"另有 {pending - len(issues)} 项待处理，完整结果可分页查看。"}
            )
        return {
            "processing_epoch": processing_epoch,
            "completed": {v["id"]: v for v in values},
            "processed": len(completed),
            "total": len(items),
            "saved": counters["linked"],
            "reused": counters["reused"],
            "manual": counters["manual"],
            "excluded": counters["excluded"],
            "skipped": counters["skipped"],
            "pending": pending,
            "details_truncated": len(completed) > 200,
            "operation_groups": len(payload["groups"]),
            "coverage": payload["coverage"],
            "issues": issues
            + [
                {"label": "扫描范围", "message": limitation["message"]}
                for limitation in payload["coverage"]["limitations"]
            ],
            "result_refs": [{"kind": "analysis_set", "id": payload["analysis_set_id"]}],
        }

    def persist(key, outcome):
        with service.db() as s:
            validate(s)
            job = s.get(Job, jid)
            row = s.scalar(
                select(Checkpoint).where(
                    Checkpoint.job_id == jid,
                    Checkpoint.stage == "ASSOCIATE",
                    Checkpoint.input_hash == key,
                    Checkpoint.processor_version == PROCESSOR,
                )
            )
            if row:
                row.result_json = outcome
            else:
                s.add(
                    Checkpoint(
                        job_id=jid, stage="ASSOCIATE", input_hash=key, processor_version=PROCESSOR, result_json=outcome
                    )
                )
            if payload.get("scan_pipeline"):
                from .scan_pipeline import PrototypeScanTarget

                target = s.get(PrototypeScanTarget, (payload["scan_run_id"], key))
                if not target or target.job_id != jid:
                    raise DomainError("VKS_STATE_CONFLICT", "逐页关联目标消费记录已变化", 409)
                target.state = (
                    "complete"
                    if outcome["status"] in {"linked", "reused", "manual", "excluded", "skipped"}
                    else "failed"
                )
                target.outcome, target.updated_at = deepcopy(outcome), now()
            with completed_lock:
                completed[key] = outcome
                counters[outcome["status"]] += 1
                job.stage, job.progress, job.result_json = (
                    "ASSOCIATE",
                    int(len(completed) * 100 / max(1, len(items))),
                    result(),
                )

    if payload.get("needs_model", True) and len(completed) < len(items) and assistant.rag.store.settings(pid).enabled:
        with service.db() as s:
            actor = validate(s)
            s.get(Job, jid).stage = "DOCUMENT_INDEX"

        def validate_index():
            with service.db() as s:
                validate(s)

        assistant.rag.build_documents(
            actor,
            pid,
            analysis_set_id=payload["analysis_set_id"],
            validate_scope=validate_index,
            job_id=jid,
            job_attempt=attempt,
        )

    def reusable(actor, source, s):
        if not source:
            return None
        form = AssistanceInput(
            analysis_set_id=payload["analysis_set_id"],
            package_id=source["package_id"],
            target=source["target"],
            context=source["context"],
        )
        bound = assistant._binding(actor, pid, form, session=s)
        if not bound or bound["excluded"] or bound.get("origin") != "model" or bound["validity_status"] != "valid":
            return None
        point = s.get(Revision, bound["point_revision_id"])
        artifact = s.get(Artifact, point.artifact_id)
        generation = artifact.metadata_json.get("interactive_generation", {})
        _config, chapters, _corpus, request_key, *_ = assistant.prepare(actor, pid, form, session=s)
        allowed = {c["revision_id"] for c in chapters}
        if generation.get("request_key") != request_key or not set(generation.get("input_revision_ids", [])) <= allowed:
            return None
        sections = [c["revision_id"] for c in bound["sections"]]
        if not sections or not set(sections) <= allowed:
            return None
        return {
            "title": bound["title"],
            "summary": bound["summary"],
            "sections": sections,
            "generation": deepcopy(generation),
            "canonical_item": source,
        }

    def process_group(group):
        with service.db() as s:
            actor = validate(s)
            inferred = reusable(actor, payload.get("reuse_from_item"), s)
        for key in group:
            checkpoint(force=True)
            if key in completed:
                continue
            item = items[key]
            with service.db() as s:
                actor = validate(s)
                current = assistant._binding(
                    actor,
                    pid,
                    AssistanceInput(
                        analysis_set_id=payload["analysis_set_id"], package_id=item["package_id"], target=item["target"]
                    ),
                    session=s,
                )
            status, message = None, None
            if current and current["excluded"]:
                status, message = "excluded", "用户已移除，保持移除状态"
            elif current and current.get("origin") == "manual" and current["validity_status"] == "valid":
                status, message = "manual", "保留人工维护的说明与引用"
            elif current and current["validity_status"] == "valid" and payload["mode"] != "all":
                status, message = "reused", "复用当前有效关联"
            elif not current and payload["mode"] == "changed":
                status, message = "skipped", "本次仅分析已有变更项，未关联控件留待补齐"
            if status is None:
                form = AssistanceInput(
                    analysis_set_id=payload["analysis_set_id"],
                    package_id=item["package_id"],
                    target=item["target"],
                    context=item["context"],
                    refresh=attempt == 1,
                )
                try:
                    # A row inserted or edited manually during inference is not overwritten.
                    before_manual = (
                        current["point_revision_id"] if current and current.get("origin") == "manual" else None
                    )

                    def guard(session, actor=actor, form=form, before_manual=before_manual):
                        validate(session)
                        latest = assistant._binding(actor, pid, form, session=session)
                        if latest and latest.get("origin") == "manual" and latest["point_revision_id"] != before_manual:
                            raise DomainError("VKS_STATE_CONFLICT", "人工说明已更新，保留当前人工内容", 409)

                    if inferred and inferred.get("failure"):
                        status, message = inferred["status"], inferred["failure"]
                    elif inferred is None:
                        answer = assistant.run(
                            actor, pid, form, migrate_manual=bool(before_manual), validate_scope=guard
                        )
                        if answer["saved"]:
                            with service.db() as s:
                                validate(s)
                                saved = assistant._binding(actor, pid, form, session=s)
                                point = s.get(Revision, saved["point_revision_id"])
                                art = s.get(Artifact, point.artifact_id)
                                inferred = {
                                    "title": answer["title"],
                                    "summary": answer["explanation"],
                                    "sections": [c["revision_id"] for c in answer["sections"]],
                                    "generation": deepcopy(art.metadata_json["interactive_generation"]),
                                    "canonical_item": item,
                                }
                        status, message = (
                            ("linked", "关联已保存")
                            if answer["saved"]
                            else ("insufficient", "文档依据不足或结果未保存")
                        )
                        if not answer["saved"]:
                            inferred = {"failure": message, "status": status}
                    else:
                        with assistant.links.reader.reading.db() as s:
                            if reusable(actor, inferred["canonical_item"], s) is None:
                                raise DomainError("VKS_STATE_CONFLICT", "复用依据或模型设置已变化，请重试 AI", 409)
                            alias_key = assistant.prepare(actor, pid, form, session=s)[3]
                        generation = {
                            **inferred["generation"],
                            "request_key": alias_key,
                            "batch_operation_reuse": group[0],
                        }
                        manual = bool(current and current.get("origin") == "manual")
                        generation["origin"] = "manual" if manual else "model"
                        alias = InteractiveBinding(
                            analysis_set_id=payload["analysis_set_id"],
                            package_id=form.package_id,
                            target=form.target,
                            point={
                                "title": current["title"] if manual else inferred["title"],
                                "summary": current["summary"] if manual else inferred["summary"],
                            },
                            section_revision_ids=inferred["sections"],
                            **(
                                {
                                    "binding_id": current["id"],
                                    "binding_version": current["binding_version"],
                                    "point_revision_id": current["point_revision_id"],
                                }
                                if current
                                else {}
                            ),
                        )
                        assistant.links.save(
                            actor,
                            pid,
                            alias,
                            None,
                            "batch." + jid + "." + key[:24],
                            generation=generation,
                            validate=lambda session, _a, _c, guard=guard, actor=actor, form=form, alias_key=alias_key, canonical_item=inferred["canonical_item"]: (
                                guard(session),
                                assistant._unchanged(actor, pid, form, alias_key, None, session),
                                reusable(actor, canonical_item, session)
                                or (_ for _ in ()).throw(
                                    DomainError("VKS_STATE_CONFLICT", "复用依据已变化，请重试 AI", 409)
                                ),
                            ),
                        )
                        status, message = "linked", "复用同表格操作的已核对依据，关联已保存"
                except DomainError as error:
                    if error.code in {"VKS_FORBIDDEN", "VKS_UNAUTHENTICATED", "VKS_VERSION_CONFLICT", "VKS_CANCELLED"}:
                        raise
                    status, message = "failed", error.message
                    inferred = {"failure": message, "status": status}
            outcome = {
                "id": key,
                "label": item["target"]["label"],
                "status": status,
                "saved": status == "linked",
                "message": message,
                "attempt": attempt,
            }
            persist(key, outcome)

    if payload.get("concurrency", 3) == 1:
        for group in payload["groups"]:
            process_group(group)
    else:
        with ThreadPoolExecutor(max_workers=payload.get("concurrency", 3), thread_name_prefix="vks-associate") as pool:
            futures = [pool.submit(copy_context().run, process_group, group) for group in payload["groups"]]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    return result()


def install_bulk_association(app, service, actor, envelope):
    shared = BulkAssociationService(service)
    ActorDep = Annotated[object, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/associations/batch"
    inventory_prefix = "/api/v3/projects/{pid}/associations/inventory"

    @app.get(inventory_prefix, response_model=PrototypeEnvelope[dict])
    def inventory_status(pid: str, analysis_set_id: str, request: Request, a: ActorDep):
        return envelope(request, shared.inventory_status(a, pid, analysis_set_id))

    @app.put(inventory_prefix, response_model=PrototypeEnvelope[dict])
    def save_inventory(
        pid: str,
        payload: InventoryPageInput,
        request: Request,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.save_inventory(a, pid, payload, if_match, idempotency_key))

    @app.get(prefix + "/items", response_model=PrototypeEnvelope[dict])
    def details(pid: str, job_id: str, request: Request, a: ActorDep, offset: int = 0, limit: int = 100):
        if offset < 0 or not 1 <= limit <= 200:
            raise DomainError("VKS_INVALID_INPUT", "分页范围无效", 422)
        return envelope(request, shared.details(a, pid, job_id, offset, limit))

    @app.get(prefix, response_model=PrototypeEnvelope[dict])
    def status(pid: str, analysis_set_id: str, request: Request, a: ActorDep):
        return envelope(request, shared.status(a, pid, analysis_set_id))

    @app.post(prefix, response_model=PrototypeEnvelope[dict])
    def enqueue(
        pid: str,
        payload: BatchAssociationInput,
        request: Request,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.enqueue(a, pid, payload, if_match, idempotency_key))
