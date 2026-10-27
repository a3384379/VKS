"""Bounded, recoverable knowledge jobs. Deterministic templates work without a model."""

from .v2_contracts import Envelope, JobOutput, TaskOutput, ModuleAccepted

import json
from typing import Literal
from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .models import Analysis, Artifact, Audit, Job, uid, now
from .service import DomainError, canonical, sha
from .jobs import current_actor
from .reading import ReadingService

STAGES = ["Plan", "Collect", "Analyze", "Propose", "Validate", "Review"]
KINDS = Literal[
    "project_overview",
    "module_analysis",
    "business_flow",
    "evidence_gaps",
    "document_draft",
    "difference_review",
    "knowledge_plan",
]
MODEL_KINDS = ("document_draft", "knowledge_plan")


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    task_kind: KINDS
    instruction: str = Field(default="", max_length=4000)
    action_ids: list[str] = Field(default_factory=list, max_length=30)
    max_entities: int = Field(default=100, ge=1, le=200)
    max_model_calls: int = Field(default=0, ge=0, le=1)
    from_release_id: str | None = None


class ModuleRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_keys: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)


class KnowledgeTaskService:
    def __init__(self, service):
        self.service = service

    def create(self, actor, pid, payload, key):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "analysis:run")
            self.service.require(s, actor, pid, "knowledge:edit")
            analysis = s.get(Analysis, payload["analysis_set_id"])
            if not analysis or analysis.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到草稿基线", 404)
            if analysis.state == "frozen":
                raise DomainError("VKS_STATE_CONFLICT", "请克隆发布版后创建知识任务", 409)
            if payload["task_kind"] == "difference_review":
                from .models import Release

                release = s.get(Release, payload.get("from_release_id")) if payload.get("from_release_id") else None
                if not release or release.project_id != pid:
                    raise DomainError("VKS_NOT_FOUND", "差异任务需要选择同项目固定发布版", 404)
            if payload["task_kind"] in MODEL_KINDS:
                from .ai_gateway import ModelPolicy

                policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                if payload.get("max_model_calls", 0) != 1:
                    raise DomainError("VKS_LIMIT_EXCEEDED", "文档任务需要显式授权一次模型调用")
                if not policy or not policy.settings.get("enabled"):
                    raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "模型未启用，仍可运行普通知识模板", 503)
                if payload["task_kind"] == "knowledge_plan" and not payload.get("instruction", "").strip():
                    raise DomainError("VKS_INVALID_INPUT", "自然语言任务需要明确的目标和范围")

            def create():
                revisions = self.service._revisions(s, analysis.id)
                visible = [r for r in revisions if self.service._visible_revision(s, actor, pid, r)]
                visible_ids = {r.entity_id for r in visible}
                if set(payload.get("action_ids", [])) - visible_ids:
                    raise DomainError("VKS_NOT_FOUND", "任务动作不在可见基线", 404)
                pinned = dict(
                    payload,
                    _selection_version=analysis.selection_version,
                    _input_revision_ids=sorted(r.id for r in visible),
                    _stage_history=[{"stage": "Plan", "state": "complete", "at": now()}],
                )
                if payload["task_kind"] in MODEL_KINDS:
                    pinned["_model_policy_sha256"] = sha(
                        canonical({"settings": policy.settings, "version": policy.row_version})
                    )
                if payload["task_kind"] == "knowledge_plan":
                    from .task_planning import TOOLS

                    pinned["_plan_tools"] = [
                        tool
                        for tool in TOOLS
                        if tool != "propose_relation" or self.service._can(s, actor, pid, "links:propose")
                    ]
                    pinned["_stage_history"][0]["state"] = "waiting"
                if payload["task_kind"] == "difference_review":
                    from .models import Revision

                    pinned["_historical_revision_ids"] = [
                        item["entity_revision_id"]
                        for item in release.manifest_json["selections"]
                        if self.service._visible_revision(s, actor, pid, s.get(Revision, item["entity_revision_id"]))
                    ]
                return self.service.job_dto(self.service._new_job(s, actor, pid, "knowledge_task", pinned))

            return self.service.idem(s, actor, pid, "knowledge_task.create", key, payload, create)

    def _validate(self, s, job):
        actor = current_actor(self.service, job.created_by, job.project_id, s, job=job)
        self.service.require(s, actor, job.project_id, "analysis:run")
        self.service.require(s, actor, job.project_id, "knowledge:edit")
        analysis = s.get(Analysis, job.input_json["analysis_set_id"])
        if job.input_json.get("task_kind") in MODEL_KINDS:
            from .ai_gateway import ModelPolicy

            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == job.project_id))
            if (
                not policy
                or not policy.settings.get("enabled")
                or sha(canonical({"settings": policy.settings, "version": policy.row_version}))
                != job.input_json.get("_model_policy_sha256")
            ):
                raise DomainError("VKS_VERSION_CONFLICT", "模型策略或授权已变化，请创建新任务", 412)
        if (
            not analysis
            or analysis.state == "frozen"
            or analysis.selection_version != job.input_json["_selection_version"]
        ):
            raise DomainError("VKS_VERSION_CONFLICT", "任务固定基线已变化，请重新预检创建任务", 412)
        revisions = {r.id: r for r in self.service._revisions(s, analysis.id)}
        if any(
            rid not in revisions or not self.service._visible_revision(s, actor, job.project_id, revisions[rid])
            for rid in job.input_json["_input_revision_ids"]
        ):
            raise DomainError("VKS_FORBIDDEN", "任务输入权限已变化，结果被丢弃", 403)
        from .models import Revision

        if any(
            not self.service._visible_revision(s, actor, job.project_id, s.get(Revision, rid))
            for rid in job.input_json.get("_historical_revision_ids", [])
        ):
            raise DomainError("VKS_FORBIDDEN", "任务历史输入权限已变化，结果被丢弃", 403)
        if (
            job.cancel_requested
            or job.state not in ("running", "cancelling")
            or (job.lease_until and job.lease_until <= now())
        ):
            raise DomainError("VKS_CANCELLED", "任务已取消或租约已失效", 409)
        return actor, analysis

    def _stage(self, jid, stage, attempt):
        with self.service.db() as s:
            job = s.get(Job, jid)
            if job.attempt != attempt:
                raise DomainError("VKS_CANCELLED", "旧任务 attempt 已失效", 409)
            self._validate(s, job)
            previous = [dict(row) for row in job.input_json.get("_stage_history", [])]
            if previous and previous[-1]["state"] == "running":
                previous[-1]["state"] = "interrupted"
            history = [*previous, {"stage": stage, "state": "running", "at": now()}]
            job.input_json = dict(job.input_json, _stage_history=history[-30:])
            job.stage = stage.upper()

    def _collect(self, actor, pid, aid, payload):
        w = self.service.workspace(actor, pid, aid)
        # Stage artifacts are bounded summaries, not another copy of every source body.
        limit = payload["max_entities"]
        required = set(payload.get("action_ids", []))
        required_revisions = {
            rid for step in payload.get("_model_plan", {}).get("steps", []) for rid in step["target_revision_ids"]
        }

        def summary(row):
            fields = (
                "id",
                "entity_id",
                "entity_revision_id",
                "stable_key",
                "type",
                "name",
                "content_sha256",
                "artifact_id",
                "locator",
            )
            item = {k: row[k] for k in fields if k in row}
            item["metadata"] = {
                k: value
                for k, value in row.get("metadata", {}).items()
                if k
                in (
                    "page_key",
                    "pageKey",
                    "service_key",
                    "http_method",
                    "method",
                    "normalized_route",
                    "path",
                    "source_kind",
                    "discovery_basis",
                    "evidence_origin",
                    "runtime_source_alignment",
                )
            }
            return item

        output = {
            key: w[key]
            for key in ("project", "selected_analysis_set", "selected_release", "sources", "unknowns", "stats")
        }
        for key in ("entities", "pages", "actions", "scenarios", "documents"):
            ordered = sorted(
                w[key],
                key=lambda row: (
                    row.get("entity_id") not in required and row.get("entity_revision_id") not in required_revisions,
                    row.get("entity_id", ""),
                ),
            )
            # Explicit ordered actions/model targets are retained even with a small preview budget.
            chosen = [
                row
                for i, row in enumerate(ordered)
                if i < limit or row.get("entity_id") in required or row.get("entity_revision_id") in required_revisions
            ]
            output[key] = [summary(row) for row in chosen]
        if payload["task_kind"] == "module_analysis":
            artifact_ids = {row.get("artifact_id") for row in output["entities"]} - {None}
            with self.service.db() as s:
                origins = {
                    art.id: (
                        art.relative_path,
                        next(
                            (
                                source["source_id"]
                                for source in w["sources"]
                                if source["source_snapshot_id"] == art.snapshot_id
                            ),
                            "",
                        ),
                    )
                    for art in s.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids)))
                }
            for row in output["entities"]:
                if row.get("artifact_id") in origins:
                    row["source_path"], row["source_key"] = origins[row["artifact_id"]]
        action_revisions = {row["entity_revision_id"] for row in output["actions"]}
        trusted = [
            row for row in w["relations"] if row["review_status"] == "confirmed" and row["validity_status"] == "valid"
        ]
        output["_connected_actions"] = sorted(
            action_revisions & {rid for row in trusted for rid in (row["from_revision_id"], row["to_revision_id"])}
        )
        output["_review_relation_ids"] = [
            row["id"]
            for row in w["relations"]
            if row["review_status"] == "candidate" or row["validity_status"] != "valid"
        ][:200]
        output["relations"] = [
            row
            for row in w["relations"]
            if row["from_revision_id"] in action_revisions or row["to_revision_id"] in action_revisions
        ][:200]
        output["evidence"] = [{k: value for k, value in row.items() if k != "content"} for row in w["evidence"][:200]]
        output["_scope"] = {
            key: {"total": len(w[key]), "included": len(output[key]), "is_truncated": len(w[key]) > len(output[key])}
            for key in ("entities", "pages", "actions", "scenarios", "documents", "relations", "evidence")
        }
        return output

    def _analyze(self, actor, pid, aid, payload, w):
        from .workbench_v2 import coverage

        report = {
            "task_kind": payload["task_kind"],
            "analysis_set_id": aid,
            "source_vector": w["sources"],
            "coverage": coverage(self.service, actor, pid, aid),
            "unknowns": w["unknowns"],
            "candidate_only": True,
            "model_calls": 0,
            "entities": w["entities"][: payload["max_entities"]],
            "is_truncated": any(row["is_truncated"] for row in w["_scope"].values()),
            "scope": w["_scope"],
        }
        if payload["task_kind"] == "module_analysis":
            from .module_discovery import candidates

            report["module_candidates"] = candidates(w)
        if payload["task_kind"] == "evidence_gaps":
            trusted = [
                r for r in w["relations"] if r["review_status"] == "confirmed" and r["validity_status"] == "valid"
            ]
            connected = set(w.get("_connected_actions", [])) | {
                rid for row in trusted for rid in (row["from_revision_id"], row["to_revision_id"])
            }
            report["uncovered_actions"] = [
                dict(entity_id=row["id"], revision_id=row["entity_revision_id"], name=row["name"])
                for row in w["actions"]
                if row["entity_revision_id"] not in connected
            ][: payload["max_entities"]]
            report["requires_review_relation_ids"] = w["_review_relation_ids"]
        if payload["task_kind"] == "difference_review":
            report["change_report"] = self.service.diff(actor, pid, payload["from_release_id"], aid)
        return report

    def _model_output(self, jid, operation):
        from .task_checkpoints import TaskCheckpoints

        with self.service.db() as s:
            job = s.get(Job, jid)
            self._validate(s, job)
            attempt = job.attempt
            inputs = dict(
                revisions=job.input_json["_input_revision_ids"], policy=job.input_json["_model_policy_sha256"]
            )

        def reserved_call():
            with self.service.db() as s:
                job = s.get(Job, jid)
                self._validate(s, job)
                if job.attempt != attempt or job.input_json.get("_model_reserved"):
                    raise DomainError("VKS_LIMIT_EXCEEDED", "先前模型调用结果不确定；本任务的一次调用额度已用尽")
                job.input_json = dict(job.input_json, _model_reserved=True)
            return operation()

        return TaskCheckpoints(self, jid, attempt).run("ModelResult", inputs, reserved_call, track=False)

    def _plan(self, jid, actor, pid, aid, payload):
        from .ai_gateway import AiService
        from .task_planning import PlanExecutor

        result = self._model_output(
            jid,
            lambda: AiService(self.service).run(
                actor,
                pid,
                payload["instruction"],
                analysis_set_id=aid,
                knowledge_plan=True,
                plan_tools=payload["_plan_tools"],
                plan_entity_limit=payload["max_entities"],
            ),
        )
        if "steps" not in result or "_catalog" not in result:
            raise DomainError("VKS_INVALID_EVIDENCE", "当前无充分且获准的计划输入，请补充证据或调整资料策略")
        PlanExecutor(self, jid, None).validate(result)
        return result

    def _propose(self, jid, actor, pid, aid, payload, w, report):
        from copy import deepcopy

        report = deepcopy(report)
        if payload["task_kind"] == "knowledge_plan":
            from .task_planning import proposals

            report.update(proposals(payload["_model_plan"], jid, w))
            report["_knowledge_plan"] = payload["_model_plan"]
            report["tool_results"] = w.get("tool_results", [])
        if payload["task_kind"] == "business_flow":
            actions = {r["entity_id"]: r for r in w["actions"]}
            if (
                not payload["action_ids"]
                or len(set(payload["action_ids"])) != len(payload["action_ids"])
                or any(eid not in actions for eid in payload["action_ids"])
            ):
                raise DomainError("VKS_INVALID_INPUT", "业务流程模板需要明确有序且无重复的动作范围")
            report["journey_dsl"] = dict(
                schema_version="2.0",
                key="task-" + jid,
                title="业务流程候选",
                entry="step-0",
                nodes=[
                    dict(
                        key="step-" + str(i),
                        title=actions[eid]["name"],
                        kind="action",
                        x=40 + (i % 4) * 240,
                        y=40 + (i // 4) * 140,
                        binding={"entity_id": eid, "revision_id": actions[eid]["entity_revision_id"]},
                    )
                    for i, eid in enumerate(payload["action_ids"])
                ]
                + [
                    dict(
                        key="done",
                        title="结束",
                        kind="end",
                        x=40,
                        y=160 + ((len(payload["action_ids"]) - 1) // 4) * 140,
                    )
                ],
                edges=[
                    dict(
                        key="edge-" + str(i),
                        source="step-" + str(i),
                        target="step-" + str(i + 1) if i + 1 < len(payload["action_ids"]) else "done",
                        label="",
                    )
                    for i in range(len(payload["action_ids"]))
                ],
            )
            report["unknowns"] = [*report["unknowns"], "顺序由任务发起人声明，尚未证明真实业务执行顺序"]
        if payload["task_kind"] == "document_draft":
            from .ai_gateway import AiService

            report["_document_output"] = self._model_output(jid, lambda: AiService(self.service).prepare_document(jid))
            report["model_calls"] = 1
        return report

    def _validate_report(self, jid, report):
        with self.service.db() as s:
            job = s.get(Job, jid)
            actor, analysis = self._validate(s, job)
            if report.get("_knowledge_plan"):
                from .task_planning import PlanExecutor

                PlanExecutor(self, jid, None).validate(report["_knowledge_plan"], s)
            if report.get("candidate_only") is not True or report.get("analysis_set_id") != analysis.id:
                raise DomainError("VKS_INVALID_EVIDENCE", "阶段产物不属于当前候选基线")
            if "journey_dsl" in report:
                from .journeys import JourneyService

                JourneyService(self.service)._validate(s, actor, analysis, report["journey_dsl"])
            if "_document_output" in report:
                from .ai_gateway import AiService

                AiService(self.service).validate_inputs(
                    s, actor, job.project_id, report["_document_output"].get("_input_provenance", [])
                )
            return {"validated": True}

    def prepare(self, jid):
        from .task_checkpoints import TaskCheckpoints

        with self.service.db() as s:
            job = s.get(Job, jid)
            actor, analysis = self._validate(s, job)
            attempt, pid, aid, payload = job.attempt, job.project_id, analysis.id, dict(job.input_json)
        checkpoints = TaskCheckpoints(self, jid, attempt)
        if payload["task_kind"] == "knowledge_plan":
            payload["_model_plan"] = checkpoints.run(
                "Plan", payload["_input_revision_ids"], lambda: self._plan(jid, actor, pid, aid, payload)
            )

        def collect():
            w = self._collect(actor, pid, aid, payload)
            if payload.get("_model_plan"):
                from .task_planning import PlanExecutor

                w["tool_results"] = PlanExecutor(self, jid, checkpoints).collect(payload["_model_plan"])
            return w

        w = checkpoints.run(
            "Collect", dict(revisions=payload["_input_revision_ids"], plan=payload.get("_model_plan")), collect
        )
        report = checkpoints.run("Analyze", w, lambda: self._analyze(actor, pid, aid, payload, w))
        proposal = checkpoints.run("Propose", report, lambda: self._propose(jid, actor, pid, aid, payload, w, report))
        checkpoints.run("Validate", proposal, lambda: self._validate_report(jid, proposal))
        # Always revalidate after the final cached stage, before the job's atomic commit.
        self._validate_report(jid, proposal)
        return proposal

    def finalize(self, s, job, report):
        actor, analysis = self._validate(s, job)
        from .journeys import JourneyService
        from .models import Entity, Revision

        result = {"analysis_set_id": analysis.id, "requires_review": True, "result_refs": []}
        if "_document_output" in report:
            from .ai_gateway import AiService

            result.update(AiService(self.service).finalize_document(s, job, report.pop("_document_output")))
        if "journey_dsl" in report:
            journeys = JourneyService(self.service)
            dsl, refs = journeys._validate(s, actor, analysis, report["journey_dsl"])
            entity = Entity(id=uid(), project_id=job.project_id, type="journey", stable_key=dsl["key"])
            s.add(entity)
            s.flush()
            row = journeys._save(s, actor, analysis, entity, dsl, refs)
            result["result_refs"].append({"kind": "journey_revision", "id": row["revision_id"]})
        if report.get("relation_candidates"):
            from .models import Relation

            self.service.require(s, actor, job.project_id, "links:propose")
            changed = False
            for candidate in report["relation_candidates"]:
                row = s.scalar(
                    select(Relation).where(
                        Relation.analysis_set_id == analysis.id,
                        Relation.from_revision_id == candidate["from_revision_id"],
                        Relation.to_revision_id == candidate["to_revision_id"],
                        Relation.relation_type == candidate["relation_type"],
                    )
                )
                if not row:
                    row = Relation(
                        id=uid(),
                        project_id=job.project_id,
                        analysis_set_id=analysis.id,
                        review_status="candidate",
                        validity_status="valid",
                        row_version=1,
                        **{k: v for k, v in candidate.items() if k not in ("key", "review_status")},
                    )
                    self.service._verify_relation(s, row)
                    s.add(row)
                    s.flush()
                    changed = True
                candidate.update(
                    relation_id=row.id,
                    review_status=row.review_status,
                    manual_decision_preserved=row.review_status != "candidate",
                )
            if changed:
                analysis.selection_version += 1
        plan = report.pop("_knowledge_plan", None)
        if plan:
            self.service.audit(
                s,
                actor,
                job.project_id,
                "knowledge_plan.commit",
                job.id,
                metadata={"model_usage_id": plan["usage_id"], "step_ids": [step["id"] for step in plan["steps"]]},
            )
        raw = canonical(report).encode()
        all_input_ids = sorted(
            set(job.input_json["_input_revision_ids"]) | set(job.input_json.get("_historical_revision_ids", []))
        )
        revisions = [s.get(Revision, rid) for rid in all_input_ids]
        artifacts = [s.get(Artifact, row.artifact_id) for row in revisions if row.artifact_id]
        from .reading import artifact_policy

        policies = [artifact_policy(s, art) for art in artifacts]
        artifact = Artifact(
            id=uid(),
            project_id=job.project_id,
            snapshot_id=uid(),
            relative_path="task-reports/" + job.id + ".json",
            sha256=sha(raw),
            size_bytes=len(raw),
            kind="knowledge",
            media_type="application/json",
            object_key=self.service._put_object(raw),
            metadata_json={
                "journey_binding_revisions": all_input_ids,
                "requires_source_read": any(
                    s.get(Entity, row.entity_id).type in ("symbol", "sql", "table") for row in revisions
                )
                or any(art.kind == "source" or art.sensitivity == "confidential" for art in artifacts)
                or any(p.get("requires_source_read") for p in policies),
                "requires_trace_read": any(art.kind in ("trace", "capture") for art in artifacts)
                or any(p.get("requires_trace_read") for p in policies),
            },
        )
        s.add(artifact)
        s.flush()
        result["report_artifact_id"] = artifact.id
        result["result_refs"].append({"kind": "task_report", "id": artifact.id})
        history = [dict(row) for row in job.input_json["_stage_history"]]
        if history and history[-1]["state"] == "running":
            history[-1]["state"] = "complete"
        job.input_json = dict(
            job.input_json,
            _stage_history=[
                *history,
                {"stage": "Review", "state": "waiting_for_human", "at": now()},
            ],
        )
        return result

    def get(self, actor, jid, pid=None):
        dto = self.service.get_job(actor, jid, pid)
        with self.service.db() as s:
            job = s.get(Job, jid)
            if job.kind != "knowledge_task":
                raise DomainError("VKS_NOT_FOUND", "未找到知识任务", 404)
            dto.update(
                task_kind=job.input_json["task_kind"],
                stages=STAGES,
                stage_history=job.input_json.get("_stage_history", []),
                budget={"max_model_calls": job.input_json.get("max_model_calls", 0)},
                analysis_set_id=job.input_json["analysis_set_id"],
                report=None,
                stage_artifacts=[],
                module_decisions={},
            )
            if self._can_read_stages(s, actor, job):
                from .jobs import Checkpoint

                for point in s.scalars(
                    select(Checkpoint)
                    .where(Checkpoint.job_id == jid, Checkpoint.stage.like("KNOWLEDGE_%"))
                    .order_by(Checkpoint.stage)
                ):
                    try:
                        packet, raw = self._read_stage(point)
                    except DomainError:
                        continue  # A damaged cache must not hide the job/retry controls.
                    dto["stage_artifacts"].append(dict(stage=packet["stage"], sha256=sha(raw), size_bytes=len(raw)))
            artifact = (
                s.get(Artifact, job.result_json.get("report_artifact_id"))
                if job.result_json.get("report_artifact_id")
                else None
            )
            if artifact:
                from .reading import artifact_policy

                policy = artifact_policy(s, artifact)
                if ReadingService.policy_visible(
                    artifact.kind,
                    artifact.sensitivity,
                    policy,
                    self.service._can(s, actor, job.project_id, "source:read"),
                    self.service._can(s, actor, job.project_id, "trace:read"),
                ):
                    raw = (self.service.data_dir / "objects" / artifact.object_key).read_bytes()
                    if sha(raw) != artifact.sha256:
                        raise DomainError("VKS_STATE_CONFLICT", "任务报告摘要不匹配", 409)
                    dto["report"] = json.loads(raw)
                    if job.input_json["task_kind"] == "module_analysis":
                        from .modules import BusinessModule

                        keys = {row["key"] for row in dto["report"].get("module_candidates", [])}
                        for module in s.scalars(
                            select(BusinessModule).where(
                                BusinessModule.project_id == job.project_id, BusinessModule.key.in_(keys)
                            )
                        ):
                            dto["module_decisions"][module.key] = "accepted"
                        for decision in s.scalars(
                            select(Audit).where(
                                Audit.project_id == job.project_id,
                                Audit.resource_id == job.id,
                                Audit.action == "module.proposal.reject",
                            )
                        ):
                            if decision.metadata_json.get("report_sha256") == artifact.sha256:
                                for key in decision.metadata_json["candidate_keys"]:
                                    dto["module_decisions"].setdefault(key, "rejected")
                else:
                    dto["diagnostics_restricted"] = True
            return dto

    def _can_read_stages(self, s, actor, job):
        from .models import Revision

        return all(
            self.service._can(s, actor, job.project_id, scope)
            for scope in ("knowledge:edit", "source:read", "trace:read")
        ) and all(
            self.service._visible_revision(s, actor, job.project_id, s.get(Revision, rid))
            for rid in [
                *job.input_json.get("_input_revision_ids", []),
                *job.input_json.get("_historical_revision_ids", []),
            ]
        )

    def _read_stage(self, point):
        from .task_checkpoints import MAX_STAGE_BYTES

        key = point.result_json.get("object_key", "")
        try:
            if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                raise ValueError()
            path = self.service.data_dir / "objects" / key
            if path.stat().st_size > MAX_STAGE_BYTES:
                raise ValueError()
            raw = path.read_bytes()
            packet = json.loads(raw)
            if (
                sha(raw) != key
                or packet["input_hash"] != point.input_hash
                or packet["processor"] != point.processor_version
                or "KNOWLEDGE_" + packet["stage"].upper() != point.stage
                or not isinstance(packet["result"], dict)
            ):
                raise ValueError()
            return packet, raw
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainError("VKS_STATE_CONFLICT", "阶段产物完整性校验未通过", 409) from None

    def stage_artifact(self, actor, jid, digest):
        self.service.get_job(actor, jid)
        with self.service.db() as s:
            job = s.get(Job, jid)
            if job.kind != "knowledge_task" or not self._can_read_stages(s, actor, job):
                raise DomainError("VKS_NOT_FOUND", "未找到当前可见阶段产物", 404)
            from .jobs import Checkpoint

            for point in s.scalars(
                select(Checkpoint).where(Checkpoint.job_id == jid, Checkpoint.stage.like("KNOWLEDGE_%"))
            ):
                if point.result_json.get("object_key") == digest:
                    packet, _ = self._read_stage(point)
                    return dict(stage=packet["stage"], sha256=digest, result=packet["result"])
            raise DomainError("VKS_NOT_FOUND", "未找到当前可见阶段产物", 404)

    def accept_module(self, actor, jid, candidate_key, key):
        with self.service.db() as s:
            job = s.get(Job, jid)
            if (
                not job
                or job.kind != "knowledge_task"
                or job.input_json.get("task_kind") != "module_analysis"
                or job.state != "succeeded"
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到模块候选任务", 404)
            self.service.require(s, actor, job.project_id, "knowledge:edit")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "采用模块分组需要人员身份", 403)
            analysis = s.get(Analysis, job.input_json["analysis_set_id"])
            if analysis.state == "frozen" or analysis.selection_version != job.input_json["_selection_version"]:
                raise DomainError("VKS_VERSION_CONFLICT", "模块候选的固定基线已变化，请重新分析", 412)
            from .models import Revision

            if any(
                not self.service._visible_revision(s, actor, job.project_id, s.get(Revision, rid))
                for rid in job.input_json["_input_revision_ids"]
            ):
                raise DomainError("VKS_NOT_FOUND", "候选输入不在当前权限范围", 404)

            def accept():
                artifact = s.get(Artifact, job.result_json["report_artifact_id"])
                if any(
                    candidate_key in decision.metadata_json.get("candidate_keys", [])
                    and decision.metadata_json.get("report_sha256") == artifact.sha256
                    for decision in s.scalars(
                        select(Audit).where(
                            Audit.project_id == job.project_id,
                            Audit.resource_id == jid,
                            Audit.action == "module.proposal.reject",
                        )
                    )
                ):
                    raise DomainError("VKS_STATE_CONFLICT", "此候选已由人员拒绝，请基于新任务重新评估", 409)
                raw = (self.service.data_dir / "objects" / artifact.object_key).read_bytes()
                if sha(raw) != artifact.sha256:
                    raise DomainError("VKS_STATE_CONFLICT", "候选报告摘要不匹配", 409)
                report = json.loads(raw)
                candidate = next(
                    (row for row in report.get("module_candidates", []) if row["key"] == candidate_key), None
                )
                if not candidate:
                    raise DomainError("VKS_NOT_FOUND", "未找到模块候选", 404)
                from .modules import BusinessModule, ModuleEntity

                if s.scalar(
                    select(BusinessModule).where(
                        BusinessModule.project_id == job.project_id, BusinessModule.key == candidate_key
                    )
                ):
                    raise DomainError("VKS_STATE_CONFLICT", "模块已存在，请调整已有分组", 409)
                module = BusinessModule(
                    id=uid(),
                    project_id=job.project_id,
                    key=candidate_key,
                    name=candidate["title"][:160],
                    description=candidate["reason"],
                    sort_order=0,
                )
                s.add(module)
                s.flush()
                for eid in candidate["entity_ids"]:
                    s.add(ModuleEntity(module_id=module.id, entity_id=eid))
                self.service.audit(
                    s,
                    actor,
                    job.project_id,
                    "module.proposal.accept",
                    module.id,
                    metadata={"job_id": job.id, "report_sha256": artifact.sha256},
                )
                return {"module_id": module.id, "key": candidate_key, "analysis_set_id": analysis.id}

            return self.service.idem(
                s,
                actor,
                job.project_id,
                "module.proposal.accept",
                key,
                {"job_id": jid, "candidate_key": candidate_key},
                accept,
            )

    def reject_modules(self, actor, jid, payload, key):
        value = self.get(actor, jid)
        if not value.get("report") or value["task_kind"] != "module_analysis" or value["state"] != "succeeded":
            raise DomainError("VKS_NOT_FOUND", "未找到当前可见模块候选", 404)
        keys = payload["candidate_keys"]
        if len(keys) != len(set(keys)) or set(keys) - {
            row["key"] for row in value["report"].get("module_candidates", [])
        }:
            raise DomainError("VKS_INVALID_INPUT", "拒绝范围必须为本报告中无重复的准确候选 key")
        with self.service.db() as s:
            job = s.get(Job, jid)
            self.service.require(s, actor, job.project_id, "knowledge:edit")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "拒绝模块候选需要人员身份", 403)
            analysis = s.get(Analysis, job.input_json["analysis_set_id"])
            if analysis.state == "frozen" or analysis.selection_version != job.input_json["_selection_version"]:
                raise DomainError("VKS_VERSION_CONFLICT", "模块候选基线已变化，请重新分析", 412)

            def reject():
                from .modules import BusinessModule
                from .models import Revision

                if any(
                    not self.service._visible_revision(s, actor, job.project_id, s.get(Revision, rid))
                    for rid in job.input_json["_input_revision_ids"]
                ):
                    raise DomainError("VKS_NOT_FOUND", "候选输入已不在当前可见范围", 404)
                if s.scalar(
                    select(BusinessModule.id).where(
                        BusinessModule.project_id == job.project_id, BusinessModule.key.in_(keys)
                    )
                ):
                    raise DomainError("VKS_STATE_CONFLICT", "已采用的模块应在模块管理中调整", 409)
                artifact = s.get(Artifact, job.result_json["report_artifact_id"])
                self.service.audit(
                    s,
                    actor,
                    job.project_id,
                    "module.proposal.reject",
                    jid,
                    metadata=dict(candidate_keys=keys, reason=payload["reason"], report_sha256=artifact.sha256),
                )
                return dict(candidate_keys=keys, decision="rejected", report_sha256=artifact.sha256)

            return self.service.idem(s, actor, job.project_id, "module.proposal.reject", key, payload, reject)


def install_tasks(app, service, actor, envelope):
    tasks = KnowledgeTaskService(service)

    @app.post(
        "/api/v2/projects/{pid}/knowledge-tasks",
        status_code=202,
        response_model=Envelope[JobOutput],
        response_model_exclude_unset=True,
    )
    def create(pid: str, payload: TaskInput, request: Request, a=Depends(actor), idempotency_key: str = Header(...)):
        return envelope(request, tasks.create(a, pid, payload.model_dump(), idempotency_key))

    @app.get("/api/v2/knowledge-tasks/{jid}", response_model=Envelope[TaskOutput], response_model_exclude_unset=True)
    def get(jid: str, request: Request, project_id: str | None = None, a=Depends(actor)):
        return envelope(request, tasks.get(a, jid, project_id))

    @app.get("/api/v2/knowledge-tasks/{jid}/stage-artifacts/{digest}", response_model=Envelope[dict])
    def stage(jid: str, digest: str, request: Request, a=Depends(actor)):
        return envelope(request, tasks.stage_artifact(a, jid, digest))

    @app.post("/api/v2/knowledge-tasks/{jid}/module-proposals/reject", response_model=Envelope[dict])
    def reject(
        jid: str, payload: ModuleRejection, request: Request, a=Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, tasks.reject_modules(a, jid, payload.model_dump(), idempotency_key))

    @app.post(
        "/api/v2/knowledge-tasks/{jid}/module-proposals/{candidate_key}/accept", response_model=Envelope[ModuleAccepted]
    )
    def accept(jid: str, candidate_key: str, request: Request, a=Depends(actor), idempotency_key: str = Header(...)):
        return envelope(request, tasks.accept_module(a, jid, candidate_key, idempotency_key))
