"""A complete Studio task against synthetic HTML, an isolated API and runtime site."""

import json
import os
import socket
import subprocess
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
from playwright.sync_api import expect, sync_playwright

from apps.runtime.server import RuntimeHandler
from tests.interactive import test_knowledge
from tests.ui.test_frontend_browser import chromium_path, live_api

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
ROOT = Path(__file__).resolve().parents[2]


def test_original_interaction_links_refresh_and_runtime_isolation(interactive, workspace, tmp_path, monkeypatch):
    from vks_api.model_connections import ConnectionService

    monkeypatch.setattr(
        ConnectionService,
        "_request",
        lambda *args: {"data": [{"id": "gpt-6.1-terra", "name": "Display name from service"}]},
    )
    monkeypatch.delenv("VKS_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("VKS_RUNTIME_DATA_DIR", str(tmp_path / "runtime-private"))
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
                monkeypatch.setenv("VKS_STUDIO_ALLOWED_ORIGINS", url)
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
                        panel = page.get_by_role("complementary", name="知识助手")

                        def options():
                            if not panel.is_visible():
                                page.get_by_role("button", name="打开知识助手", exact=True).click()
                            if not page.get_by_role("region", name="助手选项").is_visible():
                                page.get_by_role("button", name="助手选项", exact=True).click()

                        expect(panel).not_to_be_visible()
                        expect(page.get_by_role("button", name="打开知识助手", exact=True)).to_be_visible()
                        assert page.locator(".assistant-toolbar").count() == 0
                        assert page.locator('iframe[title="原 HTML 原型"]').bounding_box()["y"] == 114
                        options()
                        page.get_by_label("自动关联模块与控件").uncheck()
                        page.get_by_role("button", name="侧边展开", exact=True).click()
                        expect(panel).to_have_class("knowledge-window docked")
                        page.get_by_role("button", name="浮动小窗", exact=True).click()
                        page.get_by_role("button", name="收起助手面板", exact=True).click()
                        add.press("Alt+a")
                        add.press("Alt+d")
                        expect(panel).not_to_be_visible()
                        add.click(button="right")
                        menu = page.get_by_role("menu", name="控件知识菜单")
                        expect(menu).to_be_visible()
                        assert "Alt" not in menu.inner_text()
                        page.keyboard.press("Escape")
                        expect(menu).not_to_be_visible()
                        add.click(button="right")
                        menu.get_by_role("menuitem", name="AI 讲解此功能", exact=True).click()
                        expect(
                            page.get_by_text("请在 AI 设置中保存并启用模型，允许使用本项目获授权的相关内容", exact=True)
                        ).to_be_visible()
                        add.click()
                        field = source.get_by_label("Code", exact=True)
                        expect(field).to_be_visible()
                        field.press("Alt+a")
                        expect(page.locator(".assistant-loading")).not_to_be_visible()
                        field.fill("Keep this value")
                        page.get_by_role("button", name="收起助手面板", exact=True).click()
                        expect(field).to_have_value("Keep this value")
                        source.get_by_role("button", name="Close", exact=True).click()
                        options()
                        page.get_by_role("button", name="手动补充关联", exact=True).click()
                        add.click()
                        page.get_by_label("业务说明", exact=True).fill("Persistent knowledge entered through Studio")
                        doc = page.get_by_label("关联文档", exact=True)
                        doc.select_option(index=1)
                        chapter = page.get_by_label("关联章节", exact=True)
                        expect(chapter.locator("option")).not_to_have_count(1)
                        chapter.select_option(index=1)
                        page.get_by_role("button", name="保存关联", exact=True).click()
                        expect(page.locator(".knowledge-title h2")).to_have_text("Add")
                        page.locator(".knowledge-window .source-disclosure summary").first.click()
                        page.locator(".knowledge-window .citation").click()
                        page.get_by_role("dialog", name="引用原文预览", exact=True).get_by_role("button", name="打开文档原文", exact=True).click()
                        expect(page.locator(".chapter-selected")).to_be_in_viewport()
                        expect(page.locator(".document-reader .reverse-link")).to_have_count(0)
                        entry = page.locator(".document-reader").get_by_role("button", name="查看原型入口", exact=True).first
                        entry.click()
                        entries = page.get_by_role("dialog", name="原型入口", exact=True)
                        entries.get_by_role("button", name="打开原型", exact=True).click()
                        expect(page.get_by_text("已打开原型页面", exact=True)).to_be_visible()
                        page.locator(".contextbar").get_by_role("button", name="返回文档", exact=True).click()
                        entry.click()
                        entries.get_by_text("关联控件（1）", exact=True).click()
                        entries.get_by_role("listitem").filter(has_text="Add").get_by_role("button", name="打开原型").click()
                        expect(add).to_be_visible()
                        assert page.locator('iframe[title="原 HTML 原型"]').evaluate("(e)=>e.contentDocument===null")
                        page.reload()
                        expect(panel).not_to_be_visible()
                        page.get_by_role("button", name="打开知识助手", exact=True).click()
                        expect(page.locator(".knowledge-title h2")).to_have_text("Add")
                        expect(add).to_be_visible()
                        options()
                        expect(page.get_by_label("自动关联模块与控件")).not_to_be_checked()
                        page.get_by_role("button", name="收起助手面板", exact=True).click()
                        add.press("Alt+d")
                        expect(panel).not_to_be_visible()
                        add.click(button="right")
                        page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
                        expect(page.locator(".source-disclosure:visible")).not_to_have_attribute("open", "")
                        expect(page.locator(".inline-preview")).to_have_count(0)
                        add.click()
                        expect(field).to_be_visible()
                        expect(
                            page.get_by_text("Persistent knowledge entered through Studio", exact=True)
                        ).to_be_visible()
                        # Only synthetic values enter this temporary API instance.
                        page.get_by_role("button", name="AI 设置", exact=True).click()
                        key_field = page.get_by_label("API Key", exact=True)
                        expect(key_field).to_be_visible()
                        page.get_by_label("回答模型", exact=True).select_option("gpt-6.1-terra")
                        key_field.fill("synthetic-browser-credential-not-a-real-key")
                        page.get_by_role("button", name="保存并使用", exact=True).click()
                        expect(page.get_by_text("已保留密钥", exact=True)).to_be_visible()
                        expect(key_field).to_have_value("")
                        assert "synthetic-browser-credential" not in page.locator("body").inner_text()
                        expect(page.get_by_label("回答模型", exact=True)).to_have_value("gpt-6.1-terra")
                        page.reload()
                        expect(page.get_by_label("回答模型", exact=True)).to_have_value("gpt-6.1-terra")
                        expect(
                            page.locator('#ai-model-select optgroup option')
                        ).to_have_text(["gpt-6.1-terra"])
                        page.locator(".ai-provider-pills").get_by_role("button", name="DeepSeek", exact=True).click()
                        expect(page.get_by_role("button", name="保存并使用", exact=True)).to_be_disabled()
                        expect(key_field).to_have_value("")
                        # A real application transaction, with only the external model
                        # substituted: module links coexist with the authored Add link.
                        from vks_api import ai_gateway

                        model_started = Event()
                        model_release = Event()

                        def model(config, body):
                            model_started.set()
                            model_release.wait(timeout=5)
                            data = json.loads(body["input"][1]["content"])
                            rows = data["chapters"]
                            answer = {
                                "title": "收费项模块知识"
                                if data["context"]["scope"] == "module"
                                else "控件：" + data["control"]["label"],
                                "explanation": "自动生成的模块说明",
                                "section_revision_ids": [rows[0]["revision_id"]],
                                "confidence": 0.95,
                                "unknowns": [],
                            }
                            return {
                                "status": "completed",
                                "usage": {"input_tokens": 100, "output_tokens": 50},
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [{"type": "output_text", "text": json.dumps(answer)}],
                                    }
                                ],
                            }

                        monkeypatch.setattr(ai_gateway, "request_model", model)
                        page.get_by_role("button", name="原型", exact=True).click()
                        options()
                        page.get_by_label("自动关联模块与控件").check()
                        expect(page.get_by_role("button", name="模块知识", exact=True)).to_be_enabled()
                        page.get_by_role("button", name="模块知识", exact=True).click()
                        expect(page.get_by_role("region", name="相关文档检索结果")).to_be_visible()
                        assert model_started.wait(timeout=3)
                        page.locator(".knowledge-window .related-results .source-disclosure summary").first.click()
                        expect(page.locator(".knowledge-window .citation").first).to_be_visible()
                        expect(page.get_by_role("tab", name="文档", exact=True)).to_be_enabled()
                        with page.expect_response(lambda response: response.url.endswith("/assist")):
                            model_release.set()
                        page.get_by_role("tab", name="AI 讲解", exact=True).click()
                        expect(page.get_by_role("region", name="AI 功能讲解")).to_contain_text("自动生成的模块说明")
                        page.locator(".knowledge-window .assistant-answer .source-disclosure summary").first.click()
                        page.locator(".knowledge-window .citation").first.click()
                        page.get_by_role("dialog", name="引用原文预览", exact=True).get_by_role("button", name="打开文档原文", exact=True).click()
                        expect(page.locator(".chapter-selected")).to_be_in_viewport()
                        page.locator(".document-reader").get_by_role("button", name="查看原型入口", exact=True).first.click()
                        entries = page.get_by_role("dialog", name="原型入口", exact=True)
                        entries.get_by_role("article").filter(has_text="收费项管理").first.get_by_role("button", name="打开原型").first.click()
                        expect(page.get_by_text("已定位到 收费项管理", exact=True)).to_be_visible()
                        page.reload()
                        expect(panel).not_to_be_visible()
                        page.get_by_role("button", name="打开知识助手", exact=True).click()
                        expect(page.locator(".knowledge-title h2")).to_have_text("收费项模块知识")
                        page.get_by_role("button", name="收起助手面板", exact=True).click()
                        model_started.clear()
                        model_release.clear()
                        add.click()
                        expect(field).to_be_visible()
                        source.get_by_role("button", name="Close", exact=True).click()
                        expect(field).not_to_be_visible()
                        assert model_started.wait(timeout=3)
                        expect(panel).not_to_be_visible()
                        model_release.set()
                        expect(page.locator(".assistant-activity")).not_to_be_visible(timeout=10000)
                        expect(panel).not_to_be_visible()
                        expect(page.locator(".assistant-badge")).to_be_visible()
                        page.get_by_role("button", name="打开知识助手", exact=True).click()
                        expect(page.get_by_role("region", name="相关文档检索结果")).to_be_visible()
                        page.get_by_role("tab", name="AI 讲解", exact=True).click()
                        expect(page.get_by_role("region", name="AI 功能讲解")).to_contain_text("控件：Close")
                        expect(page.locator(".assistant-provenance")).to_contain_text("根据已关联章节生成讲解")
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=10)
                runtime.shutdown()
                runtime.server_close()
                thread.join(timeout=5)
