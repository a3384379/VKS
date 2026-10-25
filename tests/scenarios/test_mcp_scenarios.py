import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from vks_api.app import create_app
from vks_api.mcp_gateway import APIBridge, create_server
from vks_api.scenarios import ScenarioService


@pytest.mark.asyncio
async def test_official_sdk_validates_compatible_role_and_expected_results_extensions(tmp_path):
    app = create_app(data_dir=tmp_path, enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "scenario-sdk", "name": "场景正式SDK"}, "project")
    a = service.create_analysis(owner, p["id"], {"name": "draft", "sources": []}, "analysis")
    definition = {
        "schema_version": "1.0",
        "scenario_key": "sdk.read",
        "name": "场景",
        "role": "合成阅读者",
        "expected_results": ["只读展示"],
        "environment_ref": "mock",
        "dataset_ref": "synthetic",
        "risk_level": "read_only",
        "timeout_ms": 2000,
        "steps": [
            {
                "step_key": "open",
                "kind": "goto",
                "path": "/",
                "timeout_ms": 1000,
                "risk": "read_only",
                "wait_until": "networkidle",
            }
        ],
    }
    scenario = ScenarioService(service).create(
        owner, p["id"], {"analysis_set_id": a["id"], "dsl": definition}, "scenario"
    )
    token = service.create_token(
        owner, p["id"], {"name": "SDK", "scopes": ["knowledge:read", "knowledge:edit", "capture:run"]}
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
        response = await sdk.call_tool(
            "get_scenario",
            {
                "project_id": p["id"],
                "scenario_id": scenario["scenario_id"],
                "scenario_revision_id": scenario["scenario_revision_id"],
            },
        )
        assert not response.isError, response.content
        assert response.structuredContent["data"]["dsl"]["role"] == "合成阅读者"
        assert response.structuredContent["data"]["dsl"]["expected_results"] == ["只读展示"]
        assert response.structuredContent["data"]["dsl"]["steps"][0]["wait_until"] == "networkidle"
        capture = await sdk.call_tool(
            "request_capture",
            {
                "project_id": p["id"],
                "scenario_revision_id": scenario["scenario_revision_id"],
                "environment_ref": "mock",
                "idempotency_key": "needs-human-approval",
            },
        )
        assert not capture.isError and capture.structuredContent["data"]["decision"] == "approval_required"
    service.engine.dispose()
