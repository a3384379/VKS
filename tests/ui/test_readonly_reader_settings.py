"""Recipient settings in a file:// export, using only synthetic model endpoints."""

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader_tests
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader_tests.reader_file


def open_settings(page, reader_file):
    page.goto(reader_file.as_uri())
    page.get_by_role("button", name="打开知识助手", exact=True).click()
    page.get_by_role("tab", name="连接设置", exact=True).click()
    page.get_by_label("模型服务", exact=True).select_option("custom")
    page.get_by_label("接口地址", exact=True).fill("https://api.example.test/v1")
    page.get_by_label("API Key", exact=True).fill("synthetic-recipient-key")
    expect(page.locator("#vks-assistant-connection-status")).to_have_text("使用个人密钥")


def test_touch_model_picker_preserves_exact_catalog_and_manual_ids(reader_file):
    catalog_id = "provider/answer:v2-2026.11+fixture"
    custom_id = "my-deployment/answer-2026.11-preview"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 360, "height": 740}, has_touch=True)
        calls = []

        def respond(route):
            calls.append(route.request)
            if route.request.url.endswith("/models"):
                route.fulfill(
                    json={"data": [{"id": catalog_id}, {"id": catalog_id}, {"id": None}, {"id": 6}]},
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            else:
                route.fulfill(
                    content_type="text/event-stream",
                    headers={"Access-Control-Allow-Origin": "*"},
                    body='data: {"choices":[{"delta":{"content":"任务名称必填 [1]。"}}]}\n\n'
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                )

        page.route("https://api.example.test/v1/**", respond)
        try:
            open_settings(page, reader_file)
            custom = page.get_by_label("自定义模型 ID", exact=True)
            custom.fill(catalog_id)
            page.get_by_role("button", name="获取模型", exact=True).tap()
            picker = page.get_by_label("可用模型", exact=True)
            expect(picker).to_have_value(catalog_id)
            expect(picker.locator("option")).to_have_text(["请选择可用模型", catalog_id, "手动输入模型 ID…"])
            expect(custom).to_be_hidden()
            expect(page.locator("datalist")).to_have_count(0)
            # The native select is the touch control; manual entry is an
            # explicit option, rather than a second competing model picker.
            picker.tap()
            page.keyboard.press("Escape")
            picker.select_option(label="手动输入模型 ID…")
            expect(custom).to_be_focused()
            custom.fill(custom_id)
            page.get_by_label("允许将我的问题和本知识库的相关章节发送到上述模型服务", exact=True).check()
            page.get_by_role("button", name="应用并开始提问", exact=True).tap()
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务需要哪些信息？")
            page.get_by_role("button", name="发送", exact=True).tap()
            expect(page.locator(".answer").last).to_contain_text("任务名称必填")
            posted = [request for request in calls if request.url.endswith("/chat/completions")]
            assert len(posted) == 1
            assert posted[0].post_data_json["model"] == custom_id
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            assert "synthetic-recipient-key" not in reader_file.read_text()
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("tab", name="连接设置", exact=True).click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
            expect(page.locator("#vks-assistant-connection-status")).to_have_text("未配置 AI")
        finally:
            browser.close()


@pytest.mark.parametrize("viewport", [(1280, 900), (360, 640), (320, 480), (750, 320)])
def test_assistant_tabs_keyboard_selection_and_scrolling_stay_inside_panel(reader_file, viewport):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            open_settings(page, reader_file)
            tabs = page.get_by_role("tablist", name="助手功能")
            selected = tabs.locator('[aria-selected="true"]')
            expect(selected).to_have_text("连接设置")
            page.get_by_role("tab", name="相关知识", exact=True).hover()
            backgrounds = tabs.get_by_role("tab").evaluate_all(
                "nodes => nodes.map(node => getComputedStyle(node).backgroundColor)"
            )
            assert backgrounds[0] != backgrounds[2]
            settings = page.get_by_role("tab", name="连接设置", exact=True)
            settings.focus()
            settings.press("Home")
            expect(selected).to_have_text("相关知识")
            page.get_by_role("tab", name="相关知识", exact=True).press("ArrowRight")
            expect(selected).to_have_text("AI 讲解")
            page.get_by_role("tab", name="AI 讲解", exact=True).press("End")
            expect(settings).to_be_focused()
            expect(page.get_by_role("tabpanel", name="连接设置")).to_be_visible()
            # Both switches in the same frame reproduce the old chat rAF
            # callback scrolling the freshly opened settings to its bottom.
            page.evaluate("""() => {
                document.querySelector('#vks-assistant-tab-chat').click();
                document.querySelector('#vks-assistant-tab-settings').click();
            }""")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert page.locator(".assistant-body").evaluate("node => node.scrollTop") == 0
            page.get_by_label("自定义模型 ID", exact=True).fill("fixture-model")
            page.get_by_role("button", name="应用并开始提问", exact=True).scroll_into_view_if_needed()
            bounds = page.locator(".assistant").evaluate("""node => {
                const rect = element => {
                    const {top, right, bottom, left} = element.getBoundingClientRect();
                    return {top, right, bottom, left};
                };
                return {outer:rect(node), head:rect(node.querySelector('.assistant-head')),
                    tabs:rect(node.querySelector('.assistant-tabs')), body:rect(node.querySelector('.assistant-body')),
                    footer:rect(node.querySelector('.assistant-footer')), scrollTop:node.scrollTop};
            }""")
            assert bounds["outer"]["top"] >= 0 and bounds["outer"]["bottom"] <= viewport[1]
            assert bounds["head"]["top"] == pytest.approx(bounds["outer"]["top"] + 1)
            assert bounds["body"]["top"] >= bounds["tabs"]["bottom"]
            assert bounds["body"]["bottom"] <= bounds["footer"]["top"] + 1
            assert bounds["scrollTop"] == 0
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="收起知识助手", exact=True).click()
            expect(page.get_by_role("button", name="打开知识助手", exact=True)).to_be_focused()
            assert errors == []
        finally:
            browser.close()


