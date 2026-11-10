from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from vks_api.app import create_app
from vks_api.mcp_gateway import APIBridge, create_http_app


@pytest.mark.asyncio
async def test_streamable_http_sdk_authenticated_handshake_and_revocation(tmp_path):
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
        gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=api)))
        async with gateway.router.lifespan_context(gateway):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:8788"
            ) as bare:
                assert (await bare.post("/mcp", json={})).status_code == 401
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + token["token"]}
            ) as client:
                async with streamable_http_client("http://localhost:8788/mcp", http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as sdk:
                        assert (await sdk.initialize()).serverInfo.version == "3.0.0-rc.1"
                        result = await sdk.call_tool("list_projects", {})
                        assert not result.isError
                        assert result.structuredContent["data"][0]["id"] == project["id"]
            web.delete(f"/api/v1/projects/{project['id']}/tokens/{token['id']}", headers=headers)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway),
                base_url="http://localhost:8788",
                headers={"Authorization": "Bearer " + token["token"]},
            ) as revoked:
                assert (await revoked.post("/mcp", json={})).status_code == 401
