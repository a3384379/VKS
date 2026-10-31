"""Journey meaning is explicit and independent of ordering, names and review."""

from test_journeys import store as store  # pytest fixture registration

from copy import deepcopy
from dataclasses import replace
import pytest
from test_journeys import journey
from packages.journey_dsl import validate_journey
from vks_api.journeys import JourneyService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Artifact, Revision
from vks_api.service import DomainError


def test_old_journey_round_trips_without_inventing_business_meaning(store):
    service, actor, pid, w, _, _ = store
    dsl = journey(w)
    dsl["title"] = "运行状态对比 / 业务流程"
    assert validate_journey(dsl) == dsl
    saved = JourneyService(service).create(
        actor, pid, dict(analysis_set_id=w["selected_analysis_set"]["id"], dsl=dsl), "old"
    )
    assert saved["dsl"] == dsl
    assert "semantics" not in saved["dsl"]


@pytest.mark.parametrize(
    "kind", ["business_process", "operation_sequence", "evidence_navigation", "observation_comparison", "unclassified"]
)
def test_semantics_and_step_explanations_preserve_explicit_meaning(store, kind):
    dsl = journey(store[3])
    dsl["semantics"] = dict(
        kind=kind,
        basis="declared",
        purpose="理解所选动作的业务目标",
        preconditions=["具备测试角色"],
        outcome="查看固定版本中的结果说明",
        unknowns=["尚未经过独立业务验收"],
    )
    dsl["nodes"][0]["description"] = "此步骤的含义由编写者声明，引用不证明执行顺序。"
    assert validate_journey(dsl) == dsl


@pytest.mark.parametrize("missing", ["purpose", "outcome"])
def test_business_process_requires_explicit_goal_and_outcome(store, missing):
    dsl = journey(store[3])
    dsl["semantics"] = dict(kind="business_process", basis="declared", purpose="开票目标", outcome="开票结果")
    dsl["semantics"][missing] = "  "
    with pytest.raises(ValueError, match="业务流程"):
        validate_journey(dsl)


def test_no_model_template_creates_parallel_evidence_navigation_not_an_execution_chain(store):
    service, actor, pid, w, _, _ = store
    actions = w["actions"][:3]
    assert len(actions) >= 2
    payload = TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"],
        task_kind="business_flow",
        action_ids=[row["entity_id"] for row in actions],
    ).model_dump()
    tasks = KnowledgeTaskService(service)
    job = tasks.create(actor, pid, payload, "navigation")
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    dsl = JourneyService(service).list(actor, pid, payload["analysis_set_id"])[0]["dsl"]
    assert dsl["semantics"]["kind"] == "evidence_navigation"
    assert dsl["semantics"]["basis"] == "declared"
    assert dsl["semantics"]["unknowns"]
    by_key = {node["key"]: node for node in dsl["nodes"]}
    assert by_key[dsl["entry"]]["kind"] == "branch"
    assert not any(
        (by_key[edge["source"]]["kind"] == by_key[edge["target"]]["kind"] == "action" for edge in dsl["edges"])
    )
    assert {node["binding"]["revision_id"] for node in dsl["nodes"] if node.get("binding")} == {
        row["entity_revision_id"] for row in actions
    }
    assert result["report"]["saved_directly"] and result["report"]["model_calls"] == 0


def test_semantics_revision_is_immutable_and_cannot_bypass_reference_permissions(store):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    dsl = journey(w)
    service_journeys = JourneyService(service)
    first = service_journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=dsl), "first")
    edited = deepcopy(dsl)
    edited["semantics"] = dict(kind="business_process", basis="declared", purpose="完成申请", outcome="申请待审批")
    second = service_journeys.update(
        actor, first["journey_id"], dict(analysis_set_id=aid, dsl=edited), first["revision_id"], "classify"
    )
    assert first["revision_id"] != second["revision_id"]
    assert service_journeys.get(actor, pid, first["journey_id"], first["revision_id"]) == first
    assert second["dsl"] == edited
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    with service.db() as s:
        revision = s.get(Revision, w["actions"][0]["entity_revision_id"])
        s.get(Artifact, revision.artifact_id).sensitivity = "confidential"
    assert not service_journeys.list(limited, pid, aid)
    with pytest.raises(DomainError):
        service_journeys.get(limited, pid, second["journey_id"], second["revision_id"])


