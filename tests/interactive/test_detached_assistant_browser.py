"""Independent browser windows retain the one conversation and owning page."""

import json

import pytest

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway

from tests.interactive import test_assistant, test_chat_streaming
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui import test_readonly_reader
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
reader_file = test_readonly_reader.reader_file
streaming_provider = test_chat_streaming.streaming_provider


def document_panel(page):
    return page.frame_locator('iframe[title="知识手册 AI 助手"]')


def pop_document(page):
    with page.expect_popup() as opened:
        document_panel(page).get_by_role("button", name="弹出独立窗口", exact=True).click()
    popup = opened.value
    expect(popup.get_by_role("button", name="收回页面", exact=True)).to_be_visible()
    expect(document_panel(popup).locator('#fh-ai-panel[data-history-ready="true"]')).to_be_visible()
    expect(popup.locator('iframe[title="知识手册 AI 助手"]')).to_have_attribute("data-vks-view-state", "ready")
    return popup


@pytest.mark.parametrize("compact", [False, True])
def test_studio_document_window_preserves_stream_draft_close_and_disconnect(
    assistant, streaming_provider, tmp_path, monkeypatch, compact
):
    provider = streaming_provider
    web, _service, pid, aid, _grant, _body = test_chat_streaming.configure(assistant, provider, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        if not compact:
            page.add_init_script(
                "Object.defineProperty(window,'documentPictureInPicture',{value:undefined,configurable:true})"
            )
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = document_panel(page)
            panel.locator('#fh-ai-panel[data-history-ready="true"]').wait_for()
            panel.locator("#fh-ai-question").fill("草稿保留")
            popup = pop_document(page)
            expect(popup.locator("html")).to_have_attribute(
                "data-vks-window-mode", "compact" if compact else "standard"
            )
            if compact:
                assert page.evaluate("!!documentPictureInPicture.window")
            popup.on("pageerror", lambda error: errors.append(str(error)))
            expect(document_panel(popup).locator("#fh-ai-question")).to_have_value("草稿保留")
            expect(page.locator(".document-ai-window")).to_be_hidden()
            popup.get_by_role("button", name="收回页面", exact=True).click()
            expect(panel.locator("#fh-ai-question")).to_have_value("草稿保留")
            panel.locator("#fh-ai-question").fill("新增校验有哪些要求？")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("流式内容", timeout=12000)
            popup = pop_document(page)
            expect(document_panel(popup).locator(".fh-ai-message.assistant")).to_contain_text("流式内容")
            assert len(provider.calls) == 1
            provider.release.set()
            expect(document_panel(popup).locator(".fh-ai-message.assistant")).to_contain_text("第二段已完成")
            expect(document_panel(popup).locator("#fh-ai-question")).to_be_enabled()
            document_panel(popup).locator("#fh-ai-question").fill("关闭窗口仍保留")
            popup.close()
            expect(panel.locator("#fh-ai-question")).to_have_value("关闭窗口仍保留")
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(1)
            popup = pop_document(page)
            if compact:
                with popup.expect_event("close"):
                    page.reload()
            else:
                page.reload()
                expect(
                    popup.get_by_text("主页面已关闭或刷新。已显示内容保留，未完成回答不作为完整结果。", exact=False)
                ).to_be_visible()
                expect(document_panel(popup).locator(".fh-ai-message.assistant")).to_contain_text("第二段已完成")
                expect(document_panel(popup).locator("#fh-ai-question")).to_be_disabled()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel.locator("#fh-ai-question")).to_have_value("关闭窗口仍保留")
            panel.locator("#fh-ai-question").fill("刷新后新草稿")
            popup.close()
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel.locator("#fh-ai-question")).to_have_value("刷新后新草稿")
            assert len(provider.calls) == 1 and not errors
        finally:
            provider.release.set()
            browser.close()


