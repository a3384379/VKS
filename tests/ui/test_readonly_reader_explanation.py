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


def test_explanation_entry_offline_exported_sources_and_explicit_model_action(reader_file):
    def update(payload):
        first = payload["bindings"][0]
        first["origin"] = "manual"
        payload["bindings"].extend(
            [
                {**first, "id": "ai", "origin": "model", "summary": "已导出的 AI 说明 [1]。"},
                {
                    **first,
                    "id": "wrong-label",
                    "target": {**first["target"], "label": "其他按钮"},
                    "summary": "不属于当前目标的旧说明",
                },
            ]
        )

    reader_tests.rewrite_library(reader_file, update)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 360, "height": 740}, has_touch=True)
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            expect(page.get_by_text("已导出讲解 · 手动说明", exact=True)).to_be_visible()
            expect(page.get_by_text("已导出讲解 · AI 生成", exact=True)).to_be_visible()
            expect(page.locator(".exported-explanation")).to_have_count(2)
            expect(page.get_by_text("不属于当前目标的旧说明", exact=True)).to_have_count(0)
            generate = page.get_by_role("button", name="生成讲解", exact=True)
            expect(generate).to_be_in_viewport()
            assert calls == []
            page.get_by_role("tab", name="相关知识", exact=True).click()
            page.get_by_role("button", name="让 AI 讲解这个功能", exact=False).click()
            expect(generate).to_be_visible()
            assert calls == []
            generate.tap()
            expect(page.get_by_role("tab", name="连接设置", exact=True)).to_have_attribute("aria-selected", "true")
            expect(page.get_by_role("button", name="应用并返回讲解", exact=True)).to_be_visible()
            assert calls == []
        finally:
            browser.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_configure_then_explicit_generate_streams_citations_and_keeps_chat_separate(reader_file, protocol):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            apply_connection(page, protocol)
            expect(page.get_by_role("tab", name="AI 讲解", exact=True)).to_have_attribute("aria-selected", "true")
            assert calls == []
            page.get_by_role("tab", name="AI 问答", exact=True).click()
            page.get_by_label("向 AI 提问", exact=True).fill("保留这条未发送的问答草稿")
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            page.get_by_role("button", name="生成讲解", exact=True).click()
            expect(page.locator(".explanation-current .answer")).to_contain_text("原文事实")
            expect(page.locator(".explanation-current .answer strong")).to_have_text("原文事实")
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            assert len(calls) == 1
            assert calls[0].url.endswith("/responses" if protocol == "responses" else "/chat/completions")
            posted = calls[0].post_data_json
            assert posted["model"] == "deployment/reader+2026.11"
            instructions = posted.get("instructions") or posted["messages"][0]["content"]
            assert all(
                word in instructions for word in ["用途", "操作步骤", "注意事项", "原文事实", "推断", "准确章节引用"]
            )
            conversation = posted.get("input") or posted["messages"][1:]
            assert len(conversation) == 1
            assert '"selector":"#taskcreate"' in conversation[0]["content"]
            assert "保留这条未发送的问答草稿" not in conversation[0]["content"]
            page.get_by_role("tab", name="AI 问答", exact=True).click()
            expect(page.get_by_label("向 AI 提问", exact=True)).to_have_value("保留这条未发送的问答草稿")
            expect(page.locator(".chat-message")).to_have_count(0)
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            page.locator(".explanation-current .chat-citations button").first.click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.get_by_text("先选择要讲解的模块或控件", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="生成讲解", exact=True)).to_have_count(0)
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            assert "synthetic-explanation-key" not in reader_file.read_text()
        finally:
            browser.close()


