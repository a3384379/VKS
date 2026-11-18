"""Explicit, context-isolated explanations in a credential-free file:// export."""

import json

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader_tests
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader_tests.reader_file

EXPLANATION = "## 用途\n\n**原文事实**：创建任务需填写名称和负责人 [1]。\n\n## 操作步骤\n\n1. 填写任务信息 [1]。\n\n## 注意事项\n\n负责人必填 [1]。\n\n## 推断与资料不足\n\n原文没有说明审批流程，无法确定。"


def events(content, protocol="chat_completions", complete=True):
    if protocol == "responses":
        chunks = [{"type": "response.output_text.delta", "delta": content}]
        if complete:
            chunks.append({"type": "response.completed", "response": {"status": "completed"}})
    else:
        chunks = [{"choices": [{"delta": {"content": content}}]}]
        if complete:
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)


def mock_answers(page, content=EXPLANATION):
    calls = []

    def respond(route):
        request = route.request
        # A shared vector query is distinct from an answer-model invocation.
        if request.url.endswith(("/responses", "/chat/completions")):
            calls.append(request)
        if request.url.endswith("/models"):
            route.fulfill(json={"data": [{"id": "fixture-chat"}]})
        elif request.url.endswith("/embeddings"):
            route.fulfill(json={"data": [{"embedding": [1, 0]}]})
        else:
            protocol = "responses" if request.url.endswith("/responses") else "chat_completions"
            route.fulfill(content_type="text/event-stream", body=events(content, protocol))

    page.route("https://api.example.test/v1/**", respond)
    return calls


def open_explanation(page, label="创建任务"):
    frame = page.frame_locator("iframe.prototype-frame")
    frame.get_by_role("button", name=label, exact=True).click(button="right")
    page.get_by_role("button", name="AI 讲解这个功能", exact=False).click()
    expect(page.get_by_role("tab", name="AI 讲解", exact=True)).to_have_attribute("aria-selected", "true")


def apply_connection(page, protocol="chat_completions"):
    page.get_by_label("模型服务", exact=True).select_option("custom")
    page.get_by_label("接口地址", exact=True).fill("https://api.example.test/v1")
    page.get_by_label("API Key", exact=True).fill("synthetic-explanation-key")
    page.get_by_label("接口协议", exact=True).select_option(protocol)
    page.get_by_label("自定义模型 ID", exact=True).fill("deployment/reader+2026.11")
    page.get_by_label("允许将我的问题和本知识库的相关章节发送到上述模型服务", exact=True).check()
    page.get_by_role("button", name="应用并返回讲解", exact=True).click()


def configure(page, protocol="chat_completions"):
    page.get_by_role("button", name="连接设置", exact=True).click()
    apply_connection(page, protocol)


def test_exported_explanation_is_offline_and_settings_use_header_icon(reader_file):
    def update(payload):
        original = payload["bindings"][0]
        original["origin"] = "manual"
        payload["bindings"].append({**original, "id": "ai", "origin": "model", "summary": "已有 AI 讲解 [1]。"})

    reader_tests.rewrite_library(reader_file, update)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 360, "height": 740})
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            expect(page.get_by_text("AI 讲解 · 已缓存", exact=True)).to_be_visible()
            expect(page.locator(".explanation-intro")).to_have_count(0)
            expect(page.locator(".assistant-body .primary")).to_have_count(0)
            expect(page.locator(".assistant-body")).not_to_contain_text("按用途、操作步骤、注意事项组织")
            expect(page.locator(".assistant-footer #vks-generate-explanation")).to_be_visible()
            expect(page.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
            expect(page.get_by_role("tablist", name="助手功能").get_by_role("tab")).to_have_count(3)
            assert not calls
            configure(page)
            page.wait_for_timeout(400)
            assert not calls
            expect(page.locator(".exported-explanation")).to_have_count(2)
            page.get_by_role("button", name="重新生成讲解", exact=True).click()
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            assert len(calls) == 1
            expect(page.locator(".exported-explanation")).to_have_count(0)
            expect(
                page.locator(".assistant-footer").get_by_role("button", name="重新生成讲解", exact=True)
            ).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_configure_automatically_explains_and_reuses_result_without_chat_history(reader_file, protocol):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("tab", name="回答", exact=True).click()
            page.get_by_label("向 AI 提问", exact=True).fill("保留未发送草稿")
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            configure(page, protocol)
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            expect(page.locator(".explanation-current .answer strong")).to_have_text("原文事实")
            assert len(calls) == 1
            posted = calls[0].post_data_json
            assert posted["model"] == "deployment/reader+2026.11"
            conversation = posted.get("input") or posted["messages"][1:]
            assert len(conversation) == 1 and '"selector":"#taskcreate"' in conversation[0]["content"]
            assert "保留未发送草稿" not in conversation[0]["content"]
            page.get_by_role("tab", name="回答", exact=True).click()
            expect(page.get_by_label("向 AI 提问", exact=True)).to_have_value("保留未发送草稿")
            expect(page.locator(".chat-message")).to_have_count(0)
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            page.wait_for_timeout(400)
            assert len(calls) == 1
            page.locator(".explanation-current .chat-citations summary").first.click()
            page.locator(".explanation-current .chat-citations button").first.click()
            preview = page.get_by_role("dialog", name="引用原文预览", exact=True)
            expect(preview).to_be_visible()
            preview.get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.get_by_role("complementary", name="文档 AI 助手", exact=True)).to_be_visible()
            assert "synthetic-explanation-key" not in reader_file.read_text()
        finally:
            browser.close()


