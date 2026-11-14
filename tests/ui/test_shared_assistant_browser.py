"""Shared assistant gestures and compact model catalogs in exported artifacts."""

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader
from tests.ui.test_frontend_browser import chromium_path
from tests.ui.test_readonly_reader_settings import open_settings

reader_file = reader.reader_file


def test_continuous_reverse_resize_survives_context_updates_and_restores(reader_file):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.locator(".assistant")
            before = panel.bounding_box()
            corner = panel.locator(".reader-resize-nw").bounding_box()
            x, y = corner["x"] + 5, corner["y"] + 5
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x - 200, y - 80, steps=20)
            expect(page.locator(".reader-drag-shield")).to_have_count(1)
            # A real context/render update while holding the pointer must not
            # replace the captured handle or strand the gesture over an iframe.
            page.evaluate("document.querySelector('#vks-assistant-tab-chat').click()")
            page.mouse.move(x - 120, y - 30, steps=20)
            page.mouse.up()
            expect(page.locator(".reader-drag-shield")).to_have_count(0)
            after = panel.bounding_box()
            assert after["width"] == pytest.approx(before["width"] + 120)
            assert after["height"] == pytest.approx(before["height"] + 30)
            assert after["x"] + after["width"] == pytest.approx(before["x"] + before["width"])
            expect(page.get_by_role("tab", name="AI 问答", exact=True)).to_have_attribute("aria-selected", "true")
            page.get_by_role("button", name="收起知识助手", exact=True).click()
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            restored = panel.bounding_box()
            assert restored == pytest.approx(after)
            handle = page.get_by_role("button", name="调整助手大小", exact=True)
            handle.press("ArrowLeft")
            assert panel.bounding_box()["width"] == pytest.approx(after["width"] - 20)
            page.set_viewport_size({"width": 320, "height": 480})
            expect(panel).to_be_in_viewport(ratio=1)
            bounds = panel.bounding_box()
            assert bounds["width"] == pytest.approx(304)
            assert bounds["x"] + bounds["width"] <= 320
            assert bounds["y"] + bounds["height"] <= 480
            assert errors == []
        finally:
            browser.close()


def test_large_model_catalog_is_compact_searchable_paged_and_keeps_current(reader_file):
    ids = [f"gpt-{generation}.{minor}" for generation in range(1, 7) for minor in range(15)]
    ids += ["text-embedding-3-small", "gpt-audio", "custom-current-model"]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": value} for value in ids]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            open_settings(page, reader_file)
            page.get_by_label("自定义模型 ID", exact=True).fill("custom-current-model")
            page.get_by_role("button", name="获取模型", exact=True).click()
            picker = page.get_by_label("可用模型", exact=True)
            expect(picker).to_have_value("custom-current-model")
            assert picker.locator("option").count() <= 9
            assert "gpt-audio" not in picker.inner_text()
            page.get_by_role("button", name="查看全部模型", exact=False).click()
            assert picker.locator("option").count() <= 23
            page.get_by_role("button", name="下一页", exact=True).click()
            assert picker.locator("option").count() <= 23
            expect(picker).to_have_value("custom-current-model")
            page.get_by_label("搜索模型", exact=True).fill("gpt-6.14")
            expect(picker.locator('option[value="gpt-6.14"]')).to_have_count(1)
            picker.select_option("gpt-6.14")
            expect(picker).to_have_value("gpt-6.14")
            assert "synthetic-recipient-key" not in page.evaluate("Object.values(localStorage).join('')")
        finally:
            browser.close()