def test_clear_key_and_reconfigure_returns_to_explanation_without_automatic_call(reader_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        calls = mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            apply_connection(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            page.get_by_role("tab", name="连接设置", exact=True).click()
            page.get_by_role("button", name="清除本次密钥", exact=True).click()
            expect(page.locator("#vks-assistant-connection-status")).to_have_text("未配置 AI")
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            expect(page.locator(".explanation-current .answer")).to_contain_text("创建任务")
            page.get_by_role("button", name="重新生成讲解", exact=True).click()
            expect(page.get_by_role("tab", name="连接设置", exact=True)).to_have_attribute("aria-selected", "true")
            apply_connection(page)
            assert len(calls) == 1
            page.get_by_role("button", name="重新生成讲解", exact=True).click()
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_be_visible()
            assert len(calls) == 2
        finally:
            browser.close()


def test_context_label_title_package_and_hidden_frame_do_not_reuse_or_continue_old_explanation(reader_file):
    def update(payload):
        payload["prototypes"].append({**payload["prototypes"][0], "id": "p2", "name": "第二个原型"})

    reader_tests.rewrite_library(reader_file, update)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        mock_answers(page)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            apply_connection(page)
            page.evaluate("""() => {
                const original = window.fetch;
                window.explanationReleases = [];
                window.explanationSignals = [];
                window.fetch = (url, init) => String(url).endsWith('/chat/completions')
                    ? new Promise(resolve => {
                        const index = window.explanationReleases.length;
                        window.explanationSignals.push(init.signal);
                        window.explanationReleases.push(() => resolve(new Response(
                            'data: ' + JSON.stringify({choices:[{delta:{content:index ? '新目标讲解 [1]。' : '旧目标迟到讲解 [1]。'}}]}) + '\\n\\n' +
                            'data: ' + JSON.stringify({choices:[{delta:{},finish_reason:'stop'}]}) + '\\n\\n',
                            {headers:{'content-type':'text/event-stream'}})));
                    }) : original(url, init);
            }""")
            page.get_by_role("button", name="生成讲解", exact=True).click()
            page.wait_for_function("window.explanationReleases.length === 1")
            frame = page.locator("iframe.prototype-frame").element_handle().content_frame()
            frame.evaluate("document.getElementById('taskcreate').textContent = '删除任务'")
            open_explanation(page, "删除任务")
            assert page.evaluate("window.explanationSignals[0].aborted") is True
            page.evaluate("window.explanationReleases[0]()")
            expect(page.get_by_role("button", name="生成讲解", exact=True)).to_be_enabled()
            expect(page.locator(".explanation-current .answer")).to_have_count(0)
            expect(page.locator(".exported-explanation")).to_have_count(0)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            page.wait_for_function("window.explanationReleases.length === 2")
            page.evaluate("window.explanationReleases[1]()")
            expect(page.locator(".explanation-current .answer")).to_have_text("新目标讲解 [1]。")
            frame.evaluate("document.title = '另一业务页面'")
            open_explanation(page, "删除任务")
            expect(page.locator(".explanation-current .answer")).to_have_count(0)
            page.get_by_role("button", name="知识文档", exact=True).click()
            before = page.locator(".assistant-context").inner_text()
            frame.evaluate("""() => {
                const source = Array.from(document.scripts).map(script => script.textContent).join('\\n');
                const nonce = source.match(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/i)[0];
                parent.postMessage({vks_reader:nonce,type:'context',payload:{scope:'module',page_title:'隐藏页面',
                    target:{route:'/',frame_path:[],selector:'body',label:'隐藏页面',state:''}}},'*');
            }""")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            expect(page.locator(".assistant-context")).to_have_text(before)
            expect(page.get_by_role("button", name="生成讲解", exact=True)).to_have_count(0)
            page.get_by_role("button", name="交互原型", exact=True).click()
            page.get_by_label("选择原型", exact=True).select_option("p2")
            open_explanation(page)
            expect(page.locator(".explanation-current .answer")).to_have_count(0)
            assert page.evaluate("window.explanationReleases.length") == 2
        finally:
            browser.close()


@pytest.mark.parametrize("content", ["没有引用的讲解", "不存在的引用 [99]。"])
def test_explanation_without_verifiable_citations_is_not_marked_complete(reader_file, content):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        mock_answers(page, content)
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            apply_connection(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            expect(page.locator(".explanation-current .error")).to_contain_text("引用")
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="重新生成讲解", exact=True)).to_be_enabled()
        finally:
            browser.close()


@pytest.mark.parametrize("mode", ["truncated", "oversize", "cancel"])
def test_explanation_reuses_stream_failure_size_limit_and_cancellation(reader_file, mode):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            open_explanation(page)
            page.get_by_role("button", name="生成讲解", exact=True).click()
            apply_connection(page)
            page.evaluate(
                """mode => {
                window.fetch = (url, init) => {
                    let stream;
                    const body = new ReadableStream({start(controller) {
                        stream = controller;
                        controller.enqueue(new TextEncoder().encode(mode === 'oversize' ? 'x'.repeat(2 * 1024 * 1024 + 1)
                            : 'data: ' + JSON.stringify({choices:[{delta:{content:'部分讲解 [1]。'}}]}) + '\\n\\n'));
                        if (mode !== 'cancel') controller.close();
                    }});
                    init.signal.addEventListener('abort', () => stream.error(new DOMException('Aborted','AbortError')));
                    return Promise.resolve(new Response(body, {headers:{'content-type':'text/event-stream'}}));
                };
            }""",
                mode,
            )
            page.get_by_role("button", name="生成讲解", exact=True).click()
            if mode == "cancel":
                expect(page.locator(".explanation-current .answer")).to_contain_text("部分讲解")
                page.get_by_role("button", name="停止讲解", exact=True).click()
            expected = {"truncated": "连接提前结束", "oversize": "超过 2 MiB", "cancel": "本次请求已停止"}[mode]
            expect(page.locator(".explanation-current .error")).to_contain_text(expected)
            expect(page.get_by_text("讲解已完成 · 仅本次会话", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="重新生成讲解", exact=True)).to_be_enabled()
        finally:
            browser.close()