def test_clearing_key_keeps_cache_and_unapplied_connection_does_not_generate(reader_file):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            configure(page)
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_role("button", name="清除并忘记 Key", exact=True).click()
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            expect(page.locator(".explanation-current .answer")).to_contain_text("创建任务")
            configure(page)
            page.wait_for_timeout(400)
            assert len(calls) == 1
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("接口地址", exact=True).fill("https://different.example.test/v1")
            frame = page.locator("iframe.prototype-frame").element_handle().content_frame()
            frame.evaluate("document.getElementById('taskcreate').textContent='删除任务'")
            open_explanation(page, "删除任务")
            expect(page.get_by_role("button", name="配置模型连接", exact=True)).to_be_visible()
            page.wait_for_timeout(400)
            assert len(calls) == 1
        finally:
            browser.close()


def test_context_changes_auto_refresh_and_discard_late_answer(reader_file):
    def update(data):
        data["prototypes"].append({**data["prototypes"][0], "id": "p2", "name": "第二个原型"})
        data["bindings"].append({**data["bindings"][0], "id": "b2", "package_id": "p2", "section_ids": ["s2"]})

    reader_tests.rewrite_library(reader_file, update)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.evaluate("""() => {
                window.releases=[]; window.signals=[];
                window.fetch=(url,init)=>String(url).endsWith('/embeddings')
                  ? Promise.resolve(new Response(JSON.stringify({data:[{embedding:[1,0]}]}),{headers:{'content-type':'application/json'}}))
                  : new Promise(resolve=>{
                    const n=window.releases.length; window.signals.push(init.signal);
                    window.releases.push(()=>resolve(new Response('data: '+JSON.stringify({choices:[{delta:{content:n?'新目标讲解 [1]。':'旧目标迟到 [1]。'}}]})+'\\n\\n'+'data: '+JSON.stringify({choices:[{delta:{},finish_reason:'stop'}]})+'\\n\\n',{headers:{'content-type':'text/event-stream'}})));
                });
            }""")
            configure(page)
            page.wait_for_function("window.releases.length===1")
            frame = page.locator("iframe.prototype-frame").element_handle().content_frame()
            frame.evaluate("document.getElementById('taskcreate').textContent='删除任务'")
            # Ordinary click, without using the context menu, must refresh.
            page.frame_locator("iframe.prototype-frame").get_by_role("button", name="删除任务", exact=True).click()
            page.wait_for_function("window.signals[0].aborted")
            page.evaluate("window.releases[0]()")
            page.wait_for_function("window.releases.length===2")
            page.evaluate("window.releases[1]()")
            expect(page.locator(".explanation-current .answer")).to_have_text("新目标讲解 [1]。")
            expect(page.locator(".explanation-current")).not_to_contain_text("旧目标迟到")
            page.get_by_role("tab", name="文档", exact=True).click()
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            page.wait_for_timeout(400)
            assert page.evaluate("window.releases.length") == 2
            frame.evaluate("document.title='另一业务页面'")
            page.wait_for_function("window.releases.length===3")
            expect(page.locator(".assistant-context")).to_contain_text("另一业务页面")
            page.evaluate("window.releases[2]()")
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            page.get_by_role("button", name="知识文档", exact=True).click()
            document_ai = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(document_ai.locator("#vks-host-index-status")).to_contain_text("任务操作手册")
            header = document_ai.locator("#vks-host-index-status").inner_text()
            frame.evaluate("document.title='隐藏页面';document.querySelector('#taskcreate').click()")
            page.wait_for_timeout(400)
            expect(document_ai.locator("#vks-host-index-status")).to_have_text(header)
            assert page.evaluate("window.releases.length") == 3
            page.locator(".doc-list").get_by_role("button", name="其他文档", exact=False).click()
            page.get_by_role("button", name="交互原型", exact=True).click()
            page.frame_locator("iframe.prototype-frame").get_by_role("button", name="删除任务", exact=True).click(button="right")
            page.get_by_role("button", name="AI 讲解这个功能", exact=False).click()
            page.wait_for_function("window.releases.length===4")
            page.evaluate("window.releases[3]()")
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
        finally:
            browser.close()


