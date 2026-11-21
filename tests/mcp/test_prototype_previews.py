"""Real capture worker and MCP preview transport against isolated synthetic projects."""

import asyncio
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from uuid import uuid4

import pytest
from mcp import ClientSession
from PIL import Image
from playwright.async_api import Page
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.material_site import site_html
from vks_api.models import Artifact, Job, Release, Revision
from vks_api.prototype_previews import PREVIEW_PREFIX
from vks_api.service import sha

from services.browser_runner.preview import render_preview
from services.browser_runner.runner import CaptureError
from tests.interactive import test_materials
from tests.mcp.test_requirements_readonly import connect
from tests.prototype import test_v3_workflow
from tests.ui.test_frontend_browser import chromium_path, live_api

materials = test_materials.materials
workspace = test_v3_workflow.workspace


@pytest.mark.parametrize("change", ["another-page", "reopened-page", "same-page-dialog"])
def test_capture_rejects_position_changed_while_taking_screenshot(monkeypatch, change):
    with sync_playwright() as browser:
        monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", chromium_path(browser))
    material = site_html("index.html", [
        ("index.html", b"""
        <h1>Original page</h1><a href="other.html">Next page</a>
        <button onclick="notice.showModal()">Open notice</button>
        <dialog id="notice">A newly opened dialog</dialog>
        """),
        ("other.html", b'<h1>Other page</h1><a href="index.html">Back to original</a>'),
    ])
    screenshot = Page.screenshot

    async def capture_during_change(page, **options):
        frame = page.frame_locator("iframe").frame_locator("#material").frame_locator("#actualPage")
        if change == "same-page-dialog":
            await frame.get_by_role("button", name="Open notice").click(timeout=4000)
            await frame.get_by_role("dialog").wait_for(timeout=4000)
        else:
            await frame.get_by_role("link", name="Next page").click(timeout=4000)
            await frame.get_by_role("heading", name="Other page").wait_for(timeout=4000)
            if change == "reopened-page":
                await frame.get_by_role("link", name="Back to original").click(timeout=4000)
                await frame.get_by_role("heading", name="Original page").wait_for(timeout=4000)
        return await screenshot(page, **options)

    monkeypatch.setattr(Page, "screenshot", capture_during_change)
    with pytest.raises(CaptureError) as exc:
        render_preview(
            material,
            {"route": "/index.html", "frame_path": ["actualPage"]},
            None,
            {"width": 640, "height": 400},
            lambda: False,
        )
    assert exc.value.code == "VKS_TARGET_UNAVAILABLE"


def test_capture_ignores_transparent_and_collapsed_controls_and_dialogs(monkeypatch):
    with sync_playwright() as browser:
        monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", chromium_path(browser))
    page = {"route": "/", "frame_path": []}
    binding = {"target": {**page, "selector": ".save", "label": "Save", "state": "", "open_steps": []}}
    image = render_preview(
        b"""
        <div style="opacity: 0">
            <button class="save">Save</button>
            <div role="dialog">Transparent dialog</div>
        </div>
        <button class="save" style="visibility: collapse">Save</button>
        <button class="save">Save</button>
        """,
        page,
        binding,
        {"width": 640, "height": 400},
        lambda: False,
    )
    with Image.open(BytesIO(image)) as captured:
        assert captured.size == (640, 400)


