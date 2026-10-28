"""A bounded, evidence-citing model plan; the server owns every executable tool."""

from jsonschema import Draft202012Validator
from .models import Evidence, Relation, Revision
from .service import DomainError

TOOLS = ["inspect_action", "inspect_evidence", "propose_relation", "propose_journey", "draft_document"]
RELATIONS = ["may_call", "depends_on", "documents", "implements"]
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "steps", "unknowns"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "unknowns": {"type": "array", "maxItems": 30, "items": {"type": "string", "maxLength": 2000}},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "tool",
                    "target_revision_ids",
                    "evidence_ids",
                    "relation_type",
                    "rationale",
                    "markdown",
                ],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,23}$"},
                    "tool": {"type": "string", "enum": TOOLS},
                    "target_revision_ids": {
                        "type": "array",
                        "maxItems": 30,
                        "uniqueItems": True,
                        "items": {"type": "string", "maxLength": 36},
                    },
                    "evidence_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                        "items": {"type": "string", "maxLength": 36},
                    },
                    "relation_type": {"anyOf": [{"type": "string", "enum": RELATIONS}, {"type": "null"}]},
                    "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "markdown": {"type": ["string", "null"], "maxLength": 12000},
                },
            },
        },
    },
}


def validate_plan(answer, catalog, evidence_ids, tools):
    Draft202012Validator(PLAN_SCHEMA).validate(
        {
            k: v
            for k, v in answer.items()
            if not k.startswith("_") and k not in ("model_id", "usage_id", "analysis_set_id")
        }
    )
    seen = set()
    singletons = set()
    kinds = {row["revision_id"]: row["type"] for row in catalog}
    for step in answer["steps"]:
        if step["id"] in seen or step["tool"] not in tools:
            raise DomainError("VKS_INVALID_INPUT", "模型计划重复或请求了未授予的工具")
        seen.add(step["id"])
        ids = step["target_revision_ids"]
        tool = step["tool"]
        if set(ids) - set(kinds) or set(step["evidence_ids"]) - set(evidence_ids):
            raise DomainError("VKS_INVALID_EVIDENCE", "模型计划引用了未提供或无权的实体/证据")
        if tool == "inspect_action" and (len(ids) != 1 or kinds[ids[0]] != "action"):
            raise DomainError("VKS_INVALID_INPUT", "动作工具需要一个准确动作修订")
        if (
            tool == "inspect_evidence"
            and ids
            or tool == "propose_relation"
            and (len(ids) != 2 or step["relation_type"] not in RELATIONS)
        ):
            raise DomainError("VKS_INVALID_INPUT", "证据/关系步骤参数不合法")
        if tool == "propose_journey" and (not ids or any(kinds[rid] != "action" for rid in ids)):
            raise DomainError("VKS_INVALID_INPUT", "流程步骤只能使用提供的准确动作修订")
        if tool == "draft_document" and (ids or not step["markdown"] or not step["markdown"].strip()):
            raise DomainError("VKS_INVALID_INPUT", "文档步骤必须是有引用的独立草稿")
        if (
            tool != "propose_relation"
            and step["relation_type"] is not None
            or tool != "draft_document"
            and step["markdown"] is not None
        ):
            raise DomainError("VKS_INVALID_INPUT", "工具参数不能混入其他操作")
        if tool in ("propose_journey", "draft_document"):
            if tool in singletons:
                raise DomainError("VKS_LIMIT_EXCEEDED", "一次计划至多生成一个流程及一份文档")
            singletons.add(tool)
    return answer


