from pathlib import Path
from uuid import uuid4
import hashlib
import json

import httpx
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from vks_api.app import create_app
from vks_api.mcp_gateway import APIBridge, create_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_same_release_web_mcp_import_review_publish_read(tmp_path):
    app = create_app(data_dir=tmp_path, enable_worker=False)
    service = app.state.service
    with TestClient(app) as web:
        logged = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
        csrf = {"X-CSRF-Token": logged.json()["data"]["csrf_token"]}

        def write(path, data=None, headers=None, method="POST"):
            response = web.request(
                method,
                "/api/v1" + path,
                json=data,
                headers={**csrf, "Idempotency-Key": str(uuid4()), **(headers or {})},
            )
            assert response.is_success, response.text
            return response.json()["data"]

        project = write("/projects", {"key": "charge-demo", "name": "MCP 闭环测试"})
        pid = project["id"]
        token = write(
            f"/projects/{pid}/tokens",
            {
                "name": "SDK editor",
                "scopes": [
                    "knowledge:read",
                    "knowledge:edit",
                    "source:read",
                    "source:import",
                    "analysis:run",
                    "links:propose",
                    "knowledge:export",
                ],
            },
        )
        raw = (ROOT / "fixtures/charge-demo.vksbundle.zip").read_bytes()
        upload = write(
            f"/projects/{pid}/upload-sessions",
            {
                "filename": "demo.vksbundle.zip",
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "media_type": "application/zip",
            },
            {"Authorization": "Bearer " + token["token"]},
        )
        response = web.put(upload["upload_url"], content=raw, headers={"Authorization": "Bearer " + token["token"]})
        assert response.is_success, response.text
        bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
        server = create_server(bridge)
        async with create_connected_server_and_client_session(server) as mcp:

            async def call(name, args):
                result = await mcp.call_tool(name, args)
                assert not result.isError, (name, result.content)
                return result.structuredContent["data"]

            assert (await call("list_projects", {}))[0]["id"] == pid
            arguments = {"project_id": pid, "artifact_id": upload["artifact_id"], "idempotency_key": "sdk-import-1"}
            imported = await call("import_bundle", arguments)
            assert imported["id"] == (await call("import_bundle", arguments))["id"]
            service.run_pending()
            job = await call("get_job", {"project_id": pid, "job_id": imported["id"]})
            assert job["state"] == "succeeded"
            workspace = web.get(f"/api/v1/projects/{pid}/workspace").json()["data"]
            analysis = workspace["selected_analysis_set"]["id"]
            action = workspace["actions"][0]["entity_id"]
            context = await call(
                "get_action_context", {"project_id": pid, "action_id": action, "analysis_set_id": analysis}
            )
            assert context["baseline_kind"] == "draft"
            # Structural AST relations are index facts, while this tool accepts
            # business-link candidates. Choose a supported business edge explicitly.
            relation = next(row for row in workspace["relations"] if row["relation_type"] == "documents")
            duplicate = {
                "from_revision_id": relation["from_revision_id"],
                "to_revision_id": relation["to_revision_id"],
                "relation_type": relation["relation_type"],
                "evidence_ids": relation["evidence_ids"],
                "causal_basis": relation["causal_basis"],
                "reason": "SDK 校验相同提案幂等",
            }
            proposal = await call(
                "propose_links",
                {
                    "project_id": pid,
                    "analysis_set_id": analysis,
                    "candidates": [duplicate],
                    "idempotency_key": "sdk-propose-1",
                },
            )
            assert proposal["candidates"][0]["id"] == relation["id"]
            for relation in workspace["relations"]:
                write(
                    f"/relations/{relation['id']}/review",
                    {
                        "decision": "confirm",
                        "reason": "人工核对合成材料中的定位与含义",
                        "evidence_ids": relation["evidence_ids"],
                    },
                    {"If-Match": f'"{relation["row_version"]}"'},
                )
            request = await call(
                "request_release",
                {"project_id": pid, "analysis_set_id": analysis, "idempotency_key": "sdk-preflight-1"},
            )
            assert request["published"] is False
            confirmation = write(f"/release-candidates/{request['candidate_id']}/confirm")
            released = write(
                f"/release-candidates/{request['candidate_id']}/publish",
                {
                    **{k: confirmation[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")},
                    "change_note": "Web 人工审核发布",
                },
            )
            rid = released["id"]
            context = await call("get_action_context", {"project_id": pid, "release_id": rid, "action_id": action})
            assert context["release_id"] == rid and context["baseline_kind"] == "published"
            assert all(r["review_status"] == "confirmed" for r in context["relations"])
            assert (await call("get_project_context", {"project_id": pid, "release_id": rid}))["release"]["id"] == rid
            assert await call("search_knowledge", {"project_id": pid, "release_id": rid, "query": "开票"})
            graph = await call(
                "trace_business_flow",
                {
                    "project_id": pid,
                    "release_id": rid,
                    "start_entity_id": action,
                    "relation_types": ["documents", "handler_of"],
                },
            )
            assert graph["nodes"]
            evidence = await call("get_evidence", {"project_id": pid, "evidence_ids": [workspace["evidence"][0]["id"]]})
            assert evidence[0]["locator"]["relative_path"]
            resource = await mcp.read_resource(f"vks://projects/{pid}/releases/{rid}/overview")
            assert json.loads(resource.contents[0].text)["data"]["release"]["id"] == rid
            evidence_resource = await mcp.read_resource(f"vks://evidence/{workspace['evidence'][0]['id']}")
            assert json.loads(evidence_resource.contents[0].text)["data"]["id"] == workspace["evidence"][0]["id"]
            exported = await call(
                "export_knowledge",
                {"project_id": pid, "release_id": rid, "format": "markdown_bundle", "idempotency_key": "sdk-export-1"},
            )
            service.run_pending()
            assert (await call("get_job", {"project_id": pid, "job_id": exported["id"]}))["state"] == "succeeded"
            # Revoke while the SDK connection is alive; the next business request must fail.
            write(f"/projects/{pid}/tokens/{token['id']}", method="DELETE")
            denied = await mcp.call_tool("list_projects", {})
            assert denied.isError and denied.structuredContent["error"]["code"] == "VKS_UNAUTHENTICATED"