def test_reviewed_page_and_binding_capture_and_single_image_sdk(materials, monkeypatch):
    web, service, actor, manager, projects = materials
    pid, aid = projects[0]
    service.studio_base_url = "http://127.0.0.1:5193/"
    with sync_playwright() as browser:
        monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", chromium_path(browser))

    received = []

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()

    probe = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
    thread = Thread(target=probe.serve_forever, daemon=True)
    thread.start()
    try:
        page_html = """
        <title>Exact preview page</title>
        <style>body { background: #22aa88; }
            body:has(dialog[open]) { background: #ffaa33; }
            dialog::backdrop { background: transparent; }</style>
        <button id="open" onclick="drawer.showModal()">Open</button>
        <dialog id="drawer" role="dialog"><button id="confirm">Confirm</button></dialog>
        <img src="PROBE/image">
        <script>fetch("PROBE/fetch").catch(()=>{});</script>
        """.replace("PROBE", f"http://127.0.0.1:{probe.server_port}")
        prototype = test_materials.stage(service, actor, manager, pid, aid, "prototype", page_html)
        docs = test_materials.stage(service, actor, manager, pid, aid, "documents", "# Preview rule\n\nRule detail.")
        test_materials.activate(service, actor, manager, pid, aid, [prototype, docs])
        links = InteractiveKnowledgeService(service)
        documents = links.catalog(actor, pid, aid)["documents"]
        chapter = links.document(actor, pid, documents[0]["revision_id"], aid)["sections"][0]["revision_id"]
        dom = {
            "route": "/",
            "frame_path": [],
            "selector": "#confirm",
            "label": "Confirm",
            "state": "#drawer",
            "open_steps": ["#open"],
        }
        binding_form = InteractiveBinding(
            analysis_set_id=aid,
            package_id=prototype["artifact_id"],
            target=dom,
            section_revision_ids=[chapter],
            point={"title": "Confirm rule", "summary": "Check the exact rule first."},
        )
        links.save(actor, pid, binding_form, links.catalog(actor, pid, aid)["selection_version"], str(uuid4()))
        frozen = manager.checkpoint(actor, pid, aid, "frozen preview", str(uuid4()))
        rid = frozen["id"]
        binding = links.bindings(actor, pid, release_id=rid)[0]
        reader = service.create_token(actor, pid, {"name": "preview reader", "scopes": ["knowledge:read"]})
        strong = service.create_token(
            actor, pid, {"name": "preview machine", "scopes": ["knowledge:read", "capture:run"]}
        )
        browser_session = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
        assert browser_session.is_success
        headers = {"X-CSRF-Token": browser_session.json()["data"]["csrf_token"]}
        prefix = f"/api/v3/projects/{pid}/interactive/previews"
        scope = {"project_id": pid, "release_id": rid}

        async def sdk_reads(expect_images):
            with live_api(web.app) as url:
                for transport in ("stdio", "http"):
                    async with connect(transport, url, reader["token"]) as sdk:
                        assert isinstance(sdk, ClientSession)
                        await sdk.initialize()
                        context = await sdk.call_tool("get_project_context", scope)
                        assert not context.isError, context.content
                        pages = context.structuredContent["data"]["prototype_pages"]
                        assert len(pages) == 1 and pages[0]["label"] == "Exact preview page"
                        assert "route" not in pages[0] and "page_ref=" in pages[0]["studio_url"]
                        related = await sdk.call_tool(
                            "get_interactive_context",
                            {**scope, "package_id": prototype["artifact_id"], "target": dom},
                        )
                        assert not related.isError, related.content
                        bound_locator = related.structuredContent["data"]["binding"]["locator"]
                        for locator in (pages[0]["locator"], bound_locator):
                            params = {**scope, "locator": locator, "max_width": 320}
                            result = await sdk.call_tool("get_prototype_preview", params)
                            assert not result.isError, result.content
                            data = result.structuredContent["data"]
                            assert data["locator"] == locator
                            assert len(result.content) == (2 if expect_images else 1)
                            assert data["status"] == ("available" if expect_images else "preview_unavailable")
                            if expect_images:
                                image = result.content[1]
                                assert image.type == "image" and image.mimeType == "image/png"
                                assert image.data not in result.content[0].text
                                assert "image_base64" not in result.structuredContent
                                raw = b64decode(image.data, validate=True)
                                assert len(raw) <= 160 * 1024 and sha(raw) == data["image"]["sha256"]
                                assert data["image"]["width"] == 320 and data["image"]["height"] == 200
                                with Image.open(BytesIO(raw)) as thumbnail:
                                    red, green, blue = thumbnail.getpixel((5, 100))
                                if locator["kind"] == "prototype_binding":
                                    assert data["state"] == "verified_binding" and (red, green, blue) == (255, 170, 51)
                                else:
                                    assert data["state"] == "default" and (red, green, blue) == (34, 170, 136)
                            elif locator["kind"] == "prototype_binding":
                                assert data["state"] == "binding_unverified"
                            mismatch = await sdk.call_tool(
                                "get_prototype_preview",
                                {**params, "locator": {**locator, "package_sha256": "0" * 64}},
                            )
                            assert (
                                mismatch.isError
                                and mismatch.structuredContent["error"]["code"] == "VKS_TARGET_UNAVAILABLE"
                            )

        asyncio.run(sdk_reads(False))
        with service.db() as db:
            assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "prototype_preview")) == 0
        registry = web.get(
            prefix + "/pages",
            params={
                "release_id": rid,
                "package": prototype["package_id"],
            },
        )
        assert registry.is_success, registry.text
        page = registry.json()["data"]["items"][0]
        assert page["route"] == "/" and page["frame_path"] == []
        revision_count = None
        with service.db() as db:
            revision_count = db.scalar(select(func.count()).select_from(Revision))
        capture = {"release_id": rid, "package_artifact_id": prototype["artifact_id"], "page_ref": page["page_ref"]}
        denied = web.post(
            prefix + "/capture",
            json=capture,
            headers={
                "Authorization": "Bearer " + strong["token"],
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert denied.status_code == 403
        bad = web.post(
            prefix + "/capture",
            json={**capture, "page_ref": "a" * 64},
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert bad.status_code == 404
        for body in (capture, {**capture, "binding_id": binding["id"]}):
            key = str(uuid4())
            request_headers = {**headers, "Idempotency-Key": key}
            response = web.post(prefix + "/capture", json=body, headers=request_headers)
            assert response.status_code == 202, response.text
            job_id = response.json()["data"]["id"]
            retry = web.post(prefix + "/capture", json=body, headers=request_headers)
            assert retry.json()["data"]["id"] == job_id
            service.process_job(job_id)
            job = service.get_job(actor, job_id)
            assert job["state"] == "succeeded", job
        asyncio.run(sdk_reads(True))
        with service.db() as db:
            assert db.scalar(select(func.count()).select_from(Revision)) == revision_count
            assert db.get(Release, rid).manifest_sha256 == frozen["manifest_sha256"]
            assets = list(db.scalars(select(Artifact).where(Artifact.relative_path.startswith(PREVIEW_PREFIX))))
            assert len(assets) == 2
            asset_id = assets[0].id
        direct = web.get(f"/api/v1/projects/{pid}/artifacts/{asset_id}/download")
        assert direct.status_code == 404
        with service.db() as db:
            default_asset = next(row for row in assets if row.metadata_json["source"]["state"] == "default")
            stored = db.get(Artifact, default_asset.id)
            stored.metadata_json = {**stored.metadata_json, "generator": "vks-isolated-chromium-v1"}
        obsolete = web.post(prefix + "/read", json={"release_id": rid, "locator": page["locator"]}, headers=headers)
        assert obsolete.is_success
        assert obsolete.json()["data"]["status"] == "preview_unavailable"
        with service.db() as db:
            db.get(Artifact, prototype["artifact_id"]).sensitivity = "confidential"
        concealed = web.post(
            prefix + "/read",
            json={"release_id": rid, "locator": page["locator"]},
            headers={"Authorization": "Bearer " + reader["token"]},
        )
        assert concealed.status_code == 404
        cancelled = web.post(prefix + "/capture", json=capture, headers={**headers, "Idempotency-Key": str(uuid4())})
        assert cancelled.status_code == 202, cancelled.text
        job_id = cancelled.json()["data"]["id"]
        service.cancel_job(actor, job_id, "synthetic cancellation")
        service.process_job(job_id)
        assert service.get_job(actor, job_id)["state"] == "cancelled"
        assert received == []
    finally:
        probe.shutdown()
        probe.server_close()
        thread.join(timeout=3)


def test_registered_raster_stays_in_its_own_artifact_and_default_state(workspace):
    _, web, service, owner, pid, aid, _, _ = workspace
    frozen = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, str(uuid4()))
    rid = frozen["id"]
    token = service.create_token(owner, pid, {"name": "raster preview", "scopes": ["knowledge:read"]})
    headers = {"Authorization": "Bearer " + token["token"]}
    overview = web.get(f"/api/v1/projects/{pid}/context", params={"release_id": rid}, headers=headers)
    assert overview.is_success, overview.text
    page = next(row for row in overview.json()["data"]["prototype_pages"] if row["format"] == "raster")
    assert not page.get("studio_url")
    raster = web.post(
        f"/api/v3/projects/{pid}/interactive/previews/read",
        json={"release_id": rid, "locator": page["locator"], "max_width": 320},
        headers=headers,
    )
    assert raster.is_success, raster.text
    envelope = raster.json()
    result = envelope["data"]
    assert result["locator"] == page["locator"] and result["state"] == "default"
    assert result["image"]["source"] == "registered_raster"
    raw = b64decode(envelope["image_base64"], validate=True)
    assert sha(raw) == result["image"]["sha256"] and result["image"]["width"] <= 320
