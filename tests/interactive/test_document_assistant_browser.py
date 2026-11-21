"""Original handbook assistant UI over shared platform/export retrieval."""

import base64
import gzip
import json
import re
from contextlib import nullcontext
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.interactive_ai import InteractiveAssistant, InteractiveQuestionInput
from vks_api.readonly_export import html_library

from tests.interactive import test_assistant, test_chat_streaming, test_materials
from tests.interactive.test_chat_source_identity import prepared
from tests.interactive.test_native_documents_browser import login, native_studio, source_frame
from tests.interactive.test_native_export import project as native_export_project
from tests.ui import test_readonly_reader
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
materials = test_materials.materials
reader_file = test_readonly_reader.reader_file
streaming_provider = test_chat_streaming.streaming_provider

SOURCE_MARKDOWN = """## 资料清单

| 来源 | 用途 |
| --- | --- |
| `收费规则.md` | **核对金额** |
| 业务手册 | 保留待确认条件 |

- 操作步骤
  - 核对收费规则

```text
## 这是代码，不是标题
| 保留代码竖线 |
```

<script>window.sourcePreviewExecuted = true</script>
[危险链接](javascript:alert(1))
"""


@pytest.mark.parametrize("host", ["studio", "export"])
@pytest.mark.parametrize("source_kind", ["markdown", "native"])
def test_selection_offers_explanation_without_opening_or_focusing_assistant(
    host, source_kind, materials, reader_file, tmp_path, monkeypatch
):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    content = "# 选中文字\n\n第一段业务内容需要解释。\n\n第二段保留阅读和复制。"
    if source_kind == "native":
        content = "<title>选中文字</title><h1>选中文字</h1><p>第一段业务内容需要解释。</p><p>第二段保留阅读和复制。</p>"
    if host == "studio":
        document = test_materials.stage(
            service,
            owner,
            manager,
            pid,
            aid,
            "documents",
            content,
            filename="selection.html" if source_kind == "native" else "selection.md",
        )
        test_materials.activate(service, owner, manager, pid, aid, [document])
        destination = native_studio(web.app, tmp_path, monkeypatch)
    elif source_kind == "native":
        payload, _, _, _ = native_export_project(materials, content, "selection.html")
        reader_file.write_bytes(html_library(payload))
        destination = nullcontext(reader_file.as_uri())
    else:
        packed = re.search(rb'<script[^>]*id="vks-payload"[^>]*>(.*?)</script>', reader_file.read_bytes(), re.DOTALL)[1]
        payload = json.loads(gzip.decompress(base64.b64decode(packed)))
        payload["documents"] = [
            {
                "id": "selection",
                "title": "选中文字",
                "path": "selection.md",
                "sections": [{"id": "selection-section", "title": "选中文字", "content": content}],
            }
        ]
        payload["rag"]["chunks"] = []
        payload["bindings"] = []
        reader_file.write_bytes(html_library(payload))
        destination = nullcontext(reader_file.as_uri())
    with destination as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors, external, questions = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: external.append(request.url) if request.url.startswith("https:") else None)
        page.on(
            "request",
            lambda request: (
                questions.append(request.url)
                if request.method == "POST" and request.url.endswith("/conversation")
                else None
            ),
        )
        page.route("https://**/*", lambda route: route.abort())
        try:
            if host == "studio":
                login(page, url, pid, aid)
            else:
                page.goto(url)
                page.get_by_role("button", name="知识文档", exact=True).click()
            reading = (
                page
                if source_kind == "markdown"
                else source_frame(page)
                if host == "studio"
                else page.frame_locator("iframe.native-document-frame")
            )
            first = reading.get_by_text("第一段业务内容需要解释。", exact=True)
            second = reading.get_by_text("第二段保留阅读和复制。", exact=True)

            def select_text(node):
                node.evaluate(
                    "node => {const r=document.createRange();r.selectNodeContents(node);const s=getSelection();s.removeAllRanges();s.addRange(r);}"
                )
                node.dispatch_event("mouseup")

            expect(page.get_by_role("button", name="打开知识助手", exact=True)).to_be_visible()
            if host == "studio" and source_kind == "native":
                advanced = page.locator(".document-advanced-tools")
                expect(advanced.locator("summary")).to_have_text("高级")
                expect(page.get_by_role("button", name="外部资源访问", exact=True)).to_be_hidden()
                advanced.locator("summary").click()
                page.get_by_role("button", name="外部资源访问", exact=True).click()
                settings = page.get_by_role("region", name="外部资源访问", exact=True)
                expect(settings).to_contain_text("平台 AI 连接请在“AI 设置”中配置")
                expect(settings.get_by_label("文档允许联网地址")).to_have_value(re.compile(r"https://api.openai.com"))
                settings.get_by_role("button", name="关闭外部资源访问", exact=True).click()
                advanced.locator("summary").click()
            select_text(first)
            action = page.get_by_role("menuitem", name="解释选中文字", exact=True)
            expect(action).to_have_count(0)
            first.click(button="right", position={"x": 15, "y": 10})
            expect(action).to_be_visible()
            bounds = action.bounding_box()
            assert 0 <= bounds["x"] < 1600 and 0 <= bounds["y"] < 1000
            if host == "studio" and source_kind == "native":
                first.dispatch_event("keydown", {"key": "Escape", "bubbles": True})
            else:
                page.keyboard.press("Escape")
            expect(action).to_have_count(0)
            first.click(button="right", position={"x": 15, "y": 10})
            expect(action).to_be_visible()
            first.click(position={"x": 15, "y": 10})
            expect(action).to_have_count(0)
            select_text(first)
            first.click(button="right", position={"x": 15, "y": 10})
            expect(action).to_be_visible()
            expect(page.locator(".document-ai-window:visible")).to_have_count(0)
            assert first.evaluate("getSelection().toString()") == "第一段业务内容需要解释。"
            action.click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(panel.locator("#fh-ai-selection-text")).to_have_text("第一段业务内容需要解释。")
            expect(panel.locator("#fh-ai-question")).to_have_value("请解释这段内容的业务含义和需要注意的边界。")
            expect(action).to_have_count(0)
            panel.locator("#fh-ai-question").fill("保留我的草稿")
            panel.locator("#fh-ai-close").click()
            select_text(second)
            expect(action).to_have_count(0)
            expect(page.locator(".document-ai-window:visible")).to_have_count(0)
            page.get_by_role(
                "button",
                name="关联知识" if host == "studio" and source_kind == "native" else "打开知识助手",
                exact=True,
            ).click()
            expect(panel.locator("#fh-ai-question")).to_have_value("保留我的草稿")
            with page.expect_popup() as opened:
                panel.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            popup_panel = popup.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(popup.locator('iframe[title="知识手册 AI 助手"]')).to_have_attribute("data-vks-view-state", "ready")
            expect(popup_panel.locator("#fh-ai-question")).to_have_value("保留我的草稿")
            popup_panel.locator("#fh-ai-question").evaluate(
                "node => {window.selectionFocusCalls=0;const original=node.focus.bind(node);node.focus=(...args)=>{window.selectionFocusCalls++;original(...args);};}"
            )
            select_text(first)
            expect(action).to_have_count(0)
            first.click(button="right", position={"x": 15, "y": 10})
            expect(action).to_be_visible()
            # Keyboard selection changes do not emit mouseup; the context menu
            # reads the range at the moment it is requested.
            second.evaluate(
                "node => {const r=document.createRange();r.selectNodeContents(node);const s=getSelection();s.removeAllRanges();s.addRange(r);node.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:0,clientY:0}));}"
            )
            expect(action).to_be_visible()
            assert popup_panel.locator("body").evaluate("window.selectionFocusCalls") == 0
            expect(popup_panel.locator("#fh-ai-question")).to_have_value("保留我的草稿")
            action.click()
            expect(popup_panel.locator("#fh-ai-question")).to_have_value("请解释这段内容的业务含义和需要注意的边界。")
            assert popup_panel.locator("body").evaluate("window.selectionFocusCalls") == 1
            expect(popup_panel.locator("#fh-ai-selection-text")).to_have_text("第二段保留阅读和复制。")
            popup_panel.locator("#fh-ai-new").click()
            expect(popup_panel.locator("#fh-ai-selection")).to_be_hidden()
            popup_panel.locator("#fh-ai-question").fill("解释后继续我的草稿")
            popup.get_by_role("button", name="收回页面", exact=True).click()
            expect(panel.locator("#fh-ai-question")).to_have_value("解释后继续我的草稿")
            expect(panel.locator("#fh-ai-selection")).to_be_hidden()
            assert not external and not errors and not questions
        finally:
            browser.close()


