from copy import deepcopy
import pytest
from vks_api.models import Analysis
from vks_api.scenarios import ScenarioService
from vks_api.service import DomainError, Service
from packages.scenario_dsl import ScenarioError, validate_dsl


def dsl():
    return {
        "schema_version": "1.0",
        "scenario_key": "demo.read",
        "name": "只读演示",
        "environment_ref": "mock-test",
        "dataset_ref": "synthetic",
        "risk_level": "read_only",
        "timeout_ms": 20000,
        "preconditions": ["仅使用合成数据"],
        "steps": [
            {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 3000, "risk": "read_only"},
            {"step_key": "capture", "kind": "snapshot", "name": "初始状态", "timeout_ms": 3000, "risk": "read_only"},
        ],
    }


@pytest.fixture
def setup(tmp_path):
    service = Service(tmp_path)
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "scenario-test", "name": "场景测试"}, "project")
    analysis = service.create_analysis(owner, project["id"], {"name": "draft", "sources": []}, "analysis")
    scenarios = ScenarioService(service)
    yield (service, owner, project, analysis, scenarios)
    service.engine.dispose()


def test_dsl_refuses_scripts_duplicate_steps_external_paths_and_secret_literals():
    valid = dsl()
    validate_dsl(valid)
    for changed in (
        {**valid, "eval": "doAnything()"},
        {**valid, "steps": [valid["steps"][0], valid["steps"][0]]},
        {**valid, "steps": [{**valid["steps"][0], "path": "//outside.test"}]},
        {**valid, "steps": [{**valid["steps"][0], "path": "/%2foutside.test"}]},
        {**valid, "steps": [{**valid["steps"][0], "path": "/\\outside.test"}]},
    ):
        with pytest.raises(ScenarioError):
            validate_dsl(changed)


def test_declared_graph_checks_real_state_and_action_refs(setup):
    service, owner, p, a, scenarios = setup
    graph = {
        "projectKey": p["key"],
        "baselineKey": a["key"],
        "states": [{"stateKey": "ready", "label": "初始", "evidenceOrigin": "declared"}],
        "transitions": [],
    }
    data = {**dsl(), "explanation_graph": graph}
    saved = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": data}, "scenario")
    assert saved["dsl"]["explanation_graph"]["states"][0]["evidenceOrigin"] == "declared"
    bad = deepcopy(data)
    bad["explanation_graph"]["baselineKey"] = "other-baseline"
    with pytest.raises(DomainError):
        scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": bad}, "wrong")
    bad = deepcopy(data)
    bad["explanation_graph"]["states"][0].update(
        evidenceOrigin="observed",
        captureRef="11111111-1111-4111-8111-111111111111",
        scenarioRevisionRef="22222222-2222-4222-8222-222222222222",
    )
    with pytest.raises(DomainError) as exc:
        scenarios.update(owner, saved["scenario_id"], {"analysis_set_id": a["id"], "dsl": bad}, 1)
    assert exc.value.code == "VKS_INVALID_EVIDENCE"
    bad = deepcopy(data)
    bad["explanation_graph"]["transitions"] = [
        {
            "transitionKey": "go",
            "fromState": "ready",
            "toState": "missing",
            "actionKey": "missing.action",
            "conditionLabel": "显示而非执行",
            "evidenceOrigin": "simulated",
            "scenarioRef": "demo.read",
        }
    ]
    with pytest.raises(DomainError):
        scenarios.update(owner, saved["scenario_id"], {"analysis_set_id": a["id"], "dsl": bad}, 1)


def test_scenarios_are_immutable_idempotent_and_use_optimistic_versions(setup):
    service, owner, p, a, scenarios = setup
    payload = {"analysis_set_id": a["id"], "dsl": dsl()}
    first = scenarios.create(owner, p["id"], payload, "scenario")
    assert first["scenario_revision_id"] != first["entity_revision_id"]
    assert scenarios.create(owner, p["id"], payload, "scenario") == first
    changed = {**dsl(), "name": "新名称"}
    second = scenarios.update(owner, first["scenario_id"], {"analysis_set_id": a["id"], "dsl": changed}, 1)
    assert second["revision_no"] == 2 and second["scenario_id"] == first["scenario_id"]
    assert (
        scenarios.get(owner, p["id"], first["scenario_id"], first["scenario_revision_id"])["dsl"]["name"] == "只读演示"
    )
    with pytest.raises(DomainError) as exc:
        scenarios.update(owner, first["scenario_id"], payload, 1)
    assert exc.value.status == 412
    with service.db() as s:
        s.get(Analysis, a["id"]).state = "frozen"
    with pytest.raises(DomainError) as exc:
        scenarios.update(owner, first["scenario_id"], payload, 2)
    assert exc.value.status == 409