def test_declared_business_steps_need_explanations_and_cannot_impersonate_bound_actions(store):
    service, actor, pid, w, _, _ = store
    dsl = journey(w)
    dsl["nodes"][0] = dict(
        key="select", kind="step", title="填写申请", description="手册声明的业务步骤；尚未导入独立动作。", x=0, y=0
    )
    dsl["semantics"] = dict(kind="business_process", basis="declared", purpose="提交申请", outcome="申请待审批")
    source = w["documents"][0]
    dsl["semantics"]["source_refs"] = [dict(entity_id=source["entity_id"], revision_id=source["entity_revision_id"])]
    saved = JourneyService(service).create(
        actor, pid, dict(analysis_set_id=w["selected_analysis_set"]["id"], dsl=dsl), "declared-step"
    )
    assert saved["dsl"] == dsl
    missing_source = deepcopy(dsl)
    missing_source["semantics"].pop("source_refs")
    with pytest.raises(ValueError, match="准确来源修订"):
        validate_journey(missing_source)
    for bad_node in [
        dict(dsl["nodes"][0], description="  "),
        dict(dsl["nodes"][0], binding=journey(w)["nodes"][0]["binding"]),
        dict(
            dsl["nodes"][0], state_ref=dict(scenario_id="unknown", scenario_revision_id="unknown", state_key="unknown")
        ),
    ]:
        bad = deepcopy(dsl)
        bad["nodes"][0] = bad_node
        with pytest.raises(ValueError):
            validate_journey(bad)


def test_statement_sources_require_exact_visible_baseline_and_protect_unbound_wording(store):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    dsl = journey(w)
    source = w["documents"][0]
    ref = dict(entity_id=source["entity_id"], revision_id=source["entity_revision_id"])
    dsl["semantics"] = dict(
        kind="business_process", basis="declared", purpose="声明目标", outcome="声明结果", source_refs=[ref]
    )
    dsl["nodes"][0] = dict(
        key="select", kind="step", title="手册声明步骤", description="来源修订的业务说明，尚无独立动作绑定。", x=0, y=0
    )
    journeys = JourneyService(service)
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    with service.db() as s:
        source_artifact = s.get(Revision, ref["revision_id"]).artifact_id
        s.get(Artifact, source_artifact).sensitivity = "confidential"
    with pytest.raises(DomainError):
        journeys.create(limited, pid, dict(analysis_set_id=aid, dsl=dsl), "hidden-source")
    forged = deepcopy(dsl)
    forged["semantics"]["source_refs"][0]["revision_id"] = "unknown"
    with pytest.raises(DomainError):
        journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=forged), "forged-source")
    duplicated = deepcopy(dsl)
    duplicated["semantics"]["source_refs"].append(ref)
    with pytest.raises(DomainError):
        journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=duplicated), "duplicate-source")
    saved = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=dsl), "exact-source")
    assert not journeys.list(limited, pid, aid)
    with pytest.raises(DomainError):
        journeys.get(limited, pid, saved["journey_id"], saved["revision_id"])


def test_journey_cannot_be_used_as_a_material_source_or_create_a_clone_dependency_chain(store):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    first = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "source-first")
    second = dict(deepcopy(journey(w)), key="dependent")
    second["semantics"] = dict(
        kind="business_process",
        basis="declared",
        purpose="声明目标",
        outcome="声明结果",
        source_refs=[dict(entity_id=first["journey_id"], revision_id=first["revision_id"])],
    )
    with pytest.raises(DomainError, match="不能引用其他流程"):
        journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=second), "rejected-journey-source")
    assert journeys.list(actor, pid, aid) == [first]


