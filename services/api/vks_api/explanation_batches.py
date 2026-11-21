"""Saved prototype explanations and durable batches that fill missing ones."""

import json
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from math import ceil, floor
from typing import Annotated

from fastapi import Depends, Header, Request
from pydantic import Field
from sqlalchemy import func, select

from .ai_gateway import ModelPolicy, ModelUsage
from .association_limits import host_request_ceiling
from .association_pressure import ASSOCIATION_PRESSURE
from .exports import allow_artifact, canonical, digest, read_original
from .interactive import InteractiveKnowledgeService
from .jobs import current_actor, job_timing
from .materials import MaterialService
from .models import Analysis, Artifact, Job, Project, Revision, now
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref, _artifact, version
from .reading import ReadingService, chunks, revision_policy
from .reassociation_batches import MIGRATION_BLOCKED, pending_migration
from .service import DomainError, future

KIND = "explanation_batch"
ACTIVE = ("queued", "running", "retry_wait", "cancelling")


def saved_explanations(service, s, pid, bindings, caps, *, cutoff=None):
    """Return the saved text only when the exact binding and full source set match."""
    # Saving a Studio version clones relation rows. The immutable point and
    # exact prototype anchor survive that clone, but the relation ID does not.
    by_anchor = {(row["point_revision_id"], anchor_digest(row)): row for row in bindings}
    found = {}
    for batch in chunks({point for point, _ in by_anchor}):
        query = select(Artifact).where(
            Artifact.project_id == pid,
            Artifact.kind == "generated",
            Artifact.relative_path.startswith("prototype-explanations/"),
            Artifact.metadata_json["point_revision_id"].as_string().in_(batch),
        )
        if cutoff:
            query = query.where(Artifact.created_at <= cutoff)
        for artifact in s.scalars(query.order_by(Artifact.created_at.desc(), Artifact.id.desc())):
            meta = artifact.metadata_json
            binding = by_anchor.get((meta.get("point_revision_id"), meta.get("anchor_digest")))
            if (
                not binding
                or binding["id"] in found
                or meta.get("section_ids") != binding["section_ids"]
                or not allow_artifact(artifact, caps, s)
            ):
                continue
            data = json.loads(read_original(service, artifact))
            if not data["section_ids"] or not set(data["section_ids"]) <= set(binding["section_ids"]):
                continue
            found[binding["id"]] = (artifact.id, data)
    return found


def anchor_digest(binding):
    return digest(canonical([binding["package_id"], binding["target"]]))


def binding_reference(binding):
    return {
        "id": binding["id"],
        "point_revision_id": binding["point_revision_id"],
        "section_ids": [section["revision_id"] for section in binding["sections"]],
        "package_id": binding["package_id"],
        "target": binding["target"],
    }


def readable_capabilities(service, s, actor, pid):
    source = service._can(s, actor, pid, "source:read")
    return {
        "allowed_source": source,
        "allowed_confidential": source,
        "allowed_trace": service._can(s, actor, pid, "trace:read"),
    }


def read_explanation(engine, s, actor, pid, form):
    binding = engine._binding(actor, pid, form, session=s)
    if not binding or binding["excluded"] or binding["validity_status"] != "valid":
        return None
    found = saved_explanations(
        engine.service, s, pid, [binding_reference(binding)], readable_capabilities(engine.service, s, actor, pid)
    )
    if binding["id"] not in found:
        return None
    _, data = found[binding["id"]]
    refs = {section["revision_id"]: section for section in binding["sections"]}
    analysis, _ = engine.links.reader.reading.baseline(s, actor, pid, form.analysis_set_id, form.release_id)
    return {
        **{key: data[key] for key in ("title", "explanation", "unknowns", "confidence", "model_id")},
        "sections": [refs[rid] for rid in data["section_ids"]],
        "cached": True,
        "binding_id": None,
        "saved": False,
        "selection_version": analysis.selection_version,
        "retrieval": "已保存 AI 讲解",
    }


class ExplanationBatchInput(Input):
    analysis_set_id: Ref
    binding_ids: list[Ref] = Field(min_length=1, max_length=10000)


class HistoryRange(Input):
    lower_seconds: int
    upper_seconds: int


