"""Model navigation retains every sent source, including uncited evidence."""

from test_model_gateway import system as system  # pytest fixture registration

from copy import deepcopy
from dataclasses import replace
import json
import pytest
from sqlalchemy import select
from test_task_planning import create, plan
from vks_api.journeys import JourneyService
from vks_api.models import Artifact, Evidence, Relation, Revision, uid
from vks_api.service import DomainError, sha


@pytest.mark.parametrize("restriction,scope", [("confidential", "source:read"), ("trace", "trace:read")])
def test_model_action_list_is_navigation_and_uncited_inputs_stay_restricted_after_edit(system, restriction, scope):
    service, actor, project, aid, _, requests, response, _ = system
    pid = project["id"]
    raw = b"Synthetic uncited evidence with an independent classification"
    with service.db() as s:
        art = Artifact(
            id=uid(),
            project_id=pid,
            snapshot_id=uid(),
            relative_path="proof/unbound.txt",
            sha256=sha(raw),
            size_bytes=len(raw),
            kind="knowledge",
            media_type="text/plain",
            object_key=service._put_object(raw),
            metadata_json={"allow_platform_llm": True},
        )
        s.add(art)
        s.flush()
        evidence = Evidence(
            id=uid(),
            project_id=pid,
            analysis_set_id=aid,
            artifact_id=art.id,
            stable_key="proof.unbound",
            evidence_kind="explicit",
            content_sha256=sha(raw),
            locator={"path": "proof/unbound.txt"},
            excerpt=raw.decode(),
        )
        s.add(evidence)
        s.flush()
        relation = s.scalar(select(Relation).where(Relation.analysis_set_id == aid))
        relation.evidence_ids = [*relation.evidence_ids, evidence.id]
        input_artifact_id, uncited_evidence_id = (art.id, evidence.id)
    tasks, job = create(system, "model-navigation")

    def navigation(context):
        value = plan(context)
        step = next((row for row in value["steps"] if row["tool"] == "propose_journey"))
        step["target_revision_ids"] = [row["revision_id"] for row in context["entities"] if row["type"] == "action"][:3]
        step["evidence_ids"] = [
            row["evidence_id"] for row in context["evidence"] if row["evidence_id"] != uncited_evidence_id
        ][:1]
        value["steps"] = [step]
        value["title"] = next(
            (row["excerpt"] for row in context["evidence"] if row["evidence_id"] == uncited_evidence_id)
        )
        return value

    response["plan"] = navigation
    service.process_job(job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert len(requests) == 1
    sent = json.loads(requests[0][2]["input"][1]["content"])
    assert any((row["evidence_id"] == uncited_evidence_id for row in sent["evidence"]))
    assert all((uncited_evidence_id not in row["evidence_ids"] for row in result["report"]["plan_steps"]))
    journeys = JourneyService(service)
    limited = replace(actor, scopes=actor.scopes - {scope})
    row = journeys.list(limited, pid, aid)[0]
    assert row["dsl"]["semantics"]["kind"] == "evidence_navigation"
    assert row["dsl"]["semantics"]["basis"] == "inferred"
    assert set(result["report"]["plan_steps"][0]["target_revision_ids"]) == {
        node["binding"]["revision_id"] for node in row["dsl"]["nodes"] if node.get("binding")
    }
    assert "No verified runtime chain" in row["dsl"]["semantics"]["unknowns"]
    by_key = {node["key"]: node for node in row["dsl"]["nodes"]}
    assert not any(
        (by_key[edge["source"]]["kind"] == by_key[edge["target"]]["kind"] == "action" for edge in row["dsl"]["edges"])
    )
    with service.db() as s:
        assert not s.scalar(select(Revision).where(Revision.artifact_id == input_artifact_id))
        artifact = s.get(Artifact, input_artifact_id)
        if restriction == "confidential":
            artifact.sensitivity = "confidential"
        else:
            artifact.kind = "trace"
    assert not journeys.list(limited, pid, aid)
    with pytest.raises(DomainError):
        journeys.get(limited, pid, row["journey_id"], row["revision_id"])
    edited = deepcopy(row["dsl"])
    edited["title"] += " edited"
    edited["semantics"]["purpose"] = "编辑后的说明仍来自固定模型输入"
    edited["nodes"][1]["description"] = "此说明的原始来源仍受限"
    saved = journeys.update(
        actor, row["journey_id"], dict(analysis_set_id=aid, dsl=edited), row["revision_id"], "edit-title"
    )
    assert not journeys.list(limited, pid, aid)
    with pytest.raises(DomainError):
        journeys.get(limited, pid, saved["journey_id"], saved["revision_id"])
    assert journeys.get(actor, pid, row["journey_id"], row["revision_id"]) == row


def test_known_model_journey_without_input_provenance_is_quarantined(system):
    service, actor, project, aid, _, _, response, _ = system
    tasks, job = create(system, "missing-model-provenance")
    response["plan"] = lambda context: dict(
        plan(context), steps=[next((row for row in plan(context)["steps"] if row["tool"] == "propose_journey"))]
    )
    service.process_job(job["id"])
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "succeeded"
    row = JourneyService(service).list(actor, project["id"], aid)[0]
    with service.db() as s:
        art = s.get(Artifact, s.get(Revision, row["revision_id"]).artifact_id)
        art.metadata_json = dict(
            art.metadata_json, journey_input_artifacts=[], requires_source_read=False, requires_trace_read=False
        )
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    assert not JourneyService(service).list(limited, project["id"], aid)


def structured_plan(context, bind_action=True):
    value = plan(context)
    step = next((row for row in value["steps"] if row["tool"] == "propose_journey"))
    action = step["target_revision_ids"][0]
    source = next((row["revision_id"] for row in context["entities"] if row["type"] in ("document", "section")))

    def node(key, kind, title, description=None, target=None):
        return dict(key=key, kind=kind, title=title, description=description, target_revision_id=target)

    step["target_revision_ids"] = [action] if bind_action else []
    step["journey"] = dict(
        purpose="提交开票申请",
        preconditions=["具有测试角色", "申请数据已准备"],
        outcome="得到申请受理或校验失败的说明",
        entry="prepare",
        source_revision_ids=[source],
        nodes=[
            node("prepare", "step", "准备申请", "依据固定手册整理申请条件；独立页面动作尚未导入。"),
            node("check", "decision", "检查申请条件", "来源中的条件被整理为推断分支。"),
            node(
                "submit",
                "action" if bind_action else "step",
                "提交申请",
                "请求申请受理；业务成功仍需真实验收。",
                action if bind_action else None,
            ),
            node("accepted", "step", "查看受理结果", "受理结果为材料描述，尚未采集独立运行状态。"),
            node("invalid", "step", "查看校验提示", "校验不通过时核对输入条件。"),
            node("end", "end", "结束说明"),
        ],
        edges=[
            dict(source="prepare", target="check", label="条件已准备"),
            dict(source="check", target="submit", label="条件满足"),
            dict(source="check", target="invalid", label="校验不通过"),
            dict(source="submit", target="accepted", label="受理结果"),
            dict(source="accepted", target="end", label=""),
            dict(source="invalid", target="end", label=""),
        ],
    )
    value["steps"] = [step]
    return value


@pytest.mark.parametrize("bind_action", [True, False])
def test_structured_model_business_journey_is_confirmed_as_a_plan_then_resumes_once(system, monkeypatch, bind_action):
    from vks_api.knowledge_tasks import KnowledgeTaskService

    service, actor, project, aid, _, requests, response, _ = system
    tasks, job = create(system, "structured-flow")
    response["plan"] = lambda context: structured_plan(context, bind_action)
    original = KnowledgeTaskService._propose
    attempts = []

    def interrupted(self, *args):
        attempts.append(args[0])
        if len(attempts) == 1:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic proposal interruption", 503)
        return original(self, *args)

    monkeypatch.setattr(KnowledgeTaskService, "_propose", interrupted)
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "failed"
    assert not JourneyService(service).list(actor, project["id"], aid)
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert len(requests) == 1 and len(attempts) == 2
    rows = JourneyService(service).list(actor, project["id"], aid)
    assert len(rows) == 1
    dsl = rows[0]["dsl"]
    assert dsl["semantics"]["kind"] == "business_process" and dsl["semantics"]["basis"] == "inferred"
    assert dsl["semantics"]["purpose"] == "提交开票申请"
    assert dsl["semantics"]["preconditions"] == ["具有测试角色", "申请数据已准备"]
    assert dsl["semantics"]["outcome"] == "得到申请受理或校验失败的说明"
    assert all(("binding" not in node and node["description"] for node in dsl["nodes"] if node["kind"] == "step"))
    assert {edge["label"] for edge in dsl["edges"] if edge["source"] == "check"} == {"条件满足", "校验不通过"}
    sent = json.loads(requests[0][2]["input"][1]["content"])
    assert dsl["semantics"]["source_refs"][0]["revision_id"] in {row["revision_id"] for row in sent["entities"]}
    assert result["report"]["saved_directly"] and result["report"]["model_calls"] == 1


@pytest.mark.parametrize(
    "case",
    [
        "forged_source",
        "wrong_binding_type",
        "cycle",
        "blank_description",
        "no_source",
        "forged_basis",
        "script",
        "coordinates",
        "ambiguous_branch",
        "duplicate_node",
        "wrong_action_scope",
        "no_goal",
        "node_budget",
        "edge_budget",
        "no_business_steps",
    ],
)
def test_structured_model_proposals_reject_fabrication_unsafe_fields_and_invalid_dags_before_mutations(system, case):
    service, actor, project, aid, _, requests, response, _ = system
    tasks, job = create(system, "bad-structured-flow")

    def invalid(context):
        value = structured_plan(context)
        step = value["steps"][0]
        graph = step["journey"]
        if case == "forged_source":
            graph["source_revision_ids"] = ["unknown"]
        elif case == "wrong_binding_type":
            graph["nodes"][2]["target_revision_id"] = graph["source_revision_ids"][0]
        elif case == "cycle":
            graph["edges"].append(dict(source="accepted", target="prepare", label="loop"))
        elif case == "blank_description":
            graph["nodes"][0]["description"] = "  "
        elif case == "no_source":
            graph["source_revision_ids"] = []
        elif case == "forged_basis":
            graph["basis"] = "observed"
        elif case == "script":
            graph["nodes"][0]["script"] = "fetch('https://example.invalid')"
        elif case == "coordinates":
            graph["nodes"][0]["x"] = 100
        elif case == "ambiguous_branch":
            graph["edges"][1]["label"] = graph["edges"][2]["label"]
        elif case == "duplicate_node":
            graph["nodes"][1]["key"] = graph["nodes"][0]["key"]
        elif case == "wrong_action_scope":
            step["target_revision_ids"] = []
        elif case == "no_goal":
            graph["purpose"] = "  "
        elif case == "node_budget":
            graph["nodes"] = graph["nodes"] * 21
        elif case == "edge_budget":
            graph["edges"] = graph["edges"] * 41
        elif case == "no_business_steps":
            graph["nodes"] = [graph["nodes"][-1]]
            graph["entry"] = "end"
            graph["edges"] = []
            step["target_revision_ids"] = []
        return value

    response["plan"] = invalid
    before = deepcopy(service.workspace(actor, project["id"], aid)["relations"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "failed", result
    assert len(requests) == 1 and result["report"] is None
    assert not JourneyService(service).list(actor, project["id"], aid)
    assert service.workspace(actor, project["id"], aid)["relations"] == before


def test_structured_plan_rechecks_input_classification_before_commit(system, monkeypatch):
    from vks_api.knowledge_tasks import KnowledgeTaskService

    service, actor, project, aid, _, requests, response, _ = system
    tasks, job = create(system, "structured-classification-change")
    response["plan"] = structured_plan
    original = KnowledgeTaskService._propose

    def changed(self, *args):
        result = original(self, *args)
        source_id = args[4]["_model_plan"]["steps"][0]["journey"]["source_revision_ids"][0]
        with service.db() as session:
            source = session.get(Revision, source_id)
            session.get(Artifact, source.artifact_id).sensitivity = "confidential"
        return result

    monkeypatch.setattr(KnowledgeTaskService, "_propose", changed)
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_VERSION_CONFLICT", result
    assert len(requests) == 1 and result["report"] is None
    assert not JourneyService(service).list(actor, project["id"], aid)