def test_rapid_plain_selections_debounce_to_latest_target_and_reuse_cache(reader_file):
    def update(data):
        data["prototypes"][0]["html"] = (
            '<title>任务管理</title><button id="taskcreate">创建任务</button>'
            '<button id="another">其他功能</button><input placeholder="任务名称">'
        )

    reader_tests.rewrite_library(reader_file, update)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            configure(page)
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            frame = page.locator("iframe.prototype-frame").element_handle().content_frame()
            frame.evaluate("document.querySelector('input').click();document.querySelector('#another').click()")
            expect(page.locator(".assistant-context")).to_contain_text("其他功能")
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            assert len(calls) == 2
            posted = calls[-1].post_data_json
            assert '"selector":"#another"' in (posted.get("input") or posted["messages"][1:])[0]["content"]
            page.frame_locator("iframe.prototype-frame").get_by_role("button", name="创建任务", exact=True).click()
            expect(page.locator(".assistant-context")).to_contain_text("创建任务")
            page.wait_for_timeout(400)
            assert len(calls) == 2
        finally:
            browser.close()


@pytest.mark.parametrize("content", ["没有引用的讲解", "不存在的引用 [99]。"])
def test_invalid_explanation_is_not_completed_or_automatically_retried(reader_file, content):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        calls = mock_answers(page, content)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            configure(page)
            expect(page.locator(".explanation-current .error")).to_contain_text("引用")
            page.wait_for_timeout(500)
            assert len(calls) == 1
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_have_count(0)
        finally:
            browser.close()


@pytest.mark.parametrize("mode", ["truncated", "oversize", "cancel"])
def test_automatic_explanation_preserves_stream_limits_and_stop(reader_file, mode):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.evaluate(
                """mode => {
              window.calls=0;
              window.fetch=(url,init)=>{
                if(String(url).endsWith('/embeddings')) return Promise.resolve(new Response(JSON.stringify({data:[{embedding:[1,0]}]}),{headers:{'content-type':'application/json'}}));
                window.calls++;
                let stream;
                const body=new ReadableStream({start(c){stream=c;c.enqueue(new TextEncoder().encode(mode==='oversize'?'x'.repeat(2*1024*1024+1):'data: '+JSON.stringify({choices:[{delta:{content:'部分讲解 [1]。'}}]})+'\\n\\n'));if(mode!=='cancel')c.close();}});
                init.signal.addEventListener('abort',()=>stream.error(new DOMException('Aborted','AbortError')));
                return Promise.resolve(new Response(body,{headers:{'content-type':'text/event-stream'}}));
              };
            }""",
                mode,
            )
            configure(page)
            if mode == "cancel":
                expect(page.locator(".explanation-current .answer")).to_contain_text("部分讲解")
                page.get_by_role("button", name="停止讲解", exact=True).click()
            expected = {"truncated": "连接提前结束", "oversize": "超过 2 MiB", "cancel": "本次请求已停止"}[mode]
            expect(page.locator(".explanation-current .error")).to_contain_text(expected)
            page.wait_for_timeout(400)
            assert page.evaluate("window.calls") == 1
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_have_count(0)
        finally:
            browser.close()