def test_protocol_change_cancels_late_catalog_and_pending_state_survives_tabs(reader_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            open_settings(page, reader_file)
            page.evaluate("""() => {
                window.catalogReleases = [];
                window.catalogSignals = [];
                window.fetch = (url, init) => new Promise(resolve => {
                    const index = window.catalogReleases.length;
                    window.catalogSignals.push(init.signal);
                    window.catalogReleases.push(() => resolve(new Response(JSON.stringify({data:[
                        {id:index ? 'current-protocol-model' : 'old-protocol-model'}
                    ]}), {headers:{'content-type':'application/json'}})));
                });
            }""")
            fetch = page.get_by_role("button", name="获取模型", exact=True)
            fetch.click()
            page.get_by_role("tab", name="AI 问答", exact=True).click()
            page.get_by_role("tab", name="连接设置", exact=True).click()
            expect(fetch).to_be_disabled()
            expect(page.locator(".settings-form [role=status]")).to_have_text("正在读取模型列表…")
            page.get_by_label("接口协议", exact=True).select_option("responses")
            assert page.evaluate("window.catalogSignals[0].aborted") is True
            expect(fetch).to_be_enabled()
            expect(page.get_by_label("自定义模型 ID", exact=True)).to_have_value("")
            fetch.click()
            page.evaluate("window.catalogReleases[0]()")
            expect(fetch).to_be_disabled()
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_count(2)
            page.evaluate("window.catalogReleases[1]()")
            expect(fetch).to_be_enabled()
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_text(
                ["请选择可用模型", "current-protocol-model", "手动输入模型 ID…"]
            )
        finally:
            browser.close()


def test_unavailable_catalog_keeps_manual_entry_usable(reader_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 360, "height": 640})
        page.route("https://api.example.test/v1/models", lambda route: route.fulfill(status=404))
        try:
            open_settings(page, reader_file)
            page.get_by_role("button", name="获取模型", exact=True).click()
            expect(page.locator(".settings-form .error")).to_contain_text("未找到接口或模型")
            manual = page.get_by_label("自定义模型 ID", exact=True)
            expect(manual).to_be_visible()
            manual.fill("deployment-without-model-catalog")
            page.get_by_label("允许将我的问题和本知识库的相关章节发送到上述模型服务", exact=True).check()
            page.get_by_role("button", name="应用并开始提问", exact=True).click()
            expect(page.get_by_label("向 AI 提问", exact=True)).to_be_visible()
        finally:
            browser.close()
