from io import BytesIO
import zipfile
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from vks_api.app import create_app
from vks_api.mcp_gateway import APIBridge, create_server
from scripts.build_selfhosted import build, FEATURES
from scripts.build_pilot import bundle_bytes
from scripts.seed_validation import seed
from packages.analyzers.vks_ingest import parse_bundle


def test_pilot_denominators_and_declared_scenarios_are_explicit(tmp_path):
    parsed = parse_bundle(bundle_bytes(), "pilot-jfa")
    assert sum(e["type"] == "action" for e in parsed.entities) == 30
    assert sum(e["type"] == "section" for e in parsed.entities) == 30
    assert (
        len(
            {
                (e["metadata"]["method"], e["metadata"]["normalized_route"])
                for e in parsed.entities
                if e["type"] == "api"
            }
        )
        == 15
    )
    result = seed(tmp_path)
    assert result["scenarios"] == 3 and result["actions"] == 30
    assert not result["published"] and not result["runtime_validated"]


@pytest.mark.asyncio
async def test_actual_checkout_actions_api_sources_and_evidence_through_official_sdk(tmp_path):
    files = build()
    packed = BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
        for path, raw in files.items():
            z.writestr(path, raw)
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    _, owner = s.login(s.bootstrap_token)
    p = s.create_project(owner, {"key": "vks-selfhosted", "name": "VKS源码自举"}, "project")
    with s.db() as db:
        result = s._commit_bundle(db, p["id"], owner.id, s._parse(packed.getvalue(), p["key"]))
    aid = result["analysis_set_id"]
    w = s.workspace(owner, p["id"], aid)
    token = s.create_token(
        owner,
        p["id"],
        {"name": "selfhosted-sdk", "scopes": ["knowledge:read", "knowledge:edit", "source:read"], "expires_in_days": 1},
    )
    server = create_server(APIBridge("http://testserver", token["token"], transport=httpx.ASGITransport(app=app)))
    async with create_connected_server_and_client_session(server) as sdk:
        for key, title, *_ in FEATURES:
            action = next(e for e in w["actions"] if e["stable_key"] == "self." + key)
            response = await sdk.call_tool(
                "get_action_context",
                {
                    "project_id": p["id"],
                    "analysis_set_id": aid,
                    "action_id": action["entity_id"],
                    "depth": 3,
                    "max_nodes": 50,
                },
            )
            assert not response.isError, response.structuredContent
            context = response.structuredContent["data"]
            assert context["baseline_kind"] == "draft" and context["analysis_set_id"] == aid
            assert any(r["relation_type"] == "documents" for r in context["relations"])
            assert any(r["relation_type"] == "handler_of" for r in context["relations"])
            assert all(r["review_status"] == "candidate" for r in context["relations"])
        sources = [e for e in w["evidence"] if e["locator"]["relative_path"].startswith("backend/")]
        assert len(sources) == 8
        evidence = await sdk.call_tool("get_evidence", {"project_id": p["id"], "evidence_ids": [sources[0]["id"]]})
        assert not evidence.isError and evidence.structuredContent["data"][0]["excerpt"]
        s.revoke_token(owner, p["id"], token["id"])
        revoked = await sdk.call_tool("list_projects", {})
        assert revoked.isError
