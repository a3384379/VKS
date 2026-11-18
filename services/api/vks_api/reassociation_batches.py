"""Durable preview batches. Generating/reviewing candidates never selects knowledge."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from typing import Annotated, Literal

from fastapi import Depends, Header, Query, Request
from pydantic import Field
from sqlalchemy import select

from .association_limits import request_concurrency
from .bulk_association import BulkAssociationService, target_id
from .interactive_ai import InteractiveAssistant, ReassociationInput
from .jobs import current_actor
from .materials import MaterialService
from .models import Analysis, Artifact, Job, Project, Revision, now
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref, version
from .reading import ReadingService, artifact_policy
from .service import DomainError

KIND = "reassociation_preview"
TERMINAL = {"succeeded", "failed", "cancelled"}


class BatchPreviewInput(Input):
    analysis_set_id: Ref
    instruction: str = Field(default="", max_length=4000)
    mode: Literal["missing", "all"] = "all"
    items: list[ReassociationInput] | None = Field(default=None, min_length=1, max_length=10000)
    module_only: bool = True
    concurrency: int = Field(default=3, ge=1, le=4, strict=True)


class BatchConfirmInput(Input):
    job_id: Ref
    preview_ids: list[Ref] = Field(min_length=1, max_length=10000)


class BatchResultQuery(Input):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)
    q: str = Field(default="", max_length=200)
    status: Literal["all", "needs_attention", "failed", "insufficient", "generated", "pending"] = "all"
    excluded_item_ids: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=10000
    )
    insufficient_item_ids: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=10000
    )


class ReassociationBatchService:
    def __init__(self, service):
        self.service = service
        self.engine = InteractiveAssistant(service)
        self.inventory = BulkAssociationService(service)

    def _job(self, s, actor, pid, jid):
        self.service.require(s, actor, pid, "knowledge:edit")
        job = s.get(Job, jid)
        if not job or job.project_id != pid or job.kind != KIND or job.created_by != actor.id:
            raise DomainError("VKS_NOT_FOUND", "未找到本次关联预览", 404)
        return job

    def targets(self, actor, pid, aid, module_only=True, offset=0, limit=200, mode="all", q=""):
        with ReadingService(self.service).db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            _, _, inventory, _, _ = self.inventory.scan(s, actor, pid, aid)
            bindings = {
                target_id(b["package_id"], b["target"]): b
                for b in self.engine.links.bindings(actor, pid, aid, session=s)
            }
            rows = []
            for row in inventory:
                if (
                    row["state"] == "excluded"
                    or (module_only and row["target"]["selector"] != "body")
                    or (mode == "missing" and row["state"] != "pending")
                    or (
                        q.strip()
                        and q.strip().casefold()
                        not in " ".join(
                            [
                                row["target"]["label"],
                                row["context"].get("page_title", ""),
                                row["target"]["route"],
                            ]
                        ).casefold()
                    )
                ):
                    continue
                existing = bindings.get(row["id"])
                form = ReassociationInput(
                    analysis_set_id=aid,
                    package_id=row["package_id"],
                    target=row["target"],
                    context=row["context"],
                    binding_id=existing["id"] if existing else None,
                    binding_version=existing["binding_version"] if existing else None,
                    point_revision_id=existing["point_revision_id"] if existing else None,
                )
                rows.append(
                    {
                        "item_id": row["id"],
                        "state": row["state"],
                        "label": row["target"]["label"],
                        "module_label": row["context"].get("page_title") or row["target"]["label"],
                        "request": form.model_dump(),
                    }
                )
            return {
                "total": len(rows),
                "items": rows[offset : offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None,
            }

    def enqueue(self, actor, pid, form, expected=None, key=None):
        with self.service.db() as s:
            analysis = MaterialService(self.service)._draft(s, actor, pid, form.analysis_set_id)
            if expected is not None:
                version(analysis.selection_version, expected)
            # Source inventory and binding projection are permission-filtered in
            # this transaction; no caller-provided title/count bypasses them.
            current = self.engine.links.bindings(actor, pid, analysis.id, session=s)
            bindings = {target_id(b["package_id"], b["target"]): b for b in current}
            if form.items is None:
                _, _, inventory, _, _ = self.inventory.scan(s, actor, pid, analysis.id)
                candidates = [
                    ReassociationInput(
                        analysis_set_id=analysis.id,
                        package_id=row["package_id"],
                        target=row["target"],
                        context=row["context"],
                    )
                    for row in inventory
                    if row["state"] != "excluded"
                    and (form.mode != "missing" or row["state"] == "pending")
                    and (not form.module_only or row["target"]["selector"] == "body")
                ]
            else:
                candidates = form.items
            items = []
            seen = set()
            for candidate in candidates:
                if candidate.analysis_set_id != analysis.id or candidate.release_id or candidate.batch_id:
                    raise DomainError("VKS_INVALID_INPUT", "预览目标不属于当前草稿", 422)
                identity = target_id(candidate.package_id, candidate.target.model_dump())
                if identity in seen:
                    continue
                seen.add(identity)
                existing = bindings.get(identity)
                if existing and existing["excluded"]:
                    raise DomainError("VKS_STATE_CONFLICT", "已移除的关联不能参与本次 AI 预览", 409)
                if form.mode == "missing" and existing and existing["validity_status"] == "valid":
                    continue
                baseline = (
                    {k: existing[k] for k in ("binding_version", "point_revision_id")} | {"binding_id": existing["id"]}
                    if existing
                    else {"binding_id": None, "binding_version": None, "point_revision_id": None}
                )
                # Explicit stale targets must fail rather than silently rebase.
                for field in ("binding_id", "binding_version", "point_revision_id"):
                    supplied = getattr(candidate, field)
                    if supplied is not None and supplied != baseline[field]:
                        raise DomainError("VKS_VERSION_CONFLICT", "关联已变化，请刷新后重新预览", 412)
                value = candidate.model_copy(
                    update={
                        **baseline,
                        "instruction": form.instruction or candidate.instruction,
                        "mode": "documents",
                        "refresh": True,
                        "item_id": None,
                    }
                )
                self.engine._reassociation_guard(s, actor, pid, value)
                items.append({"item_id": identity, "form": value.model_dump()})
            if not items:
                raise DomainError("VKS_INVALID_INPUT", "当前范围没有可预览的关联目标", 422)
            if len(items) > 10000:
                raise DomainError("VKS_INVALID_INPUT", "一次最多预览 10000 个目标，请缩小范围", 422)

            def apply():
                job = self.service._new_job(
                    s,
                    actor,
                    pid,
                    KIND,
                    {
                        "analysis_set_id": analysis.id,
                        "selection_version": analysis.selection_version,
                        "instruction": form.instruction,
                        "mode": form.mode,
                        "items": items,
                        "concurrency": form.concurrency,
                        "pipeline_concurrency": form.concurrency,
                    },
                )
                return {"job_id": job.id, "total": len(items)}

            if key:
                return self.service.idem(s, actor, pid, "reassociation.preview_batch", key, form.model_dump(), apply)
            return apply()

    def _visible_items(self, s, actor, pid, job):
        aid = job.input_json["analysis_set_id"]
        _, _, choices, _ = self.engine.links.reader.scope(s, actor, pid, aid, None)
        packages = {p["id"] for p in self.engine.links._packages(s, actor, pid, choices, kind="prototype")}
        visible = []
        for item in job.input_json["items"]:
            form = item["form"]
            if form["package_id"] not in packages:
                continue
            point = form.get("point_revision_id")
            if point and not self.service._visible_revision(s, actor, pid, s.get(Revision, point)):
                continue
            visible.append(item)
        return visible

    def listing(self, actor, pid, jid, offset=0, limit=50, *, filters=None):
        if offset < 0 or not 1 <= limit <= 200:
            raise DomainError("VKS_INVALID_INPUT", "分页范围无效", 422)
        with ReadingService(self.service).db() as s:
            job = self._job(s, actor, pid, jid)
            visible = self._visible_items(s, actor, pid, job)
            # A regenerated row uses the newest immutable preview. Returning to
            # a list page must not restore the older candidate from the worker.
            newest = {}
            item_ids = {item["item_id"] for item in visible}
            hidden = set()
            for artifact in s.scalars(
                select(Artifact)
                .where(
                    Artifact.project_id == pid,
                    Artifact.metadata_json["batch_id"].as_string() == jid,
                    Artifact.metadata_json["item_id"].as_string().in_(item_ids),
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            ):
                identity = artifact.metadata_json["item_id"]
                if identity in newest or identity in hidden:
                    continue
                policy = artifact_policy(s, artifact)
                authorized = not (
                    policy.get("requires_source_read")
                    and not self.service._can(s, actor, pid, "source:read")
                    or policy.get("requires_trace_read")
                    and not self.service._can(s, actor, pid, "trace:read")
                ) and all(
                    self.service._visible_revision(s, actor, pid, s.get(Revision, ref["revision_id"]))
                    for ref in artifact.metadata_json.get("input_revision_provenance", [])
                )
                if authorized:
                    newest[identity] = artifact.id
                else:
                    hidden.add(identity)
            visible = [item for item in visible if item["item_id"] not in hidden]
            outcomes = job.result_json.get("items", {})
            state = job.state
            done = sum(item["item_id"] in outcomes for item in visible)
            excluded = set(filters.excluded_item_ids) if filters else set()
            insufficient = set(filters.insufficient_item_ids) if filters else set()
            status_filter = filters.status if filters else "all"
            needle = filters.q.strip().casefold() if filters else ""
            unresolved = 0
            prepared = []
            for item in visible:
                identity = item["item_id"]
                if identity in excluded:
                    continue
                outcome = outcomes.get(identity, {})
                preview_id = newest.get(identity) or outcome.get("preview_id")
                if identity in insufficient:
                    preview_id, status = None, "insufficient"
                else:
                    status = "generated" if preview_id else outcome.get("status", "pending")
                needs_attention = not preview_id
                unresolved += needs_attention
                if status_filter == "needs_attention":
                    if not needs_attention:
                        continue
                elif status_filter != "all" and status_filter != status:
                    continue
                form = item["form"]
                if (
                    needle
                    and needle
                    not in " ".join(
                        [
                            form["target"]["label"],
                            form["target"]["route"],
                            form["context"].get("page_title", ""),
                            form["context"].get("section_title", ""),
                        ]
                    ).casefold()
                ):
                    continue
                prepared.append((item, outcome, status, preview_id))
        rows = []
        for item, outcome, status, preview_id in prepared[offset : offset + limit]:
            value = item["form"]
            row = {
                "item_id": item["item_id"],
                "label": value["target"]["label"],
                "module_label": value["context"].get("page_title") or value["target"]["label"],
                "status": status,
                "preview_id": preview_id,
                "can_confirm": bool(preview_id),
                "request": {**value, "batch_id": jid, "item_id": item["item_id"]},
            }
            if status == "failed":
                row["error"] = {
                    "code": outcome.get("error_code", "VKS_MODEL_FAILED"),
                    "message": "本项预览未完成，可修改方向后重试。",
                }
            rows.append(row)
        # The total only covers currently authorized target metadata; generated
        # full-text permissions are checked again when its card is opened.
        return {
            "job_id": jid,
            "state": state,
            "processed": done,
            "total": len(visible),
            "items": rows,
            "next_offset": offset + limit if offset + limit < len(prepared) else None,
            **({"matched_total": len(prepared), "unresolved_total": unresolved} if filters else {}),
        }

    def confirm(self, actor, pid, form):
        with ReadingService(self.service).db() as s:
            job = self._job(s, actor, pid, form.job_id)
            if job.state not in TERMINAL:
                raise DomainError("VKS_STATE_CONFLICT", "请等待本次预览完成，或先停止生成", 409)
        return self.engine.confirm_reassociations(actor, pid, form.preview_ids, batch_id=form.job_id)


def run_reassociation_batch(service, jid):
    """Fence every admission/commit; persist only opaque preview references."""
    engine = InteractiveAssistant(service)
    with ReadingService(service).db() as s:
        job = s.get(Job, jid)
        if not job or job.kind != KIND or job.state != "running":
            raise DomainError("VKS_STATE_CONFLICT", "预览任务未运行", 409)
        pid, owner, attempt = job.project_id, job.created_by, job.attempt
        payload = deepcopy(job.input_json)
        outcomes = deepcopy(job.result_json.get("items", {}))

    def fence(s):
        row = s.get(Job, jid, populate_existing=True)
        if (
            not row
            or row.attempt != attempt
            or row.state != "running"
            or row.cancel_requested
            or not row.lease_until
            or row.lease_until <= now()
        ):
            raise DomainError("VKS_CANCELLED", "本次预览已停止", 409)
        actor = current_actor(service, owner, pid, s, job=row)
        service.require(s, actor, pid, "knowledge:edit")
        analysis = s.get(Analysis, payload["analysis_set_id"], populate_existing=True)
        if not analysis or analysis.selection_version != payload["selection_version"]:
            raise DomainError("VKS_VERSION_CONFLICT", "生成期间资料或关联已变化，请重新预览", 412)
        return actor

    def one(item):
        with ReadingService(service).db() as s:
            actor = fence(s)
        form = ReassociationInput.model_validate({**item["form"], "batch_id": jid, "item_id": item["item_id"]})
        try:
            answer = engine.reassociate(actor, pid, form, preview=True, validate_scope=fence)
            return {
                "status": "generated" if answer.get("can_confirm") else "insufficient",
                "preview_id": answer.get("preview_id"),
            }
        except DomainError as error:
            if error.code in {"VKS_CANCELLED", "VKS_UNAUTHENTICATED", "VKS_FORBIDDEN", "VKS_VERSION_CONFLICT"}:
                raise
            return {"status": "failed", "error_code": error.code}

    def persist(identity, value):
        with service.db() as s:
            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            row = s.scalar(select(Job).where(Job.id == jid).with_for_update().execution_options(populate_existing=True))
            fence(s)
            outcomes[identity] = value
            row.result_json = {"items": deepcopy(outcomes), "processed": len(outcomes), "total": len(payload["items"])}
            row.progress = min(99, int(len(outcomes) * 100 / len(payload["items"])))
            row.stage = "PREVIEW"
            row.updated_at = now()

    # A worker may die after the immutable candidate commits and before its
    # progress receipt commits. Recover that exact preview, never regenerate it.
    with ReadingService(service).db() as s:
        actor = fence(s)
        recovered = {}
        for artifact in s.scalars(
            select(Artifact)
            .where(
                Artifact.project_id == pid,
                Artifact.metadata_json["batch_id"].as_string() == jid,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ):
            identity = artifact.metadata_json.get("item_id")
            if identity not in outcomes and identity not in recovered:
                engine._read_reassociation_preview(s, actor, pid, artifact.id)
                recovered[identity] = {"status": "generated", "preview_id": artifact.id}
    for identity, value in recovered.items():
        persist(identity, value)

    remaining = iter(item for item in payload["items"] if item["item_id"] not in outcomes)
    capacity = request_concurrency(payload.get("concurrency", 3))
    with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="vks-association-preview") as pool:
        pending = {}

        def admit():
            item = next(remaining, None)
            if item is not None:
                pending[pool.submit(one, item)] = item["item_id"]
                return True
            return False

        for _ in range(capacity):
            if not admit():
                break
        try:
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    identity = pending.pop(future)
                    persist(identity, future.result())
                    admit()
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return {"items": outcomes, "processed": len(outcomes), "total": len(payload["items"])}


def install_reassociation_batches(app, service, actor, envelope):
    shared = ReassociationBatchService(service)
    ActorDep = Annotated[object, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/interactive/reassociate"

    @app.get(prefix + "/targets", response_model=PrototypeEnvelope[dict])
    def targets(
        pid: str,
        analysis_set_id: str,
        request: Request,
        a: ActorDep,
        module_only: bool = True,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=200),
        mode: Literal["all", "missing"] = "all",
        q: str = Query(default="", max_length=200),
    ):
        return envelope(request, shared.targets(a, pid, analysis_set_id, module_only, offset, limit, mode, q))

    @app.post(prefix + "/batch-preview", response_model=PrototypeEnvelope[dict])
    def create(
        pid: str,
        payload: BatchPreviewInput,
        request: Request,
        a: ActorDep,
        if_match: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, max_length=96),
    ):
        return envelope(request, shared.enqueue(a, pid, payload, if_match, idempotency_key))

    @app.get(prefix + "/batch-preview/{jid}", response_model=PrototypeEnvelope[dict])
    def listing(
        pid: str,
        jid: str,
        request: Request,
        a: ActorDep,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return envelope(request, shared.listing(a, pid, jid, offset, limit))

    @app.post(prefix + "/batch-preview/{jid}/results", response_model=PrototypeEnvelope[dict])
    def query_results(pid: str, jid: str, payload: BatchResultQuery, request: Request, a: ActorDep):
        return envelope(request, shared.listing(a, pid, jid, payload.offset, payload.limit, filters=payload))

    @app.post(prefix + "/batch-confirm", response_model=PrototypeEnvelope[dict])
    def confirm(pid: str, payload: BatchConfirmInput, request: Request, a: ActorDep):
        return envelope(request, shared.confirm(a, pid, payload))
