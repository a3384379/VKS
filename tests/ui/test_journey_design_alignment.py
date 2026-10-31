"""Independent, isolated max audit regressions; no live instance is used."""

from test_frontend_browser import site as site  # pytest fixture registration

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "ui"))

from playwright.sync_api import expect, sync_playwright
from test_frontend_browser import chromium_path
from vks_api.journeys import JourneyService
from vks_api.models import Artifact, Member, Revision
from vks_api.scenarios import ScenarioService


def login(page, url, pid, aid, jid):
    page.goto(url + f"/#view=journeys&project={pid}&baseline=draft:{aid}&entity={jid}")
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()
    expect(page.get_by_label("当前导览", exact=True)).to_be_visible()


def test_saved_state_action_retains_action_knowledge_and_chain(site):
    service, url = site.service, site[0]
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "max-action-state", "name": "Max action state"}, "max-action-state")[
        "id"
    ]
    imported = service.demo_import(owner, pid, "max-action-state-demo")
    service.process_job(imported["id"])
    w = service.workspace(owner, pid)
    aid = w["selected_analysis_set"]["id"]
    api_ids = {row["entity_revision_id"] for row in w["entities"] if row["type"] == "api"}
    action_ids = {row["entity_revision_id"] for row in w["actions"]}
    edge = next(
        row for row in w["relations"] if row["from_revision_id"] in action_ids and row["to_revision_id"] in api_ids
    )
    action = next(row for row in w["actions"] if row["entity_revision_id"] == edge["from_revision_id"])
    page_key = action["metadata"]["page_key"]
    endpoint = next(row for row in w["entities"] if row["entity_revision_id"] == edge["to_revision_id"])
    service.set_relation_state(
        owner,
        edge["id"],
        {
            "excluded": dict(
                decision="confirm", reason="Synthetic audit relationship", evidence_ids=edge["evidence_ids"]
            )["decision"]
            == "reject"
        },
        edge["row_version"],
    )
    scene = ScenarioService(service).create(
        owner,
        pid,
        {
            "analysis_set_id": aid,
            "dsl": {
                "schema_version": "1.0",
                "scenario_key": "max-state",
                "name": "Max state",
                "environment_ref": "max-mock",
                "dataset_ref": "max-data",
                "risk_level": "read_only",
                "timeout_ms": 10000,
                "steps": [{"step_key": "open", "kind": "goto", "path": "/", "risk": "read_only", "timeout_ms": 3000}],
                "explanation_graph": {
                    "projectKey": "max-action-state",
                    "baselineKey": w["selected_analysis_set"]["key"],
                    "states": [
                        {
                            "stateKey": "ready",
                            "label": "Max ready state",
                            "evidenceOrigin": "declared",
                            "actionKey": action["stable_key"],
                            "pageKey": page_key,
                        }
                    ],
                    "transitions": [],
                },
            },
        },
        "max-scene",
    )
    row = JourneyService(service).create(
        owner,
        pid,
        dict(
            analysis_set_id=aid,
            dsl=dict(
                schema_version="2.0",
                key="max-state-flow",
                title="Max saved-state guide",
                entry="a",
                semantics=dict(kind="operation_sequence", basis="declared", purpose="Read exact saved action state"),
                nodes=[
                    dict(
                        key="a",
                        kind="action",
                        title="Bound saved action",
                        description="Exact action knowledge must accompany this saved state",
                        x=0,
                        y=0,
                        binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
                        state_ref=dict(
                            scenario_id=scene["scenario_id"],
                            scenario_revision_id=scene["entity_revision_id"],
                            state_key="ready",
                        ),
                    ),
                    dict(key="done", kind="end", title="End", x=300, y=0),
                ],
                edges=[dict(key="next", source="a", target="done", label="")],
            ),
        ),
        "max-state-flow",
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1000))
        try:
            login(page, url, pid, aid, row["journey_id"])
            expect(page.get_by_label("步骤保存状态")).to_contain_text("Max ready state")
            panel = page.get_by_label("当前步骤知识")
            panel.get_by_role("button", name="接口", exact=True).click()
            expect(panel).to_contain_text(endpoint["name"])
            expect(page.get_by_role("heading", name="当前动作的技术关联路径", exact=True)).to_be_visible()
        finally:
            browser.close()


def test_source_reclassification_clears_loaded_guide_on_workspace_poll(site):
    service, url = site.service, site[0]
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(
        owner, {"key": "max-policy-refresh", "name": "Max policy refresh"}, "max-policy-refresh"
    )["id"]
    imported = service.demo_import(owner, pid, "max-policy-demo")
    service.process_job(imported["id"])
    w = service.workspace(owner, pid)
    aid = w["selected_analysis_set"]["id"]
    doc = w["documents"][0]
    row = JourneyService(service).create(
        owner,
        pid,
        dict(
            analysis_set_id=aid,
            dsl=dict(
                schema_version="2.0",
                key="max-policy-flow",
                title="CLASSIFICATION-AUDIT-PRIVATE-TITLE",
                entry="a",
                semantics=dict(
                    kind="business_process",
                    basis="declared",
                    purpose="CLASSIFICATION-AUDIT-PRIVATE-GOAL",
                    outcome="Declared result",
                    source_refs=[dict(entity_id=doc["entity_id"], revision_id=doc["entity_revision_id"])],
                ),
                nodes=[
                    dict(
                        key="a",
                        kind="step",
                        title="Declared confidential step",
                        description="Exact source statement",
                        x=0,
                        y=0,
                    ),
                    dict(key="done", kind="end", title="End", x=300, y=0),
                ],
                edges=[dict(key="next", source="a", target="done", label="")],
            ),
        ),
        "max-policy-flow",
    )
    with service.db() as db:
        db.get(Member, (pid, owner.id)).role = "editor"
        service._new_job(db, owner, pid, "index", {"analysis_set_id": aid})
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1000))
        try:
            login(page, url, pid, aid, row["journey_id"])
            expect(page.get_by_label("当前导览", exact=True)).to_contain_text("CLASSIFICATION-AUDIT-PRIVATE-GOAL")
            with service.db() as db:
                artifact_id = db.get(Revision, doc["entity_revision_id"]).artifact_id
                db.get(Artifact, artifact_id).sensitivity = "confidential"
            assert page.request.get(url + f"/api/v2/projects/{pid}/journeys?analysis_set_id={aid}").json()["data"] == []
            with page.expect_response(lambda response: "/workspace?" in response.url):
                pass
            expect(page.get_by_text("CLASSIFICATION-AUDIT-PRIVATE-GOAL", exact=True)).to_have_count(0, timeout=7000)
            expect(page.get_by_text("CLASSIFICATION-AUDIT-PRIVATE-TITLE", exact=True)).to_have_count(0)
        finally:
            browser.close()
