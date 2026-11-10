from base64 import b64encode
from gzip import compress
from hashlib import sha256

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from packages.analyzers.vks_ingest import canonical_json
from scripts.build_prototype_demo import PAGE_KEY, build_bundle
from tests.prototype.test_raster_prototype import import_project, original
from vks_api.app import create_app
from vks_api.knowledge import KnowledgeService
from vks_api.mcp_gateway import APIBridge, create_http_app
from vks_api.models import Artifact


@pytest.fixture
def workspace(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    service = app.state.service
    with TestClient(app) as web:
        _, owner = service.login(service.bootstrap_token)
        pid = service.create_project(owner, {"key": "prototype-demo", "name": "Prototype"}, "v3-project")["id"]
        bundle = build_bundle()
        aid = import_project(service, owner, pid, bundle, "v3-bundle")
        grant = service.create_token(
            owner,
            pid,
            {
                "name": "v3-editor",
                "scopes": ["knowledge:read", "knowledge:edit", "source:import"],
            },
        )
        yield app, web, service, owner, pid, aid, bundle, grant
    service.engine.dispose()


def _headers(grant, current, key):
    return {"Authorization": "Bearer " + grant["token"], "If-Match": str(current), "Idempotency-Key": key}


def _binding(service, owner, pid, aid):
    current = service.workspace(owner, pid, aid)
    page = current["pages"][0]
    action = next(row for row in current["actions"] if row["stable_key"] == "charge.add")
    section = next(row for row in current["entities"] if row["stable_key"] == "charge.guide.create")
    version = current["selected_analysis_set"]["selection_version"]
    return version, {
        "analysis_set_id": aid,
        "action_revision_id": action["entity_revision_id"],
        "page_revision_id": page["entity_revision_id"],
        "point_key": "point.test-create",
        "point": {"title": "新增规则", "summary": "合成业务点的说明"},
        "section_revision_ids": [section["entity_revision_id"]],
        "state_keys": ["list"],
    }


def _upload(service, owner, pid, data, name):
    pending = service.create_upload(
        owner,
        pid,
        {
            "filename": name,
            "size_bytes": len(data),
            "sha256": sha256(data).hexdigest(),
            "media_type": "text/html",
        },
    )
    service.put_upload(owner, pid, pending["upload_session_id"], data)
    return pending["artifact_id"]


def test_offline_import_receipt_and_cancellation(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    source = b"# Source\n\nRead only.\n"
    inner = (
        b'<script id="source-data" type="application/json">'
        + canonical_json(
            {"doc": {"path": "manual.md", "base64": b64encode(source).decode(), "sha256": sha256(source).hexdigest()}}
        )
        + b"</script>"
    )
    meta = canonical_json({"format": "fh-gzip-html-1", "bytes": len(inner), "sha256": sha256(inner).hexdigest()})
    data = (
        b'<script>fetch("https://untrusted.invalid")</script>'
        b'<script id="fh-pack-meta" type="application/json">' + meta + b"</script>"
        b'<script id="fh-pack-data" type="application/octet-stream">' + b64encode(compress(inner)) + b"</script>"
    )
    machine = service.authenticate(grant["token"], "token")
    asset = _upload(service, machine, pid, data, "handbook.html")
    root = f"/api/v3/projects/{pid}/prototype"
    body = {"analysis_set_id": aid, "artifact_id": asset}
    preview = web.post(root + "/import-previews", json=body, headers=_headers(grant, 0, "preview"))
    assert preview.status_code == 200, preview.text
    report = preview.json()["data"]
    version = report["selection_version"]
    assert report["prototype_import"] == {"format": "charge-handbook", "documents": 1}
    wrong = web.post(
        root + "/imports", json={**body, "preview_digest": "0" * 64}, headers=_headers(grant, version, "wrong")
    )
    wrong_id = wrong.json()["data"]["id"]
    service.process_job(wrong_id)
    assert service.get_job(owner, wrong_id)["error"]["code"] == "VKS_VERSION_CONFLICT"
    assert service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"] == version
    request = {**body, "preview_digest": report["preview_digest"]}
    pending = web.post(root + "/imports", json=request, headers=_headers(grant, version, "cancel"))
    pending_id = pending.json()["data"]["id"]
    service.cancel_job(owner, pending_id, "Synthetic cancellation")
    service.process_job(pending_id)
    assert service.get_job(owner, pending_id)["state"] == "cancelled"
    receipt = web.post(root + "/imports", json=request, headers=_headers(grant, version, "commit"))
    assert receipt.status_code == 202, receipt.text
    job_id = receipt.json()["data"]["id"]
    service.process_job(job_id)
    assert service.get_job(owner, job_id)["state"] == "succeeded"
    current = service.workspace(owner, pid, aid)
    document = next(row for row in current["documents"] if row["name"] == "Source")
    assert service.artifact_bytes(owner, pid, document["artifact_id"])[0] == source
    assert current["selected_analysis_set"]["selection_version"] > version
    retry = web.post(root + "/imports", json=request, headers=_headers(grant, version, "commit"))
    assert retry.json()["data"]["id"] == job_id
    stale = web.post(root + "/imports", json=request, headers=_headers(grant, version, "new"))
    assert stale.status_code == 409


def test_business_point_write_reverse_scope_and_history(workspace):
    _, web, service, owner, pid, aid, bundle, grant = workspace
    root = f"/api/v3/projects/{pid}"
    endpoint = root + "/prototype/actions/charge.add/business-points"
    version, body = _binding(service, owner, pid, aid)
    saved = web.post(endpoint, json=body, headers=_headers(grant, version, "binding"))
    assert saved.status_code == 200, saved.text
    result = saved.json()["data"]
    replay = web.post(endpoint, json=body, headers=_headers(grant, version, "binding"))
    assert replay.json()["data"] == result
    assert web.post(endpoint, json=body, headers=_headers(grant, version, "stale")).status_code == 409
    headers = _headers(grant, version, "read")
    context_url = root + "/prototype/actions/charge.add/context"
    params = {"page_key": PAGE_KEY, "trigger_state_key": "list", "analysis_set_id": aid}
    context = web.get(context_url, params=params, headers=headers)
    assert context.status_code == 200, context.text
    points = context.json()["data"]["points"]
    assert len(points) == 1 and points[0]["point_revision_id"] == result["point_revision_id"]
    assert points[0]["sources"][0]["section_revision_id"] == body["section_revision_ids"][0]
    assert (
        web.get(context_url, params={**params, "trigger_state_key": "edit-drawer"}, headers=headers).status_code == 404
    )
    reverse = web.get(
        root + "/sections/charge.guide.create/prototype-references", params={"analysis_set_id": aid}, headers=headers
    )
    assert reverse.json()["data"]["total"] == 1
    assert reverse.json()["data"]["references"][0]["action_key"] == "charge.add"
    current_page = web.get(
        root + f"/prototype/pages/{PAGE_KEY}", params={"analysis_set_id": aid}, headers=headers
    ).json()["data"]
    moved = []
    for anchor in current_page["anchors"]:
        item = {
            key: value
            for key, value in anchor.items()
            if key not in {"action_id", "action_revision_id"} and value is not None
        }
        if item["actionKey"] == "charge.add":
            item["rect"][0] -= 0.02
        moved.append(item)
    update = web.put(
        root + f"/prototype/pages/{PAGE_KEY}/anchors",
        json={
            "analysis_set_id": aid,
            "page_revision_id": current_page["page_revision_id"],
            "state_key": "list",
            "name": "列表",
            "anchors": moved,
        },
        headers=_headers(grant, version + 1, "move"),
    )
    assert update.status_code == 200, update.text
    params_image = {"analysis_set_id": aid, "page_revision_id": update.json()["data"]["page_revision_id"]}
    assert web.get(
        root + f"/prototype/pages/{PAGE_KEY}/states/list/image", params=params_image, headers=headers
    ).content == original(bundle)
    assert (
        web.get(context_url, params=params, headers=headers).json()["data"]["points"][0]["point_revision_id"]
        == result["point_revision_id"]
    )
    snapshot = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    portable = service.export(owner, pid, {"release_id": snapshot["id"], "format": "vksbundle"}, "portable")
    service.process_job(portable["id"])
    export_job = service.get_job(owner, portable["id"])
    assert export_job["state"] == "succeeded", export_job
    raw, _ = service.download_export(owner, pid, portable["id"])
    imported = import_project(service, owner, pid, raw, "portable-import", expect_missing=False)
    imported_page = next(
        row for row in service.workspace(owner, pid, imported)["pages"] if row["stable_key"] == PAGE_KEY
    )
    restored_surface = web.get(
        root + f"/prototype/pages/{PAGE_KEY}/states/list/image",
        params={"analysis_set_id": imported, "page_revision_id": imported_page["entity_revision_id"]},
        headers=headers,
    )
    assert restored_surface.content == original(bundle)
    imported_context = web.get(context_url, params={**params, "analysis_set_id": imported}, headers=headers)
    assert imported_context.status_code == 200, imported_context.text
    assert imported_context.json()["data"]["points"][0]["summary"] == "合成业务点的说明"
    fork = KnowledgeService(service).clone(owner, pid, aid, "Draft after release", "clone")["id"]
    edited = web.post(
        endpoint,
        json={
            **body,
            "analysis_set_id": fork,
            "page_revision_id": params_image["page_revision_id"],
            "point_revision_id": result["point_revision_id"],
            "binding_version": 1,
            "point": {"title": "新说明", "summary": "草稿更新"},
        },
        headers=_headers(grant, 1, "update"),
    )
    assert edited.status_code == 200, edited.text
    old = web.get(
        context_url,
        params={"release_id": snapshot["id"], "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=headers,
    )
    assert old.json()["data"]["points"][0]["summary"] == "合成业务点的说明"
    assert (
        web.get(context_url, params={**params, "analysis_set_id": fork}, headers=headers).json()["data"]["points"][0][
            "summary"
        ]
        == "草稿更新"
    )
    with service.db() as s:
        source = next(row for row in service._revisions(s, fork) if row.id == body["section_revision_ids"][0])
        s.get(Artifact, source.artifact_id).sensitivity = "confidential"
    reader = service.create_token(owner, pid, {"name": "limited", "scopes": ["knowledge:read"]})
    restricted = web.get(
        context_url,
        params={"release_id": snapshot["id"], "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=_headers(reader, 0, "read"),
    )
    assert restricted.status_code == 200
    assert restricted.json()["data"]["points"] == []
    assert "合成业务点的说明" not in restricted.text


@pytest.mark.asyncio
async def test_v3_official_http_sdk_reads_and_writes(workspace):
    app, _, service, owner, pid, aid, _, grant = workspace
    version, body = _binding(service, owner, pid, aid)
    gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=app)))
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + grant["token"]}
        ) as client:
            async with streamable_http_client("http://localhost:8788/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as sdk:
                    await sdk.initialize()
                    arguments = {
                        **body,
                        "action_key": "charge.add",
                        "project_id": pid,
                        "idempotency_key": "sdk-binding",
                        "selection_version": version,
                    }
                    result = await sdk.call_tool("save_business_point_binding", arguments)
                    assert not result.isError, result.structuredContent
                    repeated = await sdk.call_tool("save_business_point_binding", arguments)
                    assert repeated.structuredContent["data"] == result.structuredContent["data"]
                    query = {
                        "project_id": pid,
                        "page_key": PAGE_KEY,
                        "action_key": "charge.add",
                        "trigger_state_key": "list",
                        "analysis_set_id": aid,
                    }
                    context = await sdk.call_tool("get_prototype_action_context", query)
                    assert not context.isError, context.structuredContent
                    assert context.structuredContent["data"]["points"][0]["title"] == "新增规则"
            service.revoke_token(owner, pid, grant["id"])
            denied = await client.post("http://localhost:8788/mcp", json={})
            assert denied.status_code == 401
