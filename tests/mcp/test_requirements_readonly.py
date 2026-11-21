"""The read profile through the official SDK and an isolated loopback API."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.knowledge import KnowledgeService
from vks_api.knowledge_locator import studio_base_url
from vks_api.materials import COMPOSITION, PurgeMaterials, TrashMaterial
from vks_api.materials import ROOT as MATERIAL_ROOT
from vks_api.mcp_gateway import REQUIREMENTS_READONLY, APIBridge, create_http_app
from vks_api.models import Artifact, Release, Revision

from tests.interactive import test_knowledge, test_materials
from tests.ui.test_frontend_browser import live_api

ROOT = Path(__file__).resolve().parents[2]
workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
materials = test_materials.materials


@asynccontextmanager
async def connect(transport, url, token):
    if transport == "stdio":
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "vks_api.mcp_gateway", "--profile", REQUIREMENTS_READONLY],
            env={"PYTHONPATH": str(ROOT / "services/api"), "VKS_API_URL": url, "VKS_TOKEN": token},
        )
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as sdk:
            yield sdk
    else:
        gateway = create_http_app(APIBridge(url), REQUIREMENTS_READONLY)
        with live_api(gateway) as endpoint:
            async with (
                httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client,
                streamable_http_client(endpoint + "/mcp", http_client=client) as (reader, writer, _),
                ClientSession(reader, writer) as sdk,
            ):
                yield sdk


def test_studio_links_accept_only_configured_web_origins():
    assert studio_base_url("https://studio.example.test") == "https://studio.example.test/"
    assert studio_base_url("http://[::1]:5193") == "http://[::1]:5193/"
    assert studio_base_url("") == ""
    for value in (
        "http://studio.example.test",
        "https://user@studio.example.test",
        "https://studio.example.test/?token=example",
        "https://studio.example.test/#redirect",
        "https://studio.example.test/path",
        "https://studio.example.test:65536",
        "http://127.0.0.1.example.test:5193",
        "javascript:alert(1)",
    ):
        with pytest.raises(ValueError):
            studio_base_url(value)


def test_unverifiable_legacy_section_links_only_to_version_directory(workspace):
    _, _, service, owner, pid, aid, _, _ = workspace
    service.studio_base_url = "http://127.0.0.1:5193/"
    state = service.workspace(owner, pid, aid)
    document = state["documents"][0]["entity_revision_id"]
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, str(uuid4()))
    with service.db() as session:
        row = session.get(Revision, document)
        row.metadata_json = {**row.metadata_json, "section_revision_ids": [str(uuid4())], "section_count": 1}
    hits = service.search(
        owner, pid, {"release_id": release["id"], "query": "charge.guide.create", "kinds": ["section"]}
    )
    assert hits and hits[0]["locator_status"] == "version_only"
    assert "locator" not in hits[0]
    assert parse_qs(urlsplit(hits[0]["studio_url"]).fragment) == {
        "project": [pid],
        "release": [release["id"]],
        "view": ["documents"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_context_does_not_disclose_a_hidden_primary_package(materials, transport):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    packages = [
        test_materials.stage(service, owner, manager, pid, aid, "prototype", f"<title>Fixture {i}</title>")
        for i in range(2)
    ]
    test_materials.activate(service, owner, manager, pid, aid, packages)
    primary = test_materials.selected(service, owner, pid, aid)["primary_package_id"]
    hidden = next(package for package in packages if package["package_id"] == primary)
    visible = next(package for package in packages if package["package_id"] != primary)
    release = manager.checkpoint(owner, pid, aid, "Package visibility", str(uuid4()))
    with service.db() as db:
        db.get(Artifact, hidden["artifact_id"]).sensitivity = "confidential"
    grant = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read"]})
    reader = service.authenticate(grant["token"], "token")
    assert manager.library(owner, pid, aid)["primary_package_id"] == primary
    assert manager.library(reader, pid, aid)["primary_package_id"] is None

    with live_api(web.app) as url:
        async with connect(transport, url, grant["token"]) as sdk:
            await sdk.initialize()
            result = await sdk.call_tool(
                "get_interactive_context",
                {
                    "project_id": pid,
                    "release_id": release["id"],
                    "package_id": visible["artifact_id"],
                    "target": {"route": "/", "frame_path": [], "selector": "body", "label": "Fixture"},
                },
            )
            assert not result.isError, result.content
            catalog = result.structuredContent["data"]["catalog"]
            assert [package["id"] for package in catalog["packages"]] == [visible["artifact_id"]]
            assert catalog.get("primary_package_id") is None


@pytest.mark.asyncio
async def test_saved_context_discovers_only_active_readable_versions(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    public = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# Visible handbook\n\nPublic rule.", filename="public.md"
    )
    test_materials.activate(service, owner, manager, pid, aid, [public])
    historical = manager.checkpoint(owner, pid, aid, "Original", str(uuid4()))
    private = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# Hidden handbook\n\nPrivate rule.", filename="private.md"
    )
    test_materials.activate(service, owner, manager, pid, aid, [private])
    mixed = manager.checkpoint(owner, pid, aid, "Mixed release", str(uuid4()))
    test_materials.activate(service, owner, manager, pid, aid, [private], entire=True)
    previous_active = service.releases(owner, pid)[0]["id"]
    latest = manager.checkpoint(owner, pid, aid, "Private release", str(uuid4()))
    assert service.project_context(owner, pid, release_selector="latest_saved")["scope"]["release_id"] == latest["id"]

    with service.db() as db:
        for choice in latest["manifest"]["selections"]:
            row = db.get(Revision, choice["entity_revision_id"])
            if MATERIAL_ROOT not in row.metadata_json and COMPOSITION not in row.metadata_json:
                db.get(Artifact, row.artifact_id).sensitivity = "confidential"
    grant = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read"]})
    reader = service.authenticate(grant["token"], "token")
    assert service.workspace(reader, pid, release_id=latest["id"])["documents"] == []
    assert len(service.workspace(reader, pid, release_id=mixed["id"])["documents"]) == 1

    with live_api(web.app) as url:
        async with connect("http", url, grant["token"]) as sdk:
            await sdk.initialize()
            discovered = await sdk.call_tool(
                "get_project_context", {"project_id": pid, "release_selector": "latest_saved"}
            )
            assert not discovered.isError, discovered.content
            assert discovered.structuredContent["data"]["scope"]["release_id"] == previous_active
            denied = await sdk.call_tool("get_project_context", {"project_id": pid, "release_id": latest["id"]})
            assert denied.isError and denied.structuredContent["error"]["code"] == "VKS_TARGET_UNAVAILABLE"
            for allowed in (mixed, historical):
                result = await sdk.call_tool("get_project_context", {"project_id": pid, "release_id": allowed["id"]})
                assert not result.isError, result.content
                assert result.structuredContent["data"]["scope"]["release_id"] == allowed["id"]

    expected = str(test_materials.selected(service, owner, pid, aid)["selection_version"])
    target = {"kind": "version", "target_id": latest["id"]}
    manager.trash(owner, pid, TrashMaterial(analysis_set_id=aid, **target), expected, str(uuid4()))
    assert service.project_context(owner, pid, release_selector="latest_saved")["scope"]["release_id"] == previous_active
    manager.purge_trash(
        owner, pid, PurgeMaterials(analysis_set_id=aid, targets=[target]), expected, str(uuid4())
    )
    assert service.project_context(owner, pid, release_selector="latest_saved")["scope"]["release_id"] == previous_active
    assert service.project_context(owner, pid, latest["id"])["scope"]["release_id"] == latest["id"]


@pytest.mark.asyncio
async def test_document_pages_reassemble_within_mcp_byte_budget(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    service.studio_base_url = "http://127.0.0.1:5193/"
    markdown = "# Many sections\n\n" + "💡🔎🚧\\\\" * 14_000 + "\n\n" + "".join(
        f"## Section {i}\n\nThis rule is brief.\n\n" for i in range(300)
    )
    item = test_materials.stage(service, owner, manager, pid, aid, "documents", markdown)
    test_materials.activate(service, owner, manager, pid, aid, [item])
    release = manager.checkpoint(owner, pid, aid, "Pagination fixture", str(uuid4()))
    document = service.workspace(owner, pid, release_id=release["id"])["documents"][0]
    rev = service.get_document(owner, document["entity_id"], document["entity_revision_id"])
    expected = {row["entity_revision_id"]: row["content"] for row in rev["sections"]}
    reader = service.create_token(owner, pid, {"name": "pagination reader", "scopes": ["knowledge:read"]})
    args = {
        "project_id": pid,
        "release_id": release["id"],
        "document_id": document["entity_id"],
        "revision_id": document["entity_revision_id"],
        "max_chars": 30_000,
    }
    received = {}
    offset = 0
    with live_api(web.app) as url:
        async with connect("http", url, reader["token"]) as sdk:
            await sdk.initialize()
            while offset is not None:
                result = await sdk.call_tool("get_workspace_document", {**args, "offset": offset})
                assert not result.isError, result.content[:1]
                assert len(result.model_dump_json().encode()) <= 256 * 1024
                page = result.structuredContent["data"]
                assert page["offset"] == offset
                size = 0
                for section in page["sections"]:
                    rid = section["section_revision_id"]
                    before = received.get(rid, "")
                    content = section["content"]
                    assert len(before) == section["start_offset"]
                    assert len(before) + len(content) == section["end_offset"]
                    assert section["locator"]["section_revision_id"] == rid
                    received[rid] = before + content
                    size += len(content)
                assert 0 < size <= args["max_chars"]
                next_offset = page["next_offset"]
                assert page["truncated"] == (next_offset is not None)
                if next_offset is not None:
                    assert next_offset == offset + size
                    if offset == 0:
                        assert size < args["max_chars"]
                offset = next_offset
    assert list(received.items()) == list(expected.items())


@pytest.mark.asyncio
async def test_large_binding_context_preserves_exact_locator_in_readonly_mcp(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    service.studio_base_url = "http://127.0.0.1:5193/"
    prototype = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", '<title>Fixture</title><button id="add">新增收费项</button>'
    )
    document = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Fixture\n\n" + "A rule. " * 24_000)
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    test_materials.bind(service, owner, pid, aid, prototype)
    release_id = manager.checkpoint(owner, pid, aid, "Binding budget", str(uuid4()))["id"]
    target = InteractiveKnowledgeService(service).bindings(owner, pid, release_id=release_id)[0]["target"]
    reader = service.create_token(owner, pid, {"name": "binding reader", "scopes": ["knowledge:read"]})
    args = {"release_id": release_id, "package_id": prototype["artifact_id"], "target": target}
    legacy = web.post(
        f"/api/v3/projects/{pid}/interactive/tools/context",
        headers={"Authorization": "Bearer " + reader["token"]},
        json=args,
    )
    assert legacy.is_success
    full = legacy.json()["data"]
    original = full["binding"]["sections"][0]
    assert len(original["content"]) > 180_000 and len(legacy.content) > 256 * 1024

    with live_api(web.app) as url:
        async with connect("http", url, reader["token"]) as sdk:
            await sdk.initialize()
            result = await sdk.call_tool("get_interactive_context", {"project_id": pid, **args})
            assert not result.isError, result.content[:1]
            assert len(result.model_dump_json().encode()) <= 256 * 1024
            public = result.structuredContent
            assert public["meta"]["truncated"]
            data = public["data"]
            bound = data["binding"]
            assert bound["locator"] == full["binding"]["locator"]
            assert bound["studio_url"] == full["binding"]["studio_url"]
            part = bound["sections"][0]
            assert part["revision_id"] == original["revision_id"]
            assert part["document_revision_id"] == original["document_revision_id"]
            assert 0 < len(part["content"]) < len(original["content"]) and original["content"].startswith(part["content"])
            assert data["catalog"]["documents"][0]["revision_id"] == part["document_revision_id"]
            assert data["catalog"]["documents"][0]["id"] == full["catalog"]["documents"][0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_exact_saved_reads_and_write_fence(interactive, transport):
    web, service, owner, pid, aid, strong, package, _, _ = interactive
    service.studio_base_url = "http://127.0.0.1:5193/"
    other = service.create_project(owner, {"key": "other-mcp", "name": "Other"}, str(uuid4()))["id"]
    empty_reader = service.create_token(owner, other, {"name": "reader", "scopes": ["knowledge:read"]})

    links = InteractiveKnowledgeService(service)
    before = links.catalog(owner, pid, aid)
    doc = before["documents"][0]
    original = service.get_document(owner, doc["id"], doc["revision_id"])
    changed = service.save_document(
        owner,
        doc["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": doc["revision_id"],
            "markdown": "# Repeatable billing rule\n\n" + "原文精确依据。\n" * 90,
        },
        str(original["row_version"]),
    )
    section = changed["sections"][0]
    binding_form = test_knowledge.form(aid, package, section)
    links.save(
        owner,
        pid,
        InteractiveBinding.model_validate(binding_form),
        links.catalog(owner, pid, aid)["selection_version"],
        str(uuid4()),
    )
    old = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, str(uuid4()))
    clone = KnowledgeService(service).clone(owner, pid, aid, "Automatic copy", str(uuid4()))
    newest = service.save_snapshot(owner, pid, {"analysis_set_id": clone["id"]}, str(uuid4()))
    reader = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read"]})
    scope = {"project_id": pid, "release_id": old["id"]}

    with live_api(web.app) as url:
        async with connect(transport, url, strong["token"]) as sdk:
            await sdk.initialize()
            tools = {tool.name: tool for tool in (await sdk.list_tools()).tools}
            assert set(tools) == {
                "list_projects",
                "get_project_context",
                "search_knowledge",
                "get_workspace_document",
                "get_interactive_context",
                "get_prototype_action_context",
                "get_evidence",
                "get_prototype_preview",
            }
            assert "release_id" in tools["get_workspace_document"].inputSchema["required"]
            assert "analysis_set_id" not in tools["get_workspace_document"].inputSchema["properties"]
            assert "next_offset" in tools["get_workspace_document"].outputSchema["properties"]["data"]["required"]

            async def call(name, args):
                result = await sdk.call_tool(name, args)
                assert not result.isError, (name, result.content)
                return result.structuredContent["data"]

            async def denied(name, args, code):
                result = await sdk.call_tool(name, args)
                assert result.isError and result.structuredContent["error"]["code"] == code

            assert (await call("list_projects", {}))[0]["current_release_id"] == old["id"]
            latest = await call("get_project_context", {"project_id": pid, "release_selector": "latest_saved"})
            assert latest["scope"] == {
                "project_id": pid,
                "release_id": newest["id"],
                "release_label": newest["release_no"],
                "manifest_sha256": newest["manifest_sha256"],
                "saved_at": newest["published_at"],
            }
            assert (await call("get_project_context", scope))["release"]["id"] == old["id"]
            await denied("get_project_context", {**scope, "release_selector": "latest_saved"}, "VKS_INVALID_SCOPE")
            await denied(
                "save_knowledge_snapshot",
                {"project_id": pid, "analysis_set_id": aid, "change_note": "must fail", "idempotency_key": "blocked"},
                "VKS_READONLY_PROFILE",
            )
            await denied("search_knowledge", {"project_id": pid, "query": "精确"}, "VKS_SAVED_SCOPE_REQUIRED")
            await denied(
                "search_knowledge", {**scope, "analysis_set_id": aid, "query": "精确"}, "VKS_SAVED_SCOPE_REQUIRED"
            )
            await denied("get_project_context", {**scope, "project_id": other}, "VKS_TARGET_UNAVAILABLE")
            with pytest.raises(McpError):
                await sdk.read_resource(f"vks://projects/{pid}/releases/{old['id']}/overview")
            with pytest.raises(McpError):
                await sdk.get_prompt("explain_action", scope)

            hits = await call("search_knowledge", {**scope, "query": "原文精确依据", "kinds": ["section"]})
            hit = hits[0]
            locator = hit["locator"]
            assert locator["document_revision_id"] == changed["entity_revision_id"]
            assert locator["section_revision_id"] == section["entity_revision_id"]
            assert locator["content_sha256"] == section["content_sha256"]
            fragment = parse_qs(urlsplit(hit["studio_url"]).fragment)
            assert fragment["doc"] == [locator["document_revision_id"]]
            assert fragment["section"] == [locator["section_revision_id"]]
            assert fragment["release"] == [old["id"]]
            doc_args = {
                **scope,
                "document_id": locator["document_id"],
                "revision_id": locator["document_revision_id"],
                "section_revision_ids": [locator["section_revision_id"]],
                "max_chars": 100,
            }
            page = await call("get_workspace_document", doc_args)
            assert page["truncated"] and page["next_offset"] == 100
            assert page["sections"][0]["locator"] == locator and len(page["sections"][0]["content"]) == 100
            following = await call("get_workspace_document", {**doc_args, "offset": page["next_offset"]})
            assert following["sections"][0]["start_offset"] == 100
            await denied("get_workspace_document", {**doc_args, "max_chars": None}, "VKS_INVALID_INPUT")
            await denied(
                "get_workspace_document", {**doc_args, "section_revision_ids": [str(uuid4())]}, "VKS_TARGET_UNAVAILABLE"
            )
            await denied(
                "get_workspace_document",
                {**doc_args, "revision_id": original["entity_revision_id"]},
                "VKS_TARGET_UNAVAILABLE",
            )

            bound = await call(
                "get_interactive_context",
                {
                    **scope,
                    "package_id": package,
                    "target": binding_form["target"],
                },
            )
            binding = bound["binding"]
            assert binding["locator"]["package_artifact_id"] == package
            assert binding["locator"]["release_id"] == old["id"]
            assert parse_qs(urlsplit(binding["studio_url"]).fragment)["binding"] == [binding["id"]]
            evidence = hit["evidence_ids"]
            assert (
                evidence
                and (await call("get_evidence", {**scope, "evidence_ids": [evidence[0]]}))[0]["id"] == evidence[0]
            )
            await denied(
                "get_evidence",
                {**scope, "release_id": newest["id"], "evidence_ids": [evidence[0]]},
                "VKS_TARGET_UNAVAILABLE",
            )
            if transport == "stdio":
                service.revoke_token(owner, pid, strong["id"])
                invalid = await sdk.call_tool("list_projects", {})
                assert invalid.isError and invalid.structuredContent["error"]["code"] == "VKS_AUTH_REQUIRED"

        async with connect(transport, url, reader["token"]) as sdk:
            await sdk.initialize()
            assert not (await sdk.call_tool("get_workspace_document", doc_args)).isError
        async with connect(transport, url, empty_reader["token"]) as sdk:
            await sdk.initialize()
            response = await sdk.call_tool(
                "get_project_context", {"project_id": other, "release_selector": "latest_saved"}
            )
            assert response.isError and response.structuredContent["error"]["code"] == "VKS_NO_SAVED_VERSION"

    assert len(service.releases(owner, pid)) == 2
    with service.db() as session:
        session.get(Release, newest["id"]).created_at = old["published_at"]
    tie = service.project_context(owner, pid, release_selector="latest_saved")
    assert tie["scope"]["release_id"] == max(old["id"], newest["id"])