def test_missing_environment_never_queues_capture(setup):
    service, owner, p, a, scenarios = setup
    saved = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": dsl()}, "scenario")
    with pytest.raises(DomainError) as error:
        scenarios.request_capture(
            owner,
            saved["scenario_id"],
            {
                "analysis_set_id": a["id"],
                "scenario_revision_id": saved["scenario_revision_id"],
                "environment_ref": "mock-test",
            },
            "capture",
        )
    assert error.value.code == "VKS_STATE_CONFLICT"
    assert not [j for j in service.workspace(owner, p["id"])["jobs"] if j["kind"] == "capture"]


def test_scope_and_cross_project_references_are_rejected(setup):
    service, owner, p, a, scenarios = setup
    saved = scenarios.create(owner, p["id"], {"analysis_set_id": a["id"], "dsl": dsl()}, "scenario")
    token = service.create_token(owner, p["id"], {"name": "reader", "scopes": ["knowledge:read"]})
    reader = service.authenticate(token["token"], "token")
    with pytest.raises(DomainError):
        scenarios.create(reader, p["id"], {"analysis_set_id": a["id"], "dsl": dsl()}, "forbidden")
    with pytest.raises(DomainError):
        scenarios.get(reader, p["id"], saved["scenario_id"], saved["scenario_revision_id"])
    other = service.create_project(owner, {"key": "other-project", "name": "其他项目"}, "other")
    with pytest.raises(DomainError) as exc:
        scenarios.get(owner, other["id"], saved["scenario_id"], saved["scenario_revision_id"])
    assert exc.value.status == 404


def test_clone_rebinds_scenario_identity_and_invalidates_old_endpoint_review(setup):
    from vks_api.knowledge import KnowledgeService

    service, owner, project, _, scenarios = setup
    imported = service.demo_import(owner, project["id"], "fixture")
    service.process_job(imported["id"])
    w = service.workspace(owner, project["id"])
    aid = w["selected_analysis_set"]["id"]
    definition = {
        **dsl(),
        "risk_level": "test_write",
        "explanation_graph": {
            "projectKey": project["key"],
            "baselineKey": w["selected_analysis_set"]["key"],
            "states": [{"stateKey": "ready", "label": "声明", "evidenceOrigin": "declared"}],
            "transitions": [],
        },
    }
    original = scenarios.create(owner, project["id"], {"analysis_set_id": aid, "dsl": definition}, "scenario")
    action = w["actions"][0]
    edge = service.propose_links(
        owner,
        project["id"],
        {
            "analysis_set_id": aid,
            "candidates": [
                {
                    "from_revision_id": original["entity_revision_id"],
                    "to_revision_id": action["entity_revision_id"],
                    "relation_type": "contains",
                    "evidence_ids": w["relations"][0]["evidence_ids"],
                    "reason": "人员组织场景步骤",
                    "causal_basis": "not_established",
                }
            ],
        },
        "scenario-link",
    )["candidates"][0]
    service.set_relation_state(owner, edge["id"], {"excluded": "confirm" == "reject"}, edge["row_version"])
    clone = KnowledgeService(service).clone(owner, project["id"], aid, "场景草稿", "clone")
    cloned = scenarios.list(owner, project["id"], analysis_set_id=clone["id"])[0]
    assert cloned["scenario_id"] == original["scenario_id"]
    assert cloned["scenario_revision_id"] != original["scenario_revision_id"]
    assert cloned["dsl"]["explanation_graph"]["baselineKey"] == clone["key"]
    assert "approval_ref" not in cloned["dsl"]
    target = service.workspace(owner, project["id"], clone["id"])
    rebound = next((r for r in target["relations"] if r["from_revision_id"] == cloned["entity_revision_id"]))
    assert rebound["validity_status"] == "stale" and rebound["excluded"] is False
    source = scenarios.get(owner, project["id"], original["scenario_id"], original["scenario_revision_id"])
    assert source["dsl"] == original["dsl"]