def test_declared_sources_support_reverse_lookup_coverage_impact_and_immutable_export(store):
    import json
    from io import BytesIO
    import zipfile
    from fastapi.testclient import TestClient
    from vks_api.documents import DocumentService
    from vks_api.knowledge import KnowledgeService
    from vks_api.workbench_v2 import coverage, explanation

    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    doc, action = (w["documents"][0], w["actions"][0])
    dsl = journey(w)
    dsl["nodes"][0] = dict(
        key="select", kind="step", title="声明步骤", description="手册中的业务步骤；暂无独立动作绑定。", x=0, y=0
    )
    dsl["semantics"] = dict(
        kind="business_process",
        basis="declared",
        purpose="理解申请目标",
        outcome="理解申请结果",
        source_refs=[dict(entity_id=row["entity_id"], revision_id=row["entity_revision_id"]) for row in (doc, action)],
        unknowns=["尚未验证业务成功"],
    )
    journeys = JourneyService(service)
    saved = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=dsl), "source-flow")
    counts = coverage(service, actor, pid, aid)
    assert counts["journeys"]["business_process"] == counts["journeys"]["total"] == 1
    assert counts["actions"]["linked"] == len(w["actions"])
    assert explanation(service, actor, pid, action["entity_id"], aid)["journeys"] == [saved]
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        back = client.get(
            f"/api/v2/entities/{doc['entity_id']}/business-references", params=dict(project_id=pid, analysis_set_id=aid)
        )
        assert back.status_code == 200, back.text
        assert back.json()["data"]["journeys"] == [saved]
    for relation in w["relations"]:
        service.set_relation_state(
            actor,
            relation["id"],
            {
                "excluded": dict(decision="confirm", reason="Synthetic fixture", evidence_ids=relation["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            relation["row_version"],
        )
    candidate = service.save_snapshot(actor, pid, dict(analysis_set_id=aid), "source-precheck")
    pass
    ticket = None
    released = candidate
    clone = KnowledgeService(service).clone(actor, pid, aid, "Source change", "source-clone")
    docs = DocumentService(service)
    original = docs.read_revision(actor, doc["entity_id"], doc["entity_revision_id"])
    edited_doc = docs.save_draft(
        actor,
        doc["entity_id"],
        dict(
            analysis_set_id=clone["id"],
            base_revision_id=doc["entity_revision_id"],
            markdown=original["markdown"] + "\n## 新的约束\n新增合成规则。\n",
            locked_section_ids=[],
        ),
        1,
    )
    report = service.diff(actor, pid, released["id"], clone["id"])
    assert any(
        (
            item["basis"] == "explicit_journey_source" and item["binding_entity_id"] == doc["entity_id"]
            for item in report["journey_impacts"]
        )
    )
    edited_dsl = deepcopy(dsl)
    edited_dsl["semantics"]["source_refs"][0]["revision_id"] = edited_doc["entity_revision_id"]
    new = journeys.update(
        actor,
        saved["journey_id"],
        dict(analysis_set_id=clone["id"], dsl=edited_dsl),
        saved["revision_id"],
        "rebind-source",
    )
    assert new["revision_id"] != saved["revision_id"]
    assert journeys.list(actor, pid, release_id=released["id"]) == [saved]
    export = service.export(
        actor, pid, dict(release_id=released["id"], format="vksbundle", include_source=True), "source-export"
    )
    service.process_job(export["id"])
    assert service.get_job(actor, export["id"])["state"] == "succeeded"
    raw, _ = service.download_export(actor, pid, export["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        sidecar = json.loads(archive.read("export/journeys/" + saved["journey_id"] + ".json"))
    assert sidecar["definition"] == dsl
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    with service.db() as s:
        s.get(Artifact, s.get(Revision, edited_doc["entity_revision_id"]).artifact_id).sensitivity = "confidential"
    assert not service.diff(limited, pid, released["id"], clone["id"])["journey_impacts"]
    assert coverage(service, limited, pid, clone["id"])["journeys"]["total"] == 0


def test_reference_scan_has_a_visible_only_bound_and_reports_truncation(store):
    from vks_api.reading import ReadingService

    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    first = journey(w)
    journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=first), "scan-first")
    second = dict(deepcopy(first), key="second")
    doc = w["documents"][0]
    second["semantics"] = dict(
        kind="evidence_navigation",
        basis="declared",
        source_refs=[dict(entity_id=doc["entity_id"], revision_id=doc["entity_revision_id"])],
    )
    journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=second), "scan-second")
    reading = ReadingService(service)
    with reading.db() as s:
        analysis, release = reading.baseline(s, actor, pid, aid)
        choices, _ = reading.selections(s, analysis, release)
        rows, truncated = journeys.referencing(s, actor, pid, choices, w["actions"][0]["entity_revision_id"], limit=1)
        assert len(rows) == 1 and truncated
    with service.db() as s:
        s.get(Artifact, s.get(Revision, doc["entity_revision_id"]).artifact_id).sensitivity = "confidential"
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    with reading.db() as s:
        analysis, release = reading.baseline(s, limited, pid, aid)
        choices, _ = reading.selections(s, analysis, release)
        rows, truncated = journeys.referencing(s, limited, pid, choices, w["actions"][0]["entity_revision_id"], limit=1)
        assert len(rows) == 1 and (not truncated)


@pytest.mark.asyncio
async def test_official_mcp_sdk_round_trips_declared_step_and_exact_source_refs(store):
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from vks_api.mcp_gateway import APIBridge, create_server

    service, actor, pid, w, app, _ = store
    aid, doc = (w["selected_analysis_set"]["id"], w["documents"][0])
    dsl = journey(w)
    dsl["nodes"][0] = dict(
        key="select", kind="step", title="声明业务步骤", description="引用手册声明的业务步骤；暂无独立动作。", x=0, y=0
    )
    dsl["semantics"] = dict(
        kind="business_process",
        basis="declared",
        purpose="了解申请目标",
        outcome="了解申请结果",
        source_refs=[dict(entity_id=doc["entity_id"], revision_id=doc["entity_revision_id"])],
    )
    token = service.create_token(
        actor,
        pid,
        dict(name="Source proposal SDK", scopes=["knowledge:read", "knowledge:edit", "source:read", "trace:read"]),
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
        result = await sdk.call_tool(
            "propose_journey", dict(project_id=pid, analysis_set_id=aid, dsl=dsl, idempotency_key="declared-step-sdk")
        )
        assert not result.isError, result.content
        assert result.structuredContent["data"]["dsl"] == dsl