class ExplanationBatchState(Input):
    job_id: Ref
    state: str
    processed: int
    total: int
    saved: int
    reused: int
    failed: int
    timing: dict
    history: HistoryRange | None = None


def current_model(s, pid):
    policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
    return policy.settings.get("model_id") if policy and policy.settings.get("enabled") else None


def explanation_history(s, pid, aid, owner, model_id, pending):
    """Estimate execution time from recent, comparable generation runs."""
    if not model_id or not pending:
        return None
    history = s.scalars(
        select(Job)
        .where(
            Job.project_id == pid,
            Job.created_by == owner,
            Job.kind == KIND,
            Job.state == "succeeded",
            Job.attempt == 1,
            Job.input_json["analysis_set_id"].as_string() == aid,
            Job.updated_at >= future(-7 * 24 * 3600),
        )
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(12)
    )
    estimates = []
    for job in history:
        counts = Counter(job.result_json.get("items", {}).values())
        work = counts["saved"] + counts["failed"]
        start = job.input_json.get("_run_started_at")
        if (
            not start
            or not counts["saved"]
            or pending > work * 2
            or work > pending * 2
            or counts["reused"] > work
            or counts["failed"] > work / 5
        ):
            continue
        # Older batches did not stamp a model ID. Require real ledger records
        # from the same actor and interval, with no evidence of mixed models.
        used = dict(
            s.execute(
                select(ModelUsage.model_id, func.count())
                .where(
                    ModelUsage.project_id == pid,
                    ModelUsage.actor_id == owner,
                    ModelUsage.created_at >= start,
                    ModelUsage.created_at <= job.updated_at,
                    ModelUsage.state.in_(("failed", "succeeded")),
                )
                .group_by(ModelUsage.model_id)
            ).all()
        )
        if len(used) != 1 or used.get(model_id, 0) < counts["saved"]:
            continue
        duration = (datetime.fromisoformat(job.updated_at) - datetime.fromisoformat(start)).total_seconds()
        if duration > 0:
            estimates.append(duration * pending / work)
        if len(estimates) == 5:
            break
    if not estimates:
        return None
    # A single historical run still gives a rough reference, with more room
    # for changing concurrency and one-time setup than several samples need.
    low, high = (0.7, 1.5) if len(estimates) < 3 else (0.85, 1.2)
    return {"lower_seconds": max(1, floor(min(estimates) * low)), "upper_seconds": ceil(max(estimates) * high)}


