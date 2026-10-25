import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from vks_api.app import create_app
from vks_api.mcp_gateway import APIBridge, create_server
from vks_api.modules import ModuleService


@pytest.mark.asyncio
async def test_official_sdk_reads_filtered_module_context_and_search(tmp_path):
    app = create_app(data_dir=tmp_path, enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Module SDK"}, "project")
    pid = project["id"]
    job = service.demo_import(owner, pid, "import")
    service.process_job(job["id"])
    workspace = service.workspace(owner, pid)
    aid = workspace["selected_analysis_set"]["id"]
    modules = ModuleService(service)
    root = modules.create(owner, pid, {"key": "billing", "name": "Billing"})
    child = modules.create(owner, pid, {"key": "billing.api", "name": "Billing APIs", "parent_id": root["id"]})
    symbol = next(
        entity
        for entity in workspace["entities"]
        if entity["type"] == "symbol" and "InvoiceController" in entity["name"]
    )
    doc = workspace["documents"][0]
    modules.set_members(
        owner, pid, child["id"], {"analysis_set_id": aid, "entity_ids": [symbol["entity_id"], doc["entity_id"]]}, 1
    )
    for relation in workspace["relations"]:
        service.review_relation(
            owner,
            relation["id"],
            {"decision": "confirm", "reason": "Inspected fixture", "evidence_ids": relation["evidence_ids"]},
            relation["row_version"],
        )
    candidate = service.request_release(owner, pid, {"analysis_set_id": aid}, "candidate")
    ticket = service.confirm_release(owner, candidate["candidate_id"])
    release = service.publish(
        owner,
        candidate["candidate_id"],
        {key: ticket[key] for key in ("human_confirmation_ref", "expected_manifest_sha256")}
        | {"change_note": "SDK baseline"},
        "publish",
    )
    token = service.create_token(
        owner, pid, {"name": "SDK reader", "scopes": ["knowledge:read", "source:read"], "expires_in_days": 1}
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
        response = await sdk.call_tool("get_project_context", {"project_id": pid, "release_id": release["id"]})
        assert not response.isError, response.content
        contexts = response.structuredContent["data"]["modules"]
        assert {node["id"] for node in contexts} == {root["id"], child["id"]}
        assert next(node for node in contexts if node["id"] == child["id"])["parent_id"] == root["id"]
        arguments = {
            "project_id": pid,
            "release_id": release["id"],
            "query": "Invoice",
            "module_id": root["id"],
            "kinds": ["symbol"],
            "service_key": "billing",
            "include_descendants": True,
        }
        searched = await sdk.call_tool("search_knowledge", arguments)
        assert not searched.isError, searched.content
        assert [row["entity"]["entity_id"] for row in searched.structuredContent["data"]] == [symbol["entity_id"]]
        sql_search = await sdk.call_tool(
            "search_knowledge",
            {
                "project_id": pid,
                "release_id": release["id"],
                "query": "insert",
                "kinds": ["sql"],
            },
        )
        assert not sql_search.isError, sql_search.content
        assert sql_search.structuredContent["data"] and all(
            row["entity"]["type"] == "sql" for row in sql_search.structuredContent["data"]
        )
        without_descendants = await sdk.call_tool("search_knowledge", dict(arguments, include_descendants=False))
        assert not without_descendants.isError and without_descendants.structuredContent["data"] == []
    limited_token = service.create_token(
        owner, pid, {"name": "SDK limited reader", "scopes": ["knowledge:read"], "expires_in_days": 1}
    )
    limited = APIBridge("http://testserver", limited_token["token"], httpx.ASGITransport(app=app))
    async with create_connected_server_and_client_session(create_server(limited)) as sdk:
        response = await sdk.call_tool("get_project_context", {"project_id": pid, "release_id": release["id"]})
        assert not response.isError, response.content
        assert (
            next(node for node in response.structuredContent["data"]["modules"] if node["id"] == child["id"])[
                "entity_count"
            ]
            == 1
        )
        searched = await sdk.call_tool("search_knowledge", arguments)
        assert not searched.isError and searched.structuredContent["data"] == []
    service.stop_worker()
    service.engine.dispose()