def test_studio_prototype_window_uses_same_chat_and_citation(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _raw), _body, calls = assistant

    def model(_config, body):
        value = json.loads(body["input"][1]["content"])
        calls.append(value)
        answer = test_assistant.response(value["chapters"][0]["revision_id"])
        content = json.loads(answer["output"][0]["content"][0]["text"])
        content["explanation"] = (
            "独立窗口的 **回答**。[1]\n\n| 字段 | 要求 |\n| --- | --- |\n| 名称 | 必填 |\n\n```mermaid\nsequenceDiagram\n participant U as 用户\n participant S as 系统\n U->>S: 保存\n S-->>U: 完成\n```"
        )
        answer["output"][0]["content"][0]["text"] = json.dumps(content)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("if(top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            add = (
                page.frame_locator('iframe[title="原 HTML 原型"]')
                .frame_locator("#material")
                .get_by_role("button", name="Add", exact=True)
            )
            add.click(button="right")
            positions = page.get_by_role("menuitem").evaluate_all(
                "nodes=>nodes.map(n=>({icon:n.querySelector('svg').getBoundingClientRect().x,align:getComputedStyle(n).justifyContent}))"
            )
            assert all(p["align"] == "flex-start" for p in positions)
            assert max(p["icon"] for p in positions) - min(p["icon"] for p in positions) < 1
            page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
            question = page.get_by_label("向知识助手提问", exact=True)
            question.fill("保留问题")
            page.route(
                "**/assistant-test.css",
                lambda route: route.fulfill(
                    content_type="text/css", body=".vks-ai-composer-send{--copied-assistant-style:yes}"
                ),
            )
            page.add_style_tag(url=url + "/assistant-test.css")
            page.evaluate(
                "window.originalPip=window.documentPictureInPicture;Object.defineProperty(window,'documentPictureInPicture',{value:undefined,configurable:true});window.originalOpen=window.open;window.open=()=>null"
            )
            page.get_by_role("button", name="弹出独立窗口", exact=True).click()
            expect(page.get_by_text("浏览器未允许打开独立窗口，请允许本页弹窗后重试。", exact=True)).to_be_visible()
            expect(question).to_have_value("保留问题")
            page.evaluate("window.open=window.originalOpen;delete window.originalOpen")
            with page.expect_popup() as opened:
                page.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            expect(popup.get_by_label("向知识助手提问", exact=True)).to_have_value("保留问题")
            expect(popup.locator('style[data-vks-stylesheet$="assistant-test.css"]')).to_have_count(1)
            assert (
                popup.get_by_role("button", name="发送问题", exact=True)
                .evaluate("n=>getComputedStyle(n).getPropertyValue('--copied-assistant-style')")
                .strip()
                == "yes"
            )
            assert (
                popup.get_by_label("向知识助手提问", exact=True).evaluate("n=>getComputedStyle(n).borderTopWidth")
                == "0px"
            )
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            assert len(page.context.pages) == 2
            heights = []
            for height in (600, 1000):
                popup.set_viewport_size({"width": 900, "height": height})
                field_box = popup.get_by_label("向知识助手提问", exact=True).bounding_box()
                send_box = popup.get_by_role("button", name="发送问题", exact=True).bounding_box()
                assert send_box["y"] >= field_box["y"] + field_box["height"]
                assert field_box["width"] > 700
                heights.append(field_box["height"])
            assert max(heights) <= 32 and abs(heights[1] - heights[0]) < 2
            field = popup.get_by_label("向知识助手提问", exact=True)
            field.fill("多行问题\n" * 20)
            assert 100 < field.bounding_box()["height"] <= 161
            popup.set_viewport_size({"width": 900, "height": 420})
            assert field.bounding_box()["height"] < 110
            field.fill("")
            assert field.bounding_box()["height"] <= 32
            assert popup.locator(".vks-ai-composer").bounding_box()["height"] <= 105
            popup.set_viewport_size({"width": 900, "height": 1000})
            field.fill("保留问题")
            expect(popup.locator(".chat-composer small")).to_have_count(0)
            popup.evaluate(
                "window.diagramFrames=0;new MutationObserver(rs=>rs.forEach(r=>r.addedNodes.forEach(n=>{if(n.nodeType===1&&n.matches('iframe.diagram-render-frame'))window.diagramFrames++}))).observe(document.body,{childList:true})"
            )
            popup.get_by_label("向知识助手提问", exact=True).press("Enter")
            expect(popup.locator(".chat-answer")).to_contain_text("独立窗口的 回答")
            expect(popup.locator(".chat-answer table th")).to_have_text(["字段", "要求"])
            expect(popup.get_by_role("img", name="Mermaid 图表", exact=True)).to_be_visible(timeout=25000)
            assert popup.evaluate("window.diagramFrames") >= 1
            expect(page.locator("iframe.diagram-render-frame")).to_have_count(0)
            popup.get_by_role("img", name="Mermaid 图表", exact=True).click()
            media = popup.get_by_role("dialog", name="Mermaid 图表", exact=True)
            expect(media).to_be_visible()
            expect(page.locator(".md-image-dialog")).to_have_count(0)
            for label in ("适应窗口", "按宽度", "缩小图片", "图片缩放比例", "放大图片", "关闭大图"):
                expect(media.get_by_role("button", name=label, exact=True)).to_be_visible()
            media.get_by_role("button", name="图片缩放比例", exact=True).click()
            expect(media.get_by_role("button", name="图片缩放比例", exact=True)).to_have_text("100%")
            assert media.locator("img").evaluate("n=>Math.abs(n.getBoundingClientRect().width-n.naturalWidth)<1")
            popup.keyboard.press("Escape")
            expect(media).to_have_count(0)
            assert not popup.is_closed()
            expect(popup.locator(".chat-answer")).to_contain_text("独立窗口的 回答")
            expect(popup.locator(".chat-source-details")).not_to_have_attribute("open", "")
            popup.get_by_role("button", name="原文引用 1", exact=True).click()
            preview = popup.get_by_role("dialog", name="引用原文预览", exact=True)
            expect(preview.locator(".markdown h1")).to_have_text("收费项导览手册（合成）")
            preview.get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.locator(".document-reader:visible").first).to_be_visible()
            expect(popup.locator(".chat-answer")).to_contain_text("独立窗口的 回答")
            popup.get_by_label("向知识助手提问", exact=True).fill("继续草稿")
            popup.get_by_role("button", name="收回页面", exact=True).click()
            expect(page.get_by_label("向知识助手提问", exact=True)).to_have_value("继续草稿")
            page.reload()
            page.get_by_role("button", name="原型", exact=True).click()
            add.click(button="right")
            page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
            expect(page.locator(".chat-answer")).to_contain_text("独立窗口的 回答")
            expect(page.get_by_label("向知识助手提问", exact=True)).to_have_value("继续草稿")
            page.get_by_role("button", name="原文引用 1", exact=True).click()
            expect(page.get_by_role("dialog", name="引用原文预览", exact=True)).to_be_visible()
            page.get_by_role("button", name="关闭原文预览", exact=True).click()
            assert len(calls) == 1
        finally:
            browser.close()


