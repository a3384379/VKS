"""Recipient-side floating controls and local image navigation in file:// exports."""

from base64 import b64encode
from io import BytesIO

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader.reader_file


def drag(page, locator, dx, dy):
    rect = locator.bounding_box()
    x, y = rect["x"] + 20, rect["y"] + 20
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=12)
    page.mouse.up()


def test_compact_launcher_panel_drag_and_position_restore_without_credentials(reader_file):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        try:
            page.goto(reader_file.as_uri())
            launch = page.get_by_role("button", name="打开知识助手", exact=True)
            expect(launch.locator("svg")).to_have_count(1)
            assert launch.bounding_box()["width"] == 44
            assert launch.bounding_box()["height"] == 44
            before = launch.bounding_box()
            assert before["width"] <= 56
            drag(page, launch, -230, -150)
            moved = launch.bounding_box()
            assert moved["x"] == pytest.approx(before["x"] - 230)
            expect(page.locator(".assistant")).to_have_count(0)
            page.wait_for_timeout(380)
            launch.click()
            panel = page.locator(".assistant")
            header = panel.locator(".assistant-head")
            original = panel.bounding_box()
            drag(page, header, -120, -80)
            position = panel.bounding_box()
            assert position["x"] == pytest.approx(original["x"] - 120)
            page.get_by_role("tab", name="回答", exact=True).click()
            assert panel.bounding_box()["x"] == pytest.approx(position["x"])
            page.get_by_role("button", name="收起知识助手", exact=True).click()
            page.reload()
            assert launch.bounding_box()["x"] == pytest.approx(moved["x"])
            launch.click()
            assert panel.bounding_box()["x"] == pytest.approx(position["x"])
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("API Key", exact=True).fill("do-not-persist-this-fixture-key")
            stored = page.evaluate("Object.values(localStorage).join('')")
            assert "do-not-persist-this-fixture-key" not in stored
            page.set_viewport_size({"width": 360, "height": 640})
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            bounds = panel.bounding_box()
            assert bounds["x"] >= 0 and bounds["y"] >= 0
            assert bounds["x"] + bounds["width"] <= 360
            assert bounds["y"] + bounds["height"] <= 640
            expect(page.get_by_role("button", name="连接设置", exact=True)).to_be_in_viewport()
            page.get_by_role("button", name="返回助手", exact=True).click()
            expect(page.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
            page.get_by_role("button", name="收起知识助手", exact=True).click()
            expect(launch).to_be_in_viewport(ratio=1)
            before_key = launch.bounding_box()["x"]
            launch.press("Alt+ArrowLeft")
            assert launch.bounding_box()["x"] == pytest.approx(max(8, before_key - 20))
            page.reload()
            launch.click()
            page.get_by_role("button", name="连接设置", exact=True).click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
        finally:
            browser.close()


def test_input_focus_and_plain_click_refresh_knowledge_instead_of_module_fallback(reader_file):
    def update(data):
        data["bindings"].append(
            {
                "id": "module",
                "package_id": "p1",
                "scope": "module",
                "target": {"route": "/", "frame_path": [], "selector": "body", "label": "任务管理", "state": ""},
                "summary": "模块通用说明不能冒充输入框说明",
                "section_ids": ["s1"],
            }
        )

    reader.rewrite_library(reader_file, update)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            source = page.frame_locator("iframe.prototype-frame")
            source.locator("body").click(button="right", position={"x": 400, "y": 6})
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.locator(".assistant-body")).to_contain_text("模块通用说明不能冒充输入框说明")
            source = page.frame_locator("iframe.prototype-frame")
            source.get_by_role("button", name="创建任务", exact=True).click()
            source.get_by_placeholder("任务名称", exact=True).fill("保留原生输入")
            source.get_by_placeholder("任务名称", exact=True).focus()
            expect(page.locator(".assistant-context")).to_contain_text("任务名称")
            expect(page.locator(".assistant-body")).not_to_contain_text("模块通用说明不能冒充输入框说明")
            expect(page.locator(".knowledge-match").first).to_be_visible()
            expect(source.get_by_placeholder("任务名称", exact=True)).to_have_value("保留原生输入")
            source.get_by_role("button", name="提交", exact=True).click()
            expect(page.locator(".assistant-context")).to_contain_text("提交")
            expect(source.get_by_text("演示已提交", exact=True)).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("kind", ["png", "mermaid"])
def test_markdown_image_dialog_wheel_anchor_drag_reset_and_close(reader_file, kind):
    png = BytesIO()
    Image.new("RGB", (800, 400), "#2a7b62").save(png, format="PNG")

    def update(data):
        doc = data["documents"][0]
        doc["markdown"] = (
            "# 图片与图表\n\n![本地图片](picture.png)\n\n```mermaid\nflowchart LR\n A[原文] --> B[完整图表]\n```\n"
        )
        doc["images"] = {"picture.png": "data:image/png;base64," + b64encode(png.getvalue()).decode()}

    reader.rewrite_library(reader_file, update)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="知识文档", exact=True).click()
            image = page.locator(".document img[alt='" + ("本地图片" if kind == "png" else "Mermaid 图表") + "']")
            expect(image).to_be_visible(timeout=20000)
            image.dblclick()
            dialog = page.locator(".md-image-dialog")
            expect(dialog).to_be_visible()
            canvas = dialog.get_by_role("region", name="图片平移缩放画布")
            large = dialog.locator("img")
            expect(large).to_be_visible()
            before = large.bounding_box()
            anchor_x, anchor_y = before["x"] + before["width"] * 0.6, before["y"] + before["height"] * 0.4
            page.mouse.move(anchor_x, anchor_y)
            page.mouse.wheel(0, -160)
            expect(dialog.get_by_label("图片缩放比例")).not_to_have_text("100%")
            after = large.bounding_box()
            assert after["width"] > before["width"]
            assert after["x"] + after["width"] * 0.6 == pytest.approx(anchor_x, abs=2)
            rect = canvas.bounding_box()
            page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
            page.mouse.down()
            page.mouse.move(rect["x"] + rect["width"] / 2 + 90, rect["y"] + rect["height"] / 2 + 50, steps=8)
            page.mouse.up()
            assert large.bounding_box()["x"] == pytest.approx(after["x"] + 90, abs=2)
            dialog.get_by_role("button", name="图片缩放比例", exact=True).click()
            expect(dialog.get_by_label("图片缩放比例")).to_have_text("100%")
            assert large.bounding_box()["x"] == pytest.approx(before["x"], abs=2)
            canvas.focus()
            canvas.press("+")
            expect(dialog.get_by_label("图片缩放比例")).to_have_text("125%")
            canvas.press("Home")
            expect(dialog.get_by_label("图片缩放比例")).to_have_text("100%")
            page.keyboard.press("Escape")
            expect(dialog).to_have_count(0)
        finally:
            browser.close()
