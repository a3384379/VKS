from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from vks_api.app import create_app
from vks_api.mcp_gateway import FULL, REQUIREMENTS_READONLY, APIBridge, create_http_app


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [FULL, REQUIREMENTS_READONLY])
async def test_streamable_http_sdk_authenticated_handshake_and_revocation(tmp_path, profile):
    api = create_app(data_dir=tmp_path, enable_worker=False)
    service = api.state.service
    with TestClient(api) as web:
        session = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token}).json()["data"]
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": str(uuid4())}
        project = web.post("/api/v1/projects", headers=headers, json={"key": "http-demo", "name": "HTTP SDK"}).json()[
            "data"
        ]
        token = web.post(
            f"/api/v1/projects/{project['id']}/tokens",
            headers=headers,
            json={"name": "HTTP reader", "scopes": ["knowledge:read"]},
        ).json()["data"]
        gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=api)), profile)
        async with gateway.router.lifespan_context(gateway):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:8788"
            ) as bare:
                assert (await bare.post("/mcp", json={})).status_code == 401
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + token["token"]}
                ) as client,
                streamable_http_client("http://localhost:8788/mcp", http_client=client) as (read, write, _),
                ClientSession(read, write) as sdk,
            ):
                assert (await sdk.initialize()).serverInfo.version == "3.0.0-rc.1"
                result = await sdk.call_tool("list_projects", {})
                assert not result.isError
                assert result.structuredContent["data"][0]["id"] == project["id"]
                unavailable = await sdk.call_tool(
                    "search_knowledge",
                    {
                        "project_id": project["id"],
                        "release_id": str(uuid4()),
                        "query": "fixture",
                        "retrieval_mode": "keyword",
                    },
                )
                code = "VKS_TARGET_UNAVAILABLE" if profile == REQUIREMENTS_READONLY else "VKS_NOT_FOUND"
                assert unavailable.isError and unavailable.structuredContent["error"]["code"] == code
            web.delete(f"/api/v1/projects/{project['id']}/tokens/{token['id']}", headers=headers)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway),
                base_url="http://localhost:8788",
                headers={"Authorization": "Bearer " + token["token"]},
            ) as revoked:
                assert (await revoked.post("/mcp", json={})).status_code == 401
