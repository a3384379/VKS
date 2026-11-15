"""Real page regression with isolated API, Vite cache, data and browser profile."""

import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Thread

import httpx
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from vks_api.app import create_app

ROOT = Path(__file__).resolve().parents[2]
CODE = 'const visible = "read-only";'


def chromium_path(playwright):
    configured = os.getenv("VKS_CHROMIUM_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    if Path(playwright.chromium.executable_path).is_file():
        return playwright.chromium.executable_path
    installed = sorted(
        (Path.home() / "Library/Caches/ms-playwright").glob(
            "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
    )
    if installed:
        return str(installed[-1])
    pytest.skip("独立Chromium未安装；运行 uv run playwright install chromium")


def test_workbench_uses_current_valid_links_and_respects_user_exclusion(site):
    url, pid, _, _ = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            page.goto(url + f"/#view=workbench&project={pid}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            expect(page.locator(".knowledge-detail")).to_be_visible()
            workspace = page.request.get(url + f"/api/v1/projects/{pid}/workspace").json()["data"]
            action_ids = {e["entity_revision_id"] for e in workspace["actions"]}
            api_ids = {e["entity_revision_id"] for e in workspace["entities"] if e["type"] == "api"}
            edge = next(
                r
                for r in workspace["relations"]
                if r["from_revision_id"] in action_ids and r["to_revision_id"] in api_ids
            )
            action = next(e for e in workspace["actions"] if e["entity_revision_id"] == edge["from_revision_id"])
            page.goto(url + f"/#view=workbench&project={pid}&entity={action['entity_id']}")
            panel = page.locator(".knowledge-detail")
            panel.get_by_role("button", name="接口", exact=True).click()
            expect(panel.locator(".api-card")).not_to_have_count(0)
            csrf = page.request.get(url + "/api/v1/auth/session").json()["data"]["csrf_token"]
            reviewed = page.request.patch(
                url + f"/api/v1/relations/{edge['id']}",
                headers={"X-CSRF-Token": csrf, "If-Match": str(edge["row_version"])},
                data={"excluded": True},
            )
            assert reviewed.ok, reviewed.text()
            page.reload()
            panel.get_by_role("button", name="接口", exact=True).click()
            expect(panel.get_by_text("尚无接口依据", exact=True)).to_be_visible()
        finally:
            browser.close()


@contextmanager
def live_api(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    directory = tmp_path_factory.mktemp("vks-ui")
    if not shutil.which("node") or not (ROOT / "node_modules/vite").is_dir():
        pytest.skip("前端依赖未安装；运行 npm ci")
    app = create_app(directory / "data", enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Browser regression"}, "project")
    empty = service.create_project(owner, {"key": "empty", "name": "Empty regression"}, "empty")
    job = service.demo_import(owner, project["id"], "fixture")
    service.process_job(job["id"])
    workspace = service.workspace(owner, project["id"])
    aid = workspace["selected_analysis_set"]["id"]
    markdown = (
        "# Browser Code Regression\n\n## Code\n\n```javascript\n"
        + CODE
        + "\n```\n\n| Col | Value |\n| --- | --- |\n| UI | Readable |\n"
        + (ROOT / "fixtures/markdown-visuals.md").read_text()
    ).encode()
    upload = service.create_upload(
        owner,
        project["id"],
        {
            "filename": "browser.md",
            "size_bytes": len(markdown),
            "sha256": sha256(markdown).hexdigest(),
            "media_type": "text/markdown",
        },
    )
    service.put_upload(owner, project["id"], upload["upload_session_id"], markdown)
    job = service.import_bundle(
        owner,
        project["id"],
        {
            "kind": "documents",
            "artifact_ids": [upload["artifact_id"]],
            "analysis_set_id": aid,
        },
        "markdown",
    )
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "succeeded"
    doc = next(
        d for d in service.workspace(owner, project["id"], aid)["documents"] if d["name"] == "Browser Code Regression"
    )
    with live_api(app) as api:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        program = """
          import {createServer} from 'vite';
          const server = await createServer({
            root: process.argv[1] + '/apps/web',
            configFile: process.argv[1] + '/apps/web/vite.config.ts',
            cacheDir: process.argv[2],
            server: {host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,
              proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}
          });
          await server.listen();
        """
        log = (directory / "vite.log").open("wb")
        process = subprocess.Popen(
            ["node", "--input-type=module", "-e", program, str(ROOT), str(directory / "cache"), str(port), api],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(timeout=0.5) as client:
                for _ in range(200):
                    assert process.poll() is None, (directory / "vite.log").read_text()
                    try:
                        if client.get(url).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("隔离Vite启动超时")

            class BrowserSite(tuple):
                pass

            result = BrowserSite((url, project["id"], empty["id"], doc["id"]))
            result.service = service
            yield result
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()
    service.engine.dispose()


@pytest.mark.parametrize("failed_chunk", [False, True])
def test_owner_login_documents_all_pages_and_failed_highlight_stays_readable(site, failed_chunk):
    url, pid, empty, doc = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        context = browser.new_context(viewport={"width": 1586, "height": 992})
        page = context.new_page()
        errors = []
        blocked_chunks = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        if failed_chunk:

            def abort_highlighter(route):
                blocked_chunks.append(route.request.url)
                route.abort()

            page.route("**/src/CodeHighlight.tsx*", abort_highlighter)
        try:
            page.goto(url + f"/#view=documents&project={pid}&entity={doc}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            expect(page.locator(".document-reader")).to_contain_text(CODE)
            expect(page.locator(".document-reader table")).to_contain_text("Readable")
            if not failed_chunk:
                expect(page.locator(".document-reader .hljs-keyword")).to_have_text("const")
            else:
                expect(page.locator(".document-reader .hljs-keyword")).to_have_count(0)
                assert blocked_chunks
            # A missing optional chunk must not unmount the reader or sidebar.
            for label in (
                "项目空间",
                "知识构建",
                "业务探索",
                "变更与版本",
                "项目设置",
            ):
                page.get_by_role("navigation", name="主导航").get_by_role("button", name=label).click()
                expect(page.locator(".app-body > main")).not_to_be_empty()
                expect(page.get_by_text("当前页面暂时无法显示", exact=True)).to_have_count(0)
            page.go_back()
            expect(page.get_by_role("heading", name="变更与版本", exact=True)).to_be_visible()
            page.go_forward()
            expect(page.get_by_role("heading", name="项目设置", exact=True)).to_be_visible()
            page.goto(url + f"/#view=documents&project={empty}")
            expect(page.get_by_text("文档知识，从一份 Markdown 开始", exact=True)).to_be_visible()
            page.go_back()
            expect(page.get_by_label("当前项目", exact=True)).to_have_value(pid)
            expect(page.get_by_role("heading", name="项目设置", exact=True)).to_be_visible()
            page.goto(url + f"/#view=documents&project={pid}&entity={doc}")
            expect(page.locator(".document-reader")).to_contain_text(CODE)
            page.get_by_role("button", name="编辑", exact=True).click()
            editor = page.get_by_label("Markdown 草稿编辑器")
            changed = editor.input_value() + "\nUnsaved local text\n"
            editor.fill(changed)
            page.goto(url + f"/#view=documents&project={empty}")
            expect(page.get_by_text("草稿尚未保存，请先保存或还原本地修改", exact=True)).to_be_visible()
            expect(editor).to_have_value(changed)
            expect(page.get_by_label("当前项目", exact=True)).to_have_value(pid)
            page.get_by_role("button", name="还原修改", exact=True).click()
            page.goto(url + f"/#view=documents&project={empty}")
            expect(page.get_by_text("文档知识，从一份 Markdown 开始", exact=True)).to_be_visible()
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_local_diagrams_math_errors_zoom_and_no_external_requests(site):
    url, pid, _, doc = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        context = browser.new_context(viewport={"width": 1586, "height": 992})
        page = context.new_page()
        external = []
        errors = []
        page.on(
            "request",
            lambda request: (
                external.append(request.url)
                if request.url.startswith(("http:", "https:")) and not request.url.startswith(url + "/")
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + f"/#view=documents&project={pid}&entity={doc}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            reader = page.locator(".document-reader")
            expect(reader.locator(".katex")).to_have_count(3)
            expect(reader.locator(".katex-error")).to_have_count(1)
            for index in range(4):
                diagram = reader.locator(".diagram-block").nth(index)
                diagram.scroll_into_view_if_needed()
                expect(diagram.locator(".diagram-image img")).to_be_visible(timeout=20000)
                expect(diagram.locator(".diagram-image img")).to_have_js_property("complete", True)
                assert diagram.locator("img").evaluate("image => image.naturalWidth > 0 && image.naturalHeight > 0")
                text = diagram.locator("img").evaluate(
                    "async image => new DOMParser().parseFromString(await (await fetch(image.src)).text(), 'image/svg+xml').documentElement.textContent"
                )
                assert ("导入文档", "打开文档", "文档中心", "Document")[index] in text
                before_zoom = diagram.locator("img").bounding_box()["width"]
                diagram.get_by_role("button", name="放大图表").click()
                expect(diagram.get_by_role("button", name="重置图表缩放")).to_have_text("125%")
                assert diagram.locator("img").bounding_box()["width"] > before_zoom * 1.2
                diagram.get_by_role("button", name="重置图表缩放").click()
                with page.expect_download() as download:
                    diagram.get_by_role("link", name="下载 SVG").click()
                assert download.value.suggested_filename.endswith(".svg")
            broken = reader.locator(".diagram-block").nth(4)
            broken.scroll_into_view_if_needed()
            expect(broken.locator(".diagram-status")).to_contain_text("未能渲染", timeout=20000)
            expect(broken.locator("pre")).to_contain_text("unfinished")
            broken_uml = reader.locator(".diagram-block").nth(5)
            broken_uml.scroll_into_view_if_needed()
            expect(broken_uml.locator("img, details[open]").first).to_be_visible(timeout=20000)
            if broken_uml.locator("img").count():
                text = broken_uml.locator("img").evaluate("async image => (await (await fetch(image.src)).text())")
                raise AssertionError("invalid UML returned SVG: " + text[:5000])
            expect(broken_uml.locator(".diagram-status")).to_contain_text("未能渲染", timeout=20000)
            expect(reader).to_contain_text("这段正文在图表错误之后仍能阅读")
            assert external == []
            assert errors == []
            sanitizing = page.evaluate("""async () => {
                const {sanitizedSvg} = await import('/src/diagram-renderer.ts');
                const output = sanitizedSvg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><script>alert(1)</script><a href="https://outside.test"><text>Safe label</text></a><rect width="10" height="10" onload="alert(1)"/></svg>');
                let blocked = 0;
                for (const value of [
                  '<svg xmlns="http://www.w3.org/2000/svg" style="fill:url(https://outside.test/x)"></svg>',
                  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 99999 99999"></svg>',
                  '<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'
                ]) { try { sanitizedSvg(value); } catch { blocked++; } }
                return {output, blocked};
            }""")
            assert "<script" not in sanitizing["output"] and "onload=" not in sanitizing["output"]
            assert "href=" not in sanitizing["output"] and "Safe label" in sanitizing["output"]
            assert sanitizing["blocked"] == 3
            assert external == []
            assert errors == []
        finally:
            context.close()
            browser.close()


def test_failed_diagram_frame_is_isolated_readable_retryable_and_cancelled_on_navigation(site):
    url, pid, _, doc = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1586, "height": 992})
        page.route("**/src/diagram-frame.ts*", lambda route: route.abort())
        try:
            page.goto(url + f"/#view=overview&project={pid}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            expect(page.get_by_role("navigation", name="主导航")).to_be_visible()
            workspace = page.request.get(url + f"/api/v1/projects/{pid}/workspace").json()["data"]
            document = next(d for d in workspace["documents"] if d["entity_id"] == doc)
            revision = page.request.get(
                url + f"/api/v1/documents/{doc}/revisions/{document['entity_revision_id']}"
            ).json()["data"]
            csrf = page.request.get(url + "/api/v1/auth/session").json()["data"]["csrf_token"]
            # One actual document with one render job: another visible diagram's
            # frame must not start this assertion's per-job timeout window.
            saved = page.request.put(
                url + f"/api/v1/documents/{doc}/draft",
                headers={"X-CSRF-Token": csrf, "If-Match": str(revision["row_version"])},
                data={
                    "analysis_set_id": workspace["selected_analysis_set"]["id"],
                    "base_revision_id": revision["entity_revision_id"],
                    "locked_section_ids": [],
                    "markdown": "# Isolated timeout\n\n```mermaid\nflowchart LR\n A[Start] --> B[Finish]\n```\n\n正文仍可阅读\n",
                },
            )
            assert saved.ok, saved.text()
            page.goto(url + f"/#view=documents&project={pid}&entity={doc}")
            page.reload()  # Reload the revision saved through the fixture API.
            # Establish the fresh document before inspecting its render job;
            # cold browser/module startup is separate from diagram timeout.
            expect(page.get_by_role("heading", name="Isolated timeout", level=1, exact=True)).to_be_visible(timeout=15000)
            expect(page.locator(".document-reader .diagram-block")).to_have_count(1)
            first = page.locator(".document-reader .diagram-block").nth(0)
            first.scroll_into_view_if_needed()
            expect(page.locator("iframe.diagram-render-frame")).to_have_count(1)
            for frame in page.locator("iframe.diagram-render-frame").all():
                assert frame.get_attribute("sandbox") == "allow-scripts"
                assert frame.evaluate("frame => frame.contentDocument === null")
            expect(first.locator(".diagram-status")).to_contain_text("未能渲染", timeout=20000)
            expect(first.locator("pre")).to_contain_text("flowchart LR")
            expect(page.get_by_role("navigation", name="主导航")).to_be_visible()
            page.unroute("**/src/diagram-frame.ts*")
            first.get_by_role("button", name="重新渲染").click()
            expect(first.locator(".diagram-image img")).to_be_visible(timeout=20000)
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="项目空间").click()
            expect(page.locator("iframe.diagram-render-frame")).to_have_count(0)
        finally:
            browser.close()
