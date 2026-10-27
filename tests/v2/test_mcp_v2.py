import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from test_journeys import store, journey
from vks_api.mcp_gateway import APIBridge, create_server, create_http_app


@pytest.mark.asyncio
async def test_official_sdk_v2_same_context_proposal_task_and_human_boundary(store):
    service, actor, pid, w, app, _ = store
    token = service.create_token(
        actor,
        pid,
        {"name": "V2 SDK", "scopes": ["knowledge:read", "knowledge:edit", "analysis:run", "source:read", "trace:read"]},
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    aid = w["selected_analysis_set"]["id"]
    async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
        result = await sdk.call_tool("get_knowledge_coverage", {"project_id": pid, "analysis_set_id": aid})
        assert not result.isError, result.content
        assert result.structuredContent["data"]["actions"]["total"] == len(w["actions"])
        result = await sdk.call_tool(
            "propose_journey",
            {"project_id": pid, "analysis_set_id": aid, "dsl": journey(w), "idempotency_key": "sdk-journey"},
        )
        assert not result.isError, result.content
        row = result.structuredContent["data"]
        assert row["dsl"] == journey(w)
        result = await sdk.call_tool(
            "create_knowledge_task",
            {"project_id": pid, "analysis_set_id": aid, "task_kind": "project_overview", "idempotency_key": "sdk-task"},
        )
        assert not result.isError, result.content
        job = result.structuredContent["data"]
        service.process_job(job["id"])
        result = await sdk.call_tool("get_knowledge_task", {"project_id": pid, "job_id": job["id"]})
        assert not result.isError, result.content
        assert result.structuredContent["data"]["state"] == "succeeded"
        assert (
            next(row for row in result.structuredContent["data"]["stage_history"] if row["stage"] == "Collect")[
                "cached"
            ]
            == "false"
        )
        from vks_api.scenarios import ScenarioService

        scene = ScenarioService(service).create(
            actor,
            pid,
            dict(
                analysis_set_id=aid,
                dsl=dict(
                    schema_version="1.0",
                    scenario_key="sdk-state",
                    name="SDK passive state",
                    environment_ref="unused",
                    dataset_ref="unused",
                    risk_level="read_only",
                    timeout_ms=1000,
                    steps=[dict(step_key="open", kind="goto", path="/", risk="read_only", timeout_ms=1000)],
                    explanation_graph=dict(
                        projectKey=w["project"]["key"],
                        baselineKey=w["selected_analysis_set"]["key"],
                        states=[dict(stateKey="declared", label="Declared SDK state", evidenceOrigin="declared")],
                        transitions=[],
                    ),
                ),
            ),
            "sdk-scene",
        )
        passive = dict(journey(w), key="sdk-state-reference")
        passive["nodes"][0]["state_ref"] = dict(
            scenario_id=scene["scenario_id"], scenario_revision_id=scene["entity_revision_id"], state_key="declared"
        )
        result = await sdk.call_tool(
            "propose_journey",
            dict(project_id=pid, analysis_set_id=aid, dsl=passive, idempotency_key="sdk-state-reference"),
        )
        assert not result.isError, result.content
        assert result.structuredContent["data"]["dsl"] == passive
        denied = await sdk.call_tool("approve_journey", {"project_id": pid, "revision_id": row["revision_id"]})
        assert denied.isError
        changed = await sdk.call_tool(
            "propose_journey",
            {
                "project_id": pid,
                "analysis_set_id": aid,
                "dsl": dict(journey(w), script="malicious"),
                "idempotency_key": "bad",
            },
        )
        assert changed.isError
    # Exercise the actual official Streamable HTTP client for additive V2 tools.
    gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=app)))
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + token["token"]}
        ) as client:
            async with streamable_http_client("http://localhost:8788/mcp", http_client=client) as (reader, writer, _):
                async with ClientSession(reader, writer) as sdk:
                    await sdk.initialize()
                    result = await sdk.call_tool("get_knowledge_coverage", {"project_id": pid, "analysis_set_id": aid})
                    assert not result.isError, result.content
                    assert result.structuredContent["data"]["actions"]["total"] == len(w["actions"])
