"""Frozen related knowledge has styled, preview-first sources in both layouts."""

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader.reader_file


@pytest.mark.parametrize("handbook", [False, True])
def test_saved_knowledge_preview_cards_and_uninterrupted_document_reading(reader_file, handbook):
    def update(data):
        if handbook:
            data["reader_mode"] = "handbook"
        binding = data["bindings"][0]
        binding.update(
            origin="model",
            title="创建任务的完整说明",
            section_ids=["s1", "s2"],
            summary="## 创建任务的完整说明\n\n第一段的**重要内容**。[1]\n\n|字段|条件|\n|---|---|\n|负责人|必须填写 [2]|\n\n第二段保留连续阅读。[2][3] `[4]`",
        )

    reader.rewrite_library(reader_file, update)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        requests, errors = [], []
        page.on("request", lambda r: requests.append(r.url) if r.url.startswith(("http:", "https:")) else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(reader_file.as_uri())
            source = page.frame_locator("iframe.prototype-frame")
            source.get_by_role("button", name="创建任务", exact=True).click(button="right")
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            related = page.locator(".knowledge-card")
            expect(related.locator("h2.explanation-title")).to_have_count(0)
            expect(related.locator(".rich-markdown h2")).to_have_text("创建任务的完整说明")
            expect(related.locator(".knowledge-summary")).to_contain_text("第二段保留连续阅读")
            expect(related.locator(".knowledge-source-preview")).to_have_count(0)
            expect(related.locator("details.chat-citations")).not_to_have_attribute("open", "")
            expect(related.locator(".chat-source-entry")).to_have_count(2)
            expect(related.locator(".citation-missing")).to_have_text("[3]（原文缺失）")
            expect(related.locator(".knowledge-summary code")).to_have_text("[4]")
            assert related.locator("strong").evaluate("e=>getComputedStyle(e).display") == "inline"
            assert (
                related.locator(".citation-inline").first.evaluate("e=>getComputedStyle(e).backgroundColor")
                != "rgba(0, 0, 0, 0)"
            )
            original_url = page.url
            related.locator(".citation-inline").first.click()
            preview = page.get_by_role("dialog", name="引用原文预览", exact=True)
            heading = preview.get_by_role("heading", name="创建任务", exact=True)
            expect(heading).to_have_count(1)
            expect(heading).to_be_visible()
            assert page.url == original_url
            expect(page.get_by_role("button", name="交互原型", exact=True)).to_have_attribute("aria-pressed", "true")
            preview.get_by_role("button", name="返回", exact=True).click()
            expect(preview).to_have_count(0)
            expect(related.locator("details.chat-citations")).not_to_have_attribute("open", "")
            related.locator(".chat-citations summary").click()
            card = related.locator(".chat-source-entry button").first
            style = card.evaluate(
                "e=>({color:getComputedStyle(e).color,bg:getComputedStyle(e).backgroundColor,pad:getComputedStyle(e).padding})"
            )
            assert style["bg"] != "rgba(0, 0, 0, 0)"
            card.click()
            expect(preview).to_be_visible()
            page.keyboard.press("Escape")
            expect(preview).to_have_count(0)
            page.get_by_role("tab", name="AI 讲解", exact=True).click()
            expect(page.locator(".assistant-body")).to_contain_text("暂无讲解")
            expect(page.locator(".assistant-body")).not_to_contain_text("第二段保留连续阅读")
            page.get_by_role("tab", name="文档", exact=True).click()
            related.locator(".chat-citations summary").click()
            card.click()
            expect(preview).to_be_visible()
            assert page.url == original_url
            preview.get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.locator(".document .section-links, .document .target-link")).to_have_count(0)
            expect(page.locator(".document")).to_contain_text("任务名称和负责人均为必填")
            assert not requests and not errors
        finally:
            browser.close()