def test_export_windows_share_configuration_and_return(reader_file):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        calls, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def provider(route):
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
            if route.request.method == "OPTIONS":
                route.fulfill(status=204, headers=headers)
            elif route.request.url.endswith("/models"):
                route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers=headers)
            elif route.request.url.endswith("/embeddings"):
                route.abort()
            else:
                calls.append(route.request.post_data_json)
                route.fulfill(
                    json={"choices": [{"message": {"content": "任务名称必填。[1]"}, "finish_reason": "stop"}]},
                    headers=headers,
                )

        page.route("https://api.example.test/v1/**", provider)
        try:
            page.goto(reader_file.as_uri())
            test_readonly_reader.configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("原型草稿")
            with page.expect_popup() as opened:
                page.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            popup.on("pageerror", lambda error: errors.append(str(error)))
            expect(popup.get_by_label("向 AI 提问", exact=True)).to_have_value("原型草稿")
            composer_style = "n=>({radius:getComputedStyle(n).borderRadius,font:getComputedStyle(n.querySelector('textarea')).fontSize,border:getComputedStyle(n.querySelector('textarea')).borderTopWidth,actions:getComputedStyle(n.querySelector('.vks-ai-composer-actions')).display})"
            prototype_style = popup.locator(".vks-ai-composer").evaluate(composer_style)
            popup.get_by_label("向 AI 提问", exact=True).press("Enter")
            expect(popup.locator(".assistant-message .answer")).to_contain_text("任务名称必填")
            popup.locator(".citation-inline").first.click()
            expect(popup.get_by_role("dialog", name="引用原文预览", exact=True)).to_be_visible()
            expect(popup.locator(".chat-source-preview table")).to_be_visible()
            popup.get_by_role("button", name="关闭原文预览", exact=True).click()
            page.get_by_role("button", name="知识文档", exact=True).click()
            panel = document_panel(popup)
            expect(panel.locator('#fh-ai-panel[data-history-ready="true"]')).to_be_visible()
            assert panel.locator(".vks-ai-composer").evaluate(composer_style) == prototype_style
            panel.locator("#fh-ai-question").fill("文档独立提问")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("任务名称必填")
            panel.locator(".fh-ai-inline-cite").click()
            cited_document = panel.locator("#fh-ai-source-preview-title").inner_text()
            panel.get_by_role("button", name="打开当前手册原文 ↗", exact=True).click()
            expect(page.locator(".document h1")).to_have_text(cited_document)
            panel.locator("#fh-ai-question").fill("文档草稿")
            popup.get_by_role("button", name="收回页面", exact=True).click()
            expect(document_panel(page).locator("#fh-ai-question")).to_have_value("文档草稿")
            expect(document_panel(page).locator(".fh-ai-message.assistant")).to_have_count(1)
            page.reload()
            page.get_by_role("button", name="交互原型", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("tab", name="回答", exact=True).click()
            expect(page.locator(".assistant-message .answer")).to_contain_text("任务名称必填")
            assert len(calls) == 2 and not errors
        finally:
            browser.close()


@pytest.mark.parametrize("policy", ["denied", "delayed"])
def test_export_compact_window_denial_fallback_and_pending_open_coalescing(reader_file, policy):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page()
        if policy == "denied":
            page.add_init_script(
                "Object.defineProperty(window,'documentPictureInPicture',{configurable:true,value:{requestWindow:async()=>{throw new DOMException('denied','NotAllowedError')}}})"
            )
        else:
            page.add_init_script("""if(top===window){
              const pip=window.documentPictureInPicture,request=pip.requestWindow.bind(pip);
              window.compactCalls=0;
              pip.requestWindow=async options=>{window.compactCalls++;const w=await request(options);return new Promise(resolve=>{window.releaseCompact=()=>resolve(w);});};
            }""")
        try:
            page.goto(reader_file.as_uri())
            source = page.frame_locator("iframe.prototype-frame")
            source.get_by_role("button", name="创建任务", exact=True).click(button="right")
            page.get_by_role("button", name="向 AI 提问", exact=False).click()
            page.get_by_label("向 AI 提问", exact=True).fill("保留草稿")
            with page.expect_popup() as opened:
                page.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            if policy == "delayed":
                page.wait_for_function("typeof window.releaseCompact==='function'")
                page.get_by_role("button", name="弹出独立窗口", exact=True).click()
                assert page.evaluate("window.compactCalls") == 1
                # The owner leaving before requestWindow resolves must not mount
                # a stale assistant into that pending window.
                with popup.expect_event("close"):
                    page.evaluate("window.dispatchEvent(new Event('pagehide'));window.releaseCompact()")
                assert popup.is_closed()
            else:
                expect(popup.locator("html")).to_have_attribute("data-vks-window-mode", "standard")
                expect(popup.get_by_label("向 AI 提问", exact=True)).to_have_value("保留草稿")
                popup.get_by_role("button", name="收回页面", exact=True).click()
                expect(page.get_by_label("向 AI 提问", exact=True)).to_have_value("保留草稿")
        finally:
            browser.close()