def latest(s, pid, aid, owner):
    return s.scalars(
        select(Job)
        .where(
            Job.project_id == pid,
            Job.created_by == owner,
            Job.kind == KIND,
            Job.input_json["analysis_set_id"].as_string() == aid,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    ).first()


def install_explanation_batches(app, service, actor, envelope):
    links = InteractiveKnowledgeService(service)
    ActorDep = Annotated[object, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/interactive/explanation-batches"

    @app.post(prefix, status_code=202, response_model=PrototypeEnvelope[ExplanationBatchState])
    def create(
        pid: str,
        request: Request,
        a: ActorDep,
        payload: ExplanationBatchInput,
        expected: Annotated[str, Header(alias="If-Match")],
        key: Annotated[str, Header(alias="Idempotency-Key", max_length=96)],
    ):
        with service.db() as s:
            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            analysis = MaterialService(service)._draft(s, a, pid, payload.analysis_set_id)
            bindings = {row["id"]: row for row in links.bindings(a, pid, analysis.id, session=s)}
            items = {}
            for binding_id in dict.fromkeys(payload.binding_ids):
                binding = bindings.get(binding_id)
                if (
                    not binding
                    or binding["excluded"]
                    or binding["validity_status"] != "valid"
                    or not binding["sections"]
                ):
                    raise DomainError("VKS_VERSION_CONFLICT", "所选关联已变化或没有有效引用，请刷新后重试", 412)
                items[binding_id] = binding_reference(binding)

            caps = readable_capabilities(service, s, a, pid)
            full_scope = caps["allowed_source"] and caps["allowed_trace"]

            def apply():
                version(analysis.selection_version, expected)
                if pending_migration(s, pid, analysis.id):
                    raise DomainError("VKS_STATE_CONFLICT", MIGRATION_BLOCKED, 409)
                previous = latest(s, pid, analysis.id, a.id)
                if previous and previous.state in ACTIVE:
                    raise DomainError("VKS_STATE_CONFLICT", "当前草稿已有批量讲解任务，请查看进度", 409)
                model_id = current_model(s, pid)
                history = None
                if previous and model_id and full_scope:
                    existing = saved_explanations(service, s, pid, list(items.values()), caps)
                    history = explanation_history(s, pid, analysis.id, a.id, model_id, len(items) - len(existing))
                job = service._new_job(
                    s,
                    a,
                    pid,
                    KIND,
                    {
                        "analysis_set_id": analysis.id,
                        "selection_version": analysis.selection_version,
                        "items": items,
                        **({"_history_range": history, "_history_model": model_id} if history else {}),
                    },
                )
                return batch_state(job, set(items), model_id=model_id)

            response = service.idem(s, a, pid, KIND, key, payload.model_dump(), apply)
            return envelope(request, response if full_scope else {**response, "history": None})

    @app.get(prefix + "/latest", response_model=PrototypeEnvelope[ExplanationBatchState | None])
    def status(pid: str, request: Request, a: ActorDep, analysis_set_id: Ref):
        with ReadingService(service).db() as s:
            service.require(s, a, pid, "knowledge:edit")
            analysis, _, _, _ = links.reader.scope(s, a, pid, analysis_set_id, None)
            job = latest(s, pid, analysis.id, a.id)
            if not job:
                return envelope(request, None)
            items = set(job.input_json["items"])
            # A narrowed grant must not see progress attributable to sources it
            # can no longer read. Full project access needs no list projection.
            caps = readable_capabilities(service, s, a, pid)
            if not (caps["allowed_source"] and caps["allowed_trace"]):
                items.intersection_update(
                    row["id"]
                    for row in links.bindings(a, pid, analysis.id, session=s)
                    if row["id"] in items
                    and all(
                        value == job.input_json["items"][row["id"]][key]
                        for key, value in binding_reference(row).items()
                    )
                )
            model_id = (
                current_model(s, pid)
                if (job.input_json.get("_history_range") and caps["allowed_source"] and caps["allowed_trace"])
                else None
            )
            return envelope(request, batch_state(job, items, model_id=model_id))


def batch_state(job, visible, *, model_id=None):
    counts = Counter(value for key, value in job.result_json.get("items", {}).items() if key in visible)
    timing = job_timing(job) if len(visible) == len(job.input_json["items"]) else {}
    history = (
        job.input_json.get("_history_range")
        if timing and model_id and model_id == job.input_json.get("_history_model")
        else None
    )
    baseline = sum(counts.values()) if job.state == "queued" else timing.get("baseline", 0)
    if history and baseline:
        fraction = max(0, len(visible) - baseline) / len(visible)
        history = {key: max(1, ceil(value * fraction)) for key, value in history.items()}
    return {
        "job_id": job.id,
        "state": job.state,
        "processed": sum(counts.values()),
        "total": len(visible),
        "saved": counts["saved"],
        "reused": counts["reused"],
        "failed": counts["failed"],
        "timing": timing,
        "history": history,
    }


def run_explanation_batch(service, jid):
    from .interactive_ai import ExplanationInput, InteractiveAssistant

    reading = ReadingService(service)
    engine = InteractiveAssistant(service)
    with reading.db() as s:
        job = s.get(Job, jid)
        pid, owner, attempt = job.project_id, job.created_by, job.attempt
        payload = job.input_json
        outcomes = dict(job.result_json.get("items", {}))

    aid = payload["analysis_set_id"]

    def fence(s):
        job = s.get(Job, jid, populate_existing=True)
        if (
            not job
            or job.attempt != attempt
            or job.state != "running"
            or job.cancel_requested
            or not job.lease_until
            or job.lease_until <= now()
        ):
            raise DomainError("VKS_CANCELLED", "批量讲解已停止", 409)
        actor = current_actor(service, owner, pid, s, job=job)
        service.require(s, actor, pid, "knowledge:edit")
        analysis = s.get(Analysis, aid, populate_existing=True)
        if not analysis or analysis.selection_version != payload["selection_version"] or pending_migration(s, pid, aid):
            raise DomainError("VKS_VERSION_CONFLICT", "资料或关联已变化，请刷新后重新选择", 412)
        return actor, job

    def generate(item):
        with reading.db() as s:
            actor, _ = fence(s)
            if saved_explanations(service, s, pid, [item], readable_capabilities(service, s, actor, pid)):
                return "reused", None
        target = item["target"]
        form = ExplanationInput(
            analysis_set_id=aid,
            package_id=item["package_id"],
            target=target,
            context={"scope": "module" if target["selector"] == "body" else "control", "page_title": target["label"]},
        )

        def validate(s):
            principal, _ = fence(s)
            binding = engine._binding(principal, pid, form, session=s)
            if (
                not binding
                or binding["excluded"]
                or binding["validity_status"] != "valid"
                or binding_reference(binding) != item
            ):
                raise DomainError("VKS_VERSION_CONFLICT", "讲解目标或原文权限已变化", 412)

        try:
            with ASSOCIATION_PRESSURE.request():
                with reading.db() as s:
                    validate(s)
                answer = engine.run(actor, pid, form, validate_scope=validate)
        except DomainError as exc:
            if exc.details.get("upstream_status") == 429:
                ASSOCIATION_PRESSURE.throttled()
            if exc.code in {"VKS_CANCELLED", "VKS_VERSION_CONFLICT", "VKS_FORBIDDEN", "VKS_UNAUTHENTICATED"}:
                raise
            return "failed", None
        citations = [row["revision_id"] for row in answer["sections"]]
        if (
            not citations
            or answer["confidence"] < 0.7
            or not answer["explanation"].strip()
            or not set(citations) <= set(item["section_ids"])
        ):
            return "failed", None
        return "saved", {
            **{key: answer[key] for key in ("title", "explanation", "unknowns", "confidence", "model_id")},
            "section_ids": citations,
        }

    def persist(item, status, data):
        with service.db() as s:
            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            s.scalar(select(Job).where(Job.id == jid).with_for_update())
            actor, job = fence(s)
            if data:
                caps = readable_capabilities(service, s, actor, pid)
                if saved_explanations(service, s, pid, [item], caps):
                    status = "reused"
                else:
                    rows = [s.get(Revision, rid) for rid in [item["point_revision_id"], *item["section_ids"]]]
                    if not all(service._visible_revision(s, actor, pid, row) for row in rows):
                        raise DomainError("VKS_VERSION_CONFLICT", "讲解依据的读取权限已变化", 412)
                    pack = s.get(Artifact, item["package_id"])
                    _artifact(
                        s,
                        service,
                        pid,
                        pack.snapshot_id,
                        "prototype-explanations",
                        canonical(data),
                        "generated",
                        "application/json",
                        {
                            "anchor_digest": anchor_digest(item),
                            "point_revision_id": item["point_revision_id"],
                            "section_ids": item["section_ids"],
                            "input_revision_provenance": [
                                {
                                    "revision_id": row.id,
                                    "entity_id": row.entity_id,
                                    "sha256": row.content_sha256,
                                    "artifact_id": row.artifact_id,
                                    **revision_policy(s, row),
                                }
                                for row in rows
                            ],
                        },
                    )
            outcomes[item["id"]] = status
            job.result_json = result()
            job.progress = min(99, int(len(outcomes) * 100 / len(payload["items"])))
            job.stage = "EXPLAIN"
            job.updated_at = now()

    def result():
        return {"items": dict(outcomes), "processed": len(outcomes), "total": len(payload["items"])}

    remaining = iter(item for item in payload["items"].values() if item["id"] not in outcomes)
    capacity = host_request_ceiling()
    with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="vks-explanation") as pool:
        pending = {pool.submit(generate, item): item for _, item in zip(range(capacity), remaining)}
        try:
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    persist(item, *future.result())
                    if next_item := next(remaining, None):
                        pending[pool.submit(generate, next_item)] = next_item
        finally:
            for future in pending:
                future.cancel()
    return result()