def test_prototype_right_click_menu_dismisses_on_outer_and_inner_blank_clicks(reader_file):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            source = page.frame_locator("iframe.prototype-frame")
            target = source.get_by_role("button", name="创建任务", exact=True)
            for dismiss in (
                lambda: page.locator(".brand").click(),
                lambda: source.locator("body").click(position={"x": 600, "y": 2}),
                lambda: target.press("Escape"),
            ):
                target.click(button="right")
                expect(page.get_by_role("menu")).to_be_visible()
                dismiss()
                expect(page.get_by_role("menu")).to_have_count(0)
            target.click(button="right")
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.locator(".knowledge-card")).to_contain_text("新增任务需要填写名称和负责人")
            target.click()
            expect(source.get_by_placeholder("任务名称", exact=True)).to_be_visible()
        finally:
            browser.close()


def test_portal_reentering_module_keeps_same_saved_knowledge(reader_file):
    from tests.ui.prototype_navigation import portal_source

    def update(data):
        data["prototypes"][0]["html"] = portal_source()
        data["bindings"] = [
            {
                "id": "items-module",
                "package_id": "p1",
                "scope": "module",
                "target": {
                    "route": "/items.html",
                    "frame_path": ["actualPage"],
                    "selector": "body",
                    "label": "收费项目管理",
                    "state": "",
                },
                "title": "已保存的收费项目管理",
                "summary": "完整模块关联：费用类别、计费规则及适用边界。",
                "section_ids": ["s1"],
            },
            {
                "id": "items-query",
                "package_id": "p1",
                "scope": "control",
                "target": {
                    "route": "/items.html",
                    "frame_path": ["actualPage"],
                    "selector": "#query",
                    "label": "查询收费项",
                    "state": "",
                },
                "summary": "独立查询控件的关联。",
                "section_ids": ["s2"],
            },
        ]

    reader.rewrite_library(reader_file, update)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: requests.append(r.url) if r.url.startswith(("http:", "https:")) else None)
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            portal = page.frame_locator("iframe.prototype-frame")
            business = portal.frame_locator("#actualPage")
            expect(business.locator("h1")).to_have_text("收费标准管理")
            menu = portal.get_by_role("menuitem", name="收费项管理", exact=True)
            menu.click()
            expect(page.locator(".knowledge-card")).to_contain_text("完整模块关联")
            baseline = page.locator(".knowledge-card").inner_text()
            business.get_by_label("未保存内容", exact=True).fill("保留当前表单")
            loads = portal.locator("body").evaluate("()=>window.pageLoads")
            for nav in (menu, portal.get_by_role("tab", name="收费项管理", exact=True), menu):
                nav.click()
                expect(page.locator(".assistant-context")).to_have_text("当前模块 · 收费项目管理")
                expect(page.locator(".knowledge-card")).to_have_text(baseline, use_inner_text=True)
                expect(business.get_by_label("未保存内容", exact=True)).to_have_value("保留当前表单")
                assert portal.locator("body").evaluate("()=>window.pageLoads") == loads
            business.locator("#query").click()
            expect(page.locator(".knowledge-card")).to_contain_text("独立查询控件的关联")
            assert business.locator("body").evaluate("()=>window.queryClicks") == 1
            business.locator("#blank").click()
            expect(page.locator(".knowledge-card")).to_have_text(baseline, use_inner_text=True)
            business.locator("#query").click()
            portal.locator("#portal-blank").click()
            expect(page.locator(".knowledge-card")).to_have_text(baseline, use_inner_text=True)
            portal.get_by_role("menuitem", name="收费标准管理", exact=True).click()
            expect(page.locator(".assistant-context")).to_have_text("当前模块 · 收费标准管理")
            menu.click()
            expect(page.locator(".knowledge-card")).to_have_text(baseline, use_inner_text=True)
            # Explicit control lookup remains available on the portal itself.
            menu.click(button="right")
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.locator(".assistant-context")).to_have_text("当前控件 · 收费项管理")
            assert not errors and not requests
        finally:
            browser.close()
