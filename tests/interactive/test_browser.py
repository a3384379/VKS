"""A complete Studio task against synthetic HTML, an isolated API and runtime site."""

import os
import socket
import subprocess
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
from playwright.sync_api import expect, sync_playwright

from apps.runtime.server import RuntimeHandler
from tests.interactive import test_knowledge
from tests.ui.test_frontend_browser import chromium_path, live_api

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
ROOT = Path(__file__).resolve().parents[2]


def test_original_interaction_links_refresh_and_runtime_isolation(interactive, workspace, tmp_path):
    _, _, _, pid, aid, _, _, _, _ = interactive
    app = workspace[0]
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    thread = Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    with live_api(app) as api:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = """
        import {createServer} from 'vite';
        const s=await createServer({root:process.argv[1]+'/apps/studio',configFile:process.argv[1]+'/apps/studio/vite.config.ts',cacheDir:process.argv[2],define:{'import.meta.env.VITE_RUNTIME_PORT':JSON.stringify(process.argv[5])},server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}});await s.listen();
        """
        with (tmp_path / "vite.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    str(ROOT),
                    str(tmp_path / "cache"),
                    str(port),
                    api,
                    str(runtime.server_port),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(150):
                    assert process.poll() is None, (tmp_path / "vite.log").read_text()
                    try:
                        if httpx.get(url, timeout=0.4).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
                    page = browser.new_page(viewport={"width": 1600, "height": 1000})
                    try:
                        page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
                        page.get_by_label("密码", exact=True).fill("owner")
                        page.get_by_role("button", name="打开交互工作台").click()
                        source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
                        add = source.get_by_role("button", name="Add", exact=True)
                        expect(add).to_be_visible(timeout=30000)
                        add.click()
                        field = source.get_by_label("Code", exact=True)
                        expect(field).to_be_visible()
                        field.fill("Keep this value")
                        page.get_by_role("button", name="关闭知识小窗", exact=True).click()
                        expect(field).to_have_value("Keep this value")
                        source.get_by_role("button", name="Close", exact=True).click()
                        page.locator(".contextbar").get_by_role("button", name="关联知识", exact=True).click()
                        add.click()
                        page.get_by_label("业务说明", exact=True).fill("Persistent knowledge entered through Studio")
                        doc = page.get_by_label("关联文档", exact=True)
                        doc.select_option(index=1)
                        chapter = page.get_by_label("关联章节", exact=True)
                        expect(chapter.locator("option")).not_to_have_count(1)
                        chapter.select_option(index=1)
                        page.get_by_role("button", name="保存关联", exact=True).click()
                        expect(page.locator(".knowledge-title h2")).to_have_text("Add")
                        page.locator(".knowledge-window .citation").click()
                        expect(page.locator(".chapter-selected")).to_be_in_viewport()
                        page.get_by_role("button", name="打开原型 · Add", exact=True).click()
                        expect(add).to_be_visible()
                        assert page.locator('iframe[title="原 HTML 原型"]').evaluate("(e)=>e.contentDocument===null")
                        page.reload()
                        expect(page.locator(".knowledge-title h2")).to_have_text("Add")
                        expect(add).to_be_visible()
                        add.click()
                        expect(field).to_be_visible()
                        expect(
                            page.get_by_text("Persistent knowledge entered through Studio", exact=True)
                        ).to_be_visible()
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=10)
                runtime.shutdown()
                runtime.server_close()
                thread.join(timeout=5)