class PlanExecutor:
    def __init__(self, tasks, jid, checkpoints):
        self.tasks, self.service, self.jid, self.checkpoints = tasks, tasks.service, jid, checkpoints

    def validate(self, plan, session=None, allow_suspended=False):
        if session is None:
            with self.service.db() as s:
                return self.validate(plan, s, allow_suspended)
        s = session
        from .models import Job
        from .ai_gateway import AiService

        job = s.get(Job, self.jid)
        actor, analysis = self.tasks._validate(s, job, allow_suspended=allow_suspended)
        AiService(self.service).validate_inputs(s, actor, job.project_id, plan["_input_provenance"])
        validate_plan(
            plan,
            plan["_catalog"],
            [r["evidence_id"] for r in plan["_input_provenance"]],
            job.input_json["_plan_tools"],
        )
        selected = {r.id: r for r in self.service._revisions(s, analysis.id)}
        for row in plan["_catalog"]:
            rev = selected.get(row["revision_id"])
            if not rev or not self.service._visible_revision(s, actor, job.project_id, rev):
                raise DomainError("VKS_FORBIDDEN", "计划输入已失效或无权", 403)
        for step in plan["steps"]:
            if step["tool"] == "propose_relation":
                self.service.require(s, actor, job.project_id, "links:propose")
                self.service._verify_relation(
                    s,
                    Relation(
                        project_id=job.project_id,
                        analysis_set_id=analysis.id,
                        from_revision_id=step["target_revision_ids"][0],
                        to_revision_id=step["target_revision_ids"][1],
                        relation_type=step["relation_type"],
                        evidence_ids=step["evidence_ids"],
                    ),
                )
        return actor, analysis.id

    def collect(self, plan):
        outputs = []
        for step in plan["steps"]:
            if step["tool"] not in ("inspect_action", "inspect_evidence"):
                continue
            result = self.checkpoints.run(
                "TOOL_" + step["id"],
                dict(step=step, plan_provenance=plan["_input_provenance"]),
                lambda: self.read(step, plan),
                track=False,
            )
            outputs.append(dict(step_id=step["id"], tool=step["tool"], state="complete", result=result))
        return outputs

    def read(self, step, plan):
        actor, aid = self.validate(plan)
        from .models import Job

        with self.service.db() as s:
            job = s.get(Job, self.jid)
            pid = job.project_id
            if step["tool"] == "inspect_action":
                rid = step["target_revision_ids"][0]
                entity = s.get(Revision, rid).entity_id
            else:
                rows = []
                for eid in step["evidence_ids"]:
                    row = s.get(Evidence, eid)
                    if not row or not self.service._visible_evidence(s, actor, pid, row):
                        raise DomainError("VKS_NOT_FOUND", "未找到当前可见证据", 404)
                    rows.append(
                        dict(
                            evidence_id=eid,
                            artifact_id=row.artifact_id,
                            content_sha256=row.content_sha256,
                            locator=row.locator,
                            evidence_kind=row.evidence_kind,
                        )
                    )
                return {"evidence": rows}
        from .workbench_v2 import explanation

        result = explanation(self.service, actor, pid, entity, aid)
        result["entities"] = [
            {k: v for k, v in row.items() if k not in ("content", "metadata")} for row in result["entities"]
        ]
        return result


def proposals(plan, jid, w):
    report = {
        "model_calls": 1,
        "plan_title": plan["title"],
        "plan_steps": plan["steps"],
        "unknowns": plan["unknowns"],
        "model_id": plan["model_id"],
        "model_usage_id": plan["usage_id"],
        "relation_candidates": [],
    }
    actions = {row["entity_revision_id"]: row for row in w["actions"]}
    for step in plan["steps"]:
        if step["tool"] == "propose_relation":
            report["relation_candidates"].append(
                dict(
                    key=step["id"],
                    from_revision_id=step["target_revision_ids"][0],
                    to_revision_id=step["target_revision_ids"][1],
                    relation_type=step["relation_type"],
                    evidence_ids=step["evidence_ids"],
                    causal_basis="not_established",
                    reason="AI 语义候选：" + step["rationale"],
                    review_status="candidate",
                )
            )
        if step["tool"] == "propose_journey":
            chosen = [actions[rid] for rid in step["target_revision_ids"]]
            report["journey_dsl"] = dict(
                schema_version="2.0",
                key="task-" + jid,
                title=plan["title"],
                entry="step-0",
                nodes=[
                    dict(
                        key="step-" + str(i),
                        kind="action",
                        title=row["name"],
                        x=40 + (i % 4) * 240,
                        y=40 + (i // 4) * 140,
                        binding=dict(entity_id=row["entity_id"], revision_id=row["entity_revision_id"]),
                    )
                    for i, row in enumerate(chosen)
                ]
                + [dict(key="end", kind="end", title="结束", x=40, y=160 + len(chosen) // 4 * 140)],
                edges=[
                    dict(
                        key="edge-" + str(i),
                        source="step-" + str(i),
                        target="step-" + str(i + 1) if i + 1 < len(chosen) else "end",
                        label="模型建议顺序 · 待审核",
                    )
                    for i in range(len(chosen))
                ],
            )
        if step["tool"] == "draft_document":
            report["_document_output"] = dict(
                title=plan["title"],
                paragraphs=[dict(markdown=step["markdown"], evidence_ids=step["evidence_ids"])],
                unknowns=plan["unknowns"],
                model_id=plan["model_id"],
                usage_id=plan["usage_id"],
                analysis_set_id=plan["analysis_set_id"],
                _selection_version=plan["_selection_version"],
                _input_provenance=plan["_input_provenance"],
            )
    return report