def test_document_assistant_preferences_are_compact_and_optional_backup_retains_actions(
    materials, tmp_path, monkeypatch
):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    document = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 助手设置样例\n\n只读设置布局检查。"
    )
    test_materials.activate(service, owner, manager, pid, aid, [document])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors, conversations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                conversations.append(request.url)
                if request.method == "POST" and request.url.endswith("/conversation")
                else None
            ),
        )
        page.route("https://**/*", lambda route: route.abort())
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            frame = page.locator('iframe[title="知识手册 AI 助手"]')
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(panel.locator("#fh-ai-panel")).to_have_attribute("data-history-ready", "true")
            panel.get_by_role("tab", name="助手设置", exact=True).click()
            settings = panel.locator("#fh-ai-tab-settings")
            expect(settings).to_be_visible()
            backup = settings.locator("#fh-ai-history-backup")
            expect(backup).not_to_have_attribute("open", "")
            expect(settings.locator("#fh-ai-browser-history-status")).to_contain_text("浏览器")
            expect(settings.get_by_role("button", name="选择本地目录", exact=True)).to_be_hidden()
            expect(settings.get_by_role("button", name="模型连接设置", exact=True)).to_be_visible()
            panel.get_by_role("button", name="浮动小窗", exact=True).click()
            expect(page.locator(".document-ai-window")).to_have_attribute("data-window-mode", "floating")
            panel.get_by_role("button", name="侧边展开", exact=True).click()
            expect(page.locator(".document-ai-window")).to_have_attribute("data-window-mode", "docked")
            panel.get_by_role("button", name="恢复默认位置与大小", exact=True).click()
            panel.locator("#fh-ai-reading-speed").select_option("fast")
            assert page.evaluate("localStorage.getItem('vks.ai.display-speed')") == "fast"
            assert settings.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert settings.locator(".fh-ai-history-card").bounding_box()["height"] < 160
            assert settings.locator(".fh-ai-window-options").bounding_box()["height"] < 100
            screenshot_dir = Path("output/playwright/settings-layout-20261116")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            frame.screenshot(path=str(screenshot_dir / "document-assistant-settings-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            expect(settings).to_be_visible()
            assert settings.evaluate("node => node.scrollWidth <= node.clientWidth")
            frame.screenshot(path=str(screenshot_dir / "document-assistant-settings-narrow.png"))
            page.set_viewport_size({"width": 1600, "height": 1000})
            with page.expect_popup() as opened:
                panel.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            popup.on("pageerror", lambda error: errors.append(str(error)))
            popup.set_viewport_size({"width": 1000, "height": 840})
            detached = popup.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(detached.get_by_role("tab", name="助手设置", exact=True)).to_have_attribute("aria-selected", "true")
            expect(detached.locator("#fh-ai-reading-speed")).to_have_value("fast")
            expect(detached.locator("#fh-ai-history-backup")).not_to_have_attribute("open", "")
            intro_bounds = detached.locator(".fh-ai-settings-intro").bounding_box()
            assert intro_bounds["width"] <= 681
            for group in (".fh-ai-window-options", ".fh-ai-reading-settings", ".fh-ai-history-card"):
                assert abs(detached.locator(group).bounding_box()["x"] - intro_bounds["x"]) < 1
            popup.locator('iframe[title="知识手册 AI 助手"]').screenshot(
                path=str(screenshot_dir / "document-assistant-settings-detached.png")
            )
            popup.get_by_role("button", name="收回页面", exact=True).click()
            expect(settings).to_be_visible()
            expect(panel.locator("#fh-ai-reading-speed")).to_have_value("fast")
            backup.locator("summary").click()
            expect(settings.get_by_role("button", name="选择本地目录", exact=True)).to_be_visible()
            panel.locator("body").evaluate("""() => {
                const disk = window.syntheticHistoryDisk = {permission:'prompt', file:'', writes:0, requests:0};
                const handle = {
                    name:'合成备份目录',
                    queryPermission:async()=>disk.permission,
                    requestPermission:async()=>{disk.requests++;return disk.permission;},
                    getFileHandle:async(name, options={})=>{
                        if (!disk.file && !options.create) throw new DOMException('Missing fixture', 'NotFoundError');
                        return {getFile:async()=>new File([disk.file],name,{type:'application/json'}),
                            createWritable:async()=>({write:async text=>{disk.file=String(text);},close:async()=>{disk.writes++;},abort:async()=>{}})};
                    }
                };
                window.showDirectoryPicker=async()=>handle;
            }""")
            settings.get_by_role("button", name="选择本地目录", exact=True).click()
            expect(settings.locator("#fh-ai-disk-history-status")).to_contain_text("恢复目录访问")
            assert panel.locator("body").evaluate("window.syntheticHistoryDisk.writes") == 0
            panel.locator("body").evaluate("window.syntheticHistoryDisk.permission='granted'")
            settings.get_by_role("button", name="恢复目录访问 / 读取", exact=True).click()
            expect(settings.locator("#fh-ai-disk-history-status")).to_contain_text("已自动保存", timeout=10000)
            first_writes = panel.locator("body").evaluate("window.syntheticHistoryDisk.writes")
            assert first_writes > 0
            settings.get_by_role("button", name="立即保存", exact=True).click()
            expect(settings.get_by_role("button", name="立即保存", exact=True)).to_be_enabled()
            assert panel.locator("body").evaluate("window.syntheticHistoryDisk.writes") > first_writes
            saved = panel.locator("body").evaluate("window.syntheticHistoryDisk.file")
            settings.get_by_role("button", name="断开目录", exact=True).click()
            expect(settings.locator("#fh-ai-disk-history-status")).to_contain_text("已断开目录")
            expect(settings.get_by_role("button", name="恢复目录访问 / 读取", exact=True)).to_be_disabled()
            settings.locator("#fh-ai-import-history-file").set_input_files(
                {"name": "synthetic-history.json", "mimeType": "application/json", "buffer": saved.encode()}
            )
            expect(panel.locator("#fh-ai-toast")).to_contain_text("历史会话已导入")
            expect(settings.locator("#fh-ai-browser-history-status")).to_contain_text("浏览器")
            page.set_viewport_size({"width": 1600, "height": 1000})
            panel.get_by_role("button", name="模型连接设置", exact=True).click()
            expect(page.get_by_role("heading", name="AI 设置", exact=True)).to_be_visible()
            assert not errors and not conversations
        finally:
            browser.close()


@pytest.mark.parametrize("host", ["studio", "export"])
def test_source_preview_renders_markdown_and_preserves_inert_code(host, materials, reader_file, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    if host == "studio":
        document = test_materials.stage(service, owner, manager, pid, aid, "documents", SOURCE_MARKDOWN)
        test_materials.activate(service, owner, manager, pid, aid, [document])
        destination = native_studio(web.app, tmp_path, monkeypatch)
    else:
        packed = re.search(rb'<script[^>]*id="vks-payload"[^>]*>(.*?)</script>', reader_file.read_bytes(), re.DOTALL)[1]
        payload = json.loads(gzip.decompress(base64.b64decode(packed)))
        payload["documents"] = [
            {
                "id": "preview",
                "title": "预览验收",
                "path": "preview.md",
                "sections": [{"id": "source", "title": "资料清单", "content": SOURCE_MARKDOWN}],
            }
        ]
        payload["rag"]["chunks"] = []
        payload["bindings"] = []
        reader_file.write_bytes(html_library(payload))
        destination = nullcontext(reader_file.as_uri())
    with destination as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: external.append(request.url) if request.url.startswith("https:") else None)
        page.route("https://**/*", lambda route: route.abort())
        try:
            if host == "studio":
                login(page, url, pid, aid)
            else:
                page.goto(url)
                page.get_by_role("button", name="知识文档", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            panel.locator("#fh-ai-tools-toggle").click()
            panel.locator("#fh-ai-mode").select_option("local")
            panel.locator("#fh-ai-question").fill("资料清单 核对收费规则")
            panel.locator("#fh-ai-send").click()
            evidence = panel.locator("details.fh-ai-sources").first
            expect(evidence).not_to_have_attribute("open", "")
            expect(evidence.locator(".fh-ai-source-excerpt")).to_have_count(0)
            evidence.locator("summary").click()
            expect(evidence.locator(".fh-ai-source").first).to_be_visible()
            expect(evidence.locator(".fh-ai-source-excerpt,table,pre")).to_have_count(0)
            evidence.locator(".fh-ai-source").first.click()
            preview = panel.locator("#fh-ai-source-preview")
            expect(preview).to_be_visible()
            original = preview.locator(".fh-ai-source-original")
            expect(original.locator("h2")).to_have_text("资料清单")
            expect(original.locator("th")).to_have_text(["来源", "用途"])
            expect(original.locator("td strong")).to_have_text("核对金额")
            expect(original.locator("td code")).to_have_text("收费规则.md")
            expect(original.locator("ul ul li")).to_have_text("核对收费规则")
            expect(original.locator("pre code")).to_contain_text("## 这是代码，不是标题\n| 保留代码竖线 |")
            expect(original.locator("script")).to_have_count(0)
            assert original.get_by_text("危险链接", exact=True).get_attribute("href") is None
            assert original.evaluate("() => window.sourcePreviewExecuted") is None
            assert original.evaluate("node => getComputedStyle(node).whiteSpace") == "normal"
            page.screenshot(path=str(tmp_path / f"{host}-source-markdown.png"))
            preview.get_by_role("button", name="继续看回答", exact=True).click()
            evidence.locator(".fh-ai-source").first.click()
            expect(preview.locator("table")).to_have_count(1)
            assert not errors and not external
        finally:
            browser.close()


def exercise_document_window(page, panel):
    window = page.locator(".document-ai-window")
    before = window.bounding_box()
    handle = page.get_by_role("separator", name="调整文档助手大小 w", exact=True).bounding_box()
    page.mouse.move(handle["x"] + 4, handle["y"] + handle["height"] / 2)
    page.mouse.down()
    page.mouse.move(handle["x"] - 96, handle["y"] + handle["height"] / 2, steps=8)
    page.mouse.up()
    assert window.bounding_box()["width"] >= before["width"] + 85
    panel.locator('[data-ai-tab="settings"]').click()
    panel.get_by_role("button", name="浮动小窗", exact=True).click()
    expect(window).to_have_attribute("data-window-mode", "floating")
    before = window.bounding_box()
    drag = page.get_by_role("button", name="移动文档助手", exact=True).bounding_box()
    page.mouse.move(drag["x"] + 40, drag["y"] + 20)
    page.mouse.down()
    page.mouse.move(drag["x"] - 80, drag["y"] + 65, steps=8)
    page.mouse.up()
    moved = window.bounding_box()
    assert abs(moved["x"] - before["x"] + 120) < 3
    assert abs(moved["y"] - before["y"] - 45) < 3
    handle = page.get_by_role("separator", name="调整文档助手大小 se", exact=True).bounding_box()
    page.mouse.move(handle["x"] + 5, handle["y"] + 5)
    page.mouse.down()
    page.mouse.move(handle["x"] - 65, handle["y"] - 45, steps=8)
    page.mouse.up()
    resized = window.bounding_box()
    assert abs(resized["width"] - moved["width"] + 70) < 3
    assert abs(resized["height"] - moved["height"] + 50) < 3
    panel.locator("#fh-ai-close").click()
    expect(window).to_be_hidden()
    page.get_by_role("button", name="打开知识助手", exact=True).click()
    expect(panel.locator("#fh-ai-panel")).to_be_visible()
    assert abs(window.bounding_box()["width"] - resized["width"]) < 3
    panel.get_by_role("button", name="恢复默认位置与大小", exact=True).click()
    assert window.bounding_box()["width"] >= 550
    panel.get_by_role("button", name="侧边展开", exact=True).click()
    expect(window).to_have_attribute("data-window-mode", "docked")
    panel.locator('[data-ai-tab="chat"]').click()


def test_document_stream_arrives_before_terminal_and_stop_keeps_partial(
    assistant, streaming_provider, tmp_path, monkeypatch
):
    provider = streaming_provider
    web, _service, pid, aid, _grant, _body = test_chat_streaming.configure(assistant, provider, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            panel.locator("#fh-ai-question").fill("新增校验的要求是什么？")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("流式内容", timeout=12000)
            assert not provider.release.is_set()
            expect(panel.locator(".fh-ai-cite")).to_have_count(0)
            panel.locator("#fh-ai-stop").click()
            expect(panel.locator(".fh-ai-message.error")).to_contain_text("未完成")
            expect(panel.locator("#fh-ai-question")).to_have_value("新增校验的要求是什么？")
            provider.release.set()
            page.screenshot(path=str(tmp_path / "document-stream-stopped.png"))
        finally:
            provider.release.set()
            browser.close()


def test_document_scope_and_local_originals_do_not_call_models(materials, monkeypatch):
    service, owner, pid, aid, links = prepared(materials)
    docs = links.catalog(owner, pid, aid)["documents"]
    guide = next(doc for doc in docs if doc["name"] == "Guide")
    monkeypatch.setattr(ai_gateway, "request_model", lambda *_: (_ for _ in ()).throw(AssertionError("network")))
    form = InteractiveQuestionInput(
        analysis_set_id=aid,
        package_id=None,
        document_revision_id=guide["revision_id"],
        document_scope="current",
        target={
            "route": "/documents",
            "frame_path": [],
            "selector": "body",
            "label": "Guide",
            "state": "",
            "open_steps": [],
        },
        question="Guide Private Restricted",
        retrieval_mode="local",
    )
    engine = InteractiveAssistant(service)
    result = engine.run(owner, pid, form)
    assert result["sections"] and all(row["document_revision_id"] == guide["revision_id"] for row in result["sections"])
    assert "Restricted corpus" not in result["explanation"] and not result["saved"]
    assert not result["model_id"]


def test_keyword_ai_mode_does_not_embed(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _raw), body, calls = assistant
    engine = InteractiveAssistant(service)
    monkeypatch.setattr(engine.rag, "available", lambda *_: (_ for _ in ()).throw(AssertionError("embedding path")))
    form = InteractiveQuestionInput.model_validate(
        {**body, "package_id": None, "question": "新增校验", "retrieval_mode": "keyword"}
    )
    result = engine.run(owner, pid, form)
    assert result["sections"] and not result["saved"] and len(calls) == 1


def test_studio_original_document_ui_uses_shared_model_citations_and_history(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _raw), _body, calls = assistant

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["explanation"] = "**文档回答**：依据当前文档办理。[1] 代码中的 `[1]` 保持原样。"
        answer["suggested_questions"] = ["请说明办理前提。", "如何核对办理结果？"]
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            expect(page.get_by_label("文档阅读方式", exact=True)).to_have_count(0)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(panel.locator("#fh-ai-panel")).to_be_visible()
            exercise_document_window(page, panel)
            panel.locator("#fh-ai-layout-toggle").click()
            expect(page.locator(".document-ai-window.expanded")).to_be_visible()
            assert page.locator(".document-ai-window").bounding_box()["width"] > 800
            panel.locator("#fh-ai-layout-toggle").click()
            expect(page.locator(".document-ai-window.expanded")).to_have_count(0)
            expect(panel.get_by_role("button", name="相关文档", exact=True)).to_have_count(0)
            expect(panel.get_by_role("button", name="AI 讲解", exact=True)).to_have_count(0)
            panel.locator("#fh-ai-question").fill("Add")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("文档回答", timeout=15000)
            expect(panel.locator(".fh-ai-answer-text .fh-ai-inline-cite")).to_have_text("[1]")
            expect(panel.locator(".fh-ai-answer-block > .fh-ai-cite")).to_have_count(0)
            expect(panel.locator(".fh-ai-answer-text code")).to_have_text("[1]")
            expect(panel.locator("details.fh-ai-sources")).not_to_have_attribute("open", "")
            panel.locator(".fh-ai-inline-cite").click()
            expect(panel.locator("#fh-ai-source-preview")).to_be_visible()
            panel.get_by_role("button", name="继续看回答", exact=True).click()
            expect(panel.get_by_role("button", name="将问题填入输入框：请说明办理前提。", exact=True)).to_be_visible()
            assert calls[-1]["question"] == "Add" and not calls[-1]["control"]["route"].startswith("/demo")
            panel.get_by_role("button", name="将问题填入输入框：请说明办理前提。", exact=True).click()
            # Recommended questions fill the composer; submitting retains history.
            expect(panel.locator("#fh-ai-question")).to_have_value("请说明办理前提。")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            assert len(calls[-1]["history"]) == 2
            panel.locator('[data-ai-tab="settings"]').click()
            expect(panel.locator("#fh-ai-reading-speed")).to_be_visible()
            assert (
                panel.locator("#fh-ai-reading-speed")
                .locator("xpath=ancestor::*[contains(@class,'fh-ai-card')][1]")
                .bounding_box()["height"]
                < 160
            )
            expect(panel.get_by_role("button", name="模型连接设置", exact=True)).to_be_visible()
            expect(panel.get_by_role("button", name="知识索引", exact=True)).to_have_count(0)
            expect(panel.get_by_role("heading", name="平台知识索引", exact=True)).to_have_count(0)
            expect(panel.get_by_role("heading", name="统一连接设置", exact=True)).to_have_count(0)
            panel.locator("#fh-ai-reading-speed").select_option("instant")
            assert page.evaluate("localStorage.getItem('vks.ai.display-speed')") == "instant"
            panel.locator('[data-ai-tab="chat"]').click()
            panel.locator("#fh-ai-tools-toggle").click()
            expect(panel.locator("#fh-ai-scope")).to_be_visible()
            expect(panel.locator("#fh-ai-mode")).to_be_visible()
            expect(panel.locator("#fh-ai-history-select")).to_be_visible()
            previous = panel.locator("#fh-ai-history-select").input_value()
            panel.locator("#fh-ai-new").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(0)
            panel.locator("#fh-ai-history-select").select_option(previous)
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            expect(panel.locator(".fh-ai-answer-text .fh-ai-inline-cite")).to_have_text(["[1]", "[1]"])
            expect(panel.locator(".fh-ai-answer-block > .fh-ai-cite")).to_have_count(0)
            panel.locator("#fh-ai-history-folder").click()
            expect(panel.locator("#fh-ai-history-settings")).to_be_visible()
            expect(panel.locator("#fh-ai-pick-history-dir")).to_be_visible()
            # Real browser FileSystemDirectoryHandle, in isolated browser-owned
            # storage: no personal folder picker or filesystem permissions.
            panel.locator("body").evaluate(
                "() => {window.showDirectoryPicker = async () => navigator.storage.getDirectory();}"
            )
            panel.locator("#fh-ai-pick-history-dir").click()
            expect(panel.locator("#fh-ai-disk-history-status")).to_contain_text("已自动保存", timeout=10000)
            saved = panel.locator("body").evaluate(
                "async () => {const dir=await navigator.storage.getDirectory();const f=await dir.getFileHandle('知识助手_聊天历史.json');return JSON.parse(await (await f.getFile()).text());}"
            )
            assert len(saved["sessions"]) >= 2
            assert any(len(session["messages"]) == 4 for session in saved["sessions"])
            panel.locator("#fh-ai-close").click()
            expect(page.locator(".document-ai-window")).to_be_hidden()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel.locator("#fh-ai-panel")).to_be_visible()
            panel.locator('[data-ai-tab="chat"]').click()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            assert not errors
        finally:
            browser.close()


def test_export_original_document_ui_local_scope_shared_key_output_and_forget(reader_file, tmp_path):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        calls = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def provider(route):
            calls.append((route.request.url, route.request.post_data_json if route.request.post_data else None))
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
            if route.request.method == "OPTIONS":
                route.fulfill(status=204, headers=headers)
            elif route.request.url.endswith("/models"):
                route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers=headers)
            else:
                route.fulfill(
                    json={
                        "choices": [{"message": {"content": "任务名称和负责人均为必填。[1]"}, "finish_reason": "stop"}]
                    },
                    headers=headers,
                )

        page.route("https://api.example.test/v1/**", provider)
        try:
            page.goto(reader_file.as_uri())
            # Configure once on the prototype side; the document side reuses it.
            test_readonly_reader.configure(page)
            page.get_by_role("button", name="知识文档", exact=True).click()
            doc = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(doc.locator("#fh-ai-panel")).to_be_visible()
            exercise_document_window(page, doc)
            doc.locator("#fh-ai-question").fill("创建任务的要求是什么？")
            doc.locator("#fh-ai-send").click()
            expect(doc.locator(".fh-ai-message.assistant")).to_contain_text("任务名称和负责人", timeout=15000)
            expect(doc.locator(".fh-ai-inline-cite")).to_have_text("[1]")
            expect(doc.locator(".fh-ai-answer-block > .fh-ai-cite")).to_have_count(0)
            expect(doc.locator("details.fh-ai-sources")).not_to_have_attribute("open", "")
            doc.locator(".fh-ai-inline-cite").click()
            expect(doc.locator("#fh-ai-source-preview")).to_be_visible()
            doc.get_by_role("button", name="继续看回答", exact=True).click()
            posted = [body for url, body in calls if url.endswith("/chat/completions")]
            assert len(posted) == 1 and posted[0]["max_completion_tokens"] == 8000
            assert "fixture-recipient-only" not in doc.locator("body").inner_text()
            doc.locator("#fh-ai-close").click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(doc.locator("#fh-ai-panel")).to_be_visible()
            doc.locator('[data-ai-tab="settings"]').click()
            doc.get_by_role("button", name="模型连接设置", exact=True).click()
            page.get_by_label("在这台电脑记住 Key（仅个人电脑启用）", exact=True).check()
            page.get_by_label("最大输出 tokens", exact=True).select_option("4000")
            page.get_by_role("button", name="保存到本机", exact=True).click()
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("button", name="连接设置", exact=True).click()
            expect(page.get_by_label("最大输出 tokens", exact=True)).to_have_value("4000")
            assert page.evaluate("Object.values(localStorage).some(v=>v.includes('fixture-recipient-only'))")
            page.get_by_role("button", name="清除并忘记 Key", exact=True).click()
            assert not page.evaluate("Object.values(localStorage).some(v=>v.includes('fixture-recipient-only'))")
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("button", name="连接设置", exact=True).click()
            expect(page.get_by_label("最大输出 tokens", exact=True)).to_have_value("4000")
            assert "fixture-recipient-only" not in reader_file.read_text()
            assert not errors
        finally:
            browser.close()
