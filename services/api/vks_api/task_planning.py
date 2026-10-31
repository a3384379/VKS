"""A bounded, evidence-citing model plan; the server owns every executable tool."""

from copy import deepcopy

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

# The richer proposal is an additive strict-schema branch: old seven-field plan
# steps remain valid and no optional-property exception is needed by providers.
BUSINESS_JOURNEY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["purpose", "preconditions", "outcome", "entry", "nodes", "edges", "source_revision_ids"],
    "properties": {
        "purpose": {"type": "string", "minLength": 1, "maxLength": 2000},
        "preconditions": {
            "type": "array",
            "maxItems": 30,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "outcome": {"type": "string", "minLength": 1, "maxLength": 2000},
        "entry": {"type": "string", "minLength": 1, "maxLength": 96},
        "source_revision_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 36},
        },
        "nodes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 120,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "kind", "title", "description", "target_revision_id"],
                "properties": {
                    "key": {"type": "string", "pattern": "^[a-zA-Z0-9_.-]{1,96}$"},
                    "kind": {
                        "type": "string",
                        "enum": ["page", "action", "step", "decision", "branch", "end", "reference"],
                    },
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {"type": ["string", "null"], "minLength": 1, "maxLength": 2000},
                    "target_revision_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 36},
                },
            },
        },
        "edges": {
            "type": "array",
            "maxItems": 240,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "target", "label"],
                "properties": {
                    "source": {"type": "string", "minLength": 1, "maxLength": 96},
                    "target": {"type": "string", "minLength": 1, "maxLength": 96},
                    "label": {"type": "string", "maxLength": 160},
                },
            },
        },
    },
}
_legacy_step = PLAN_SCHEMA["properties"]["steps"]["items"]
_business_step = deepcopy(_legacy_step)
_business_step["required"].append("journey")
_business_step["properties"]["tool"] = {"type": "string", "enum": ["propose_journey"]}
_business_step["properties"]["journey"] = BUSINESS_JOURNEY_SCHEMA
PLAN_SCHEMA["properties"]["steps"]["items"] = {"anyOf": [_legacy_step, _business_step]}


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
        if tool == "propose_journey":
            if step.get("journey") is not None:
                from .journey_generation import planned_business_journey

                planned_business_journey(
                    step["journey"], catalog, "model-plan-validation", answer["title"], answer["unknowns"]
                )
                bound_actions = {
                    node["target_revision_id"] for node in step["journey"]["nodes"] if node["kind"] == "action"
                }
                if set(ids) != bound_actions:
                    raise DomainError("VKS_INVALID_INPUT", "业务导览动作范围必须与准确绑定的动作一致")
            elif not ids or any(kinds[rid] != "action" for rid in ids):
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
        AiService(self.service).validate_inputs(
            s, actor, job.project_id, plan["_input_provenance"], plan.get("_input_revision_provenance")
        )
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
                    excluded=False,
                )
            )
        if step["tool"] == "propose_journey":
            from .journey_generation import action_evidence_navigation, planned_business_journey

            if step.get("journey") is not None:
                report["journey_dsl"] = planned_business_journey(
                    step["journey"], plan["_catalog"], "task-" + jid, plan["title"], plan["unknowns"]
                )
            else:
                chosen = [actions[rid] for rid in step["target_revision_ids"]]
                report["journey_dsl"] = action_evidence_navigation(
                    "task-" + jid, plan["title"], chosen, basis="inferred"
                )
            report["journey_dsl"]["semantics"]["unknowns"] = list(
                dict.fromkeys(
                    [
                        *(item for item in plan["unknowns"] if item.strip()),
                        *report["journey_dsl"]["semantics"]["unknowns"],
                    ]
                )
            )
            report["unknowns"] = list(
                dict.fromkeys([*report["unknowns"], *report["journey_dsl"]["semantics"]["unknowns"]])
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
                _input_revision_provenance=plan.get("_input_revision_provenance", []),
            )
    return report
