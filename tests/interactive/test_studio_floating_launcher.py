"""The Studio launcher moves by pointer without opening, and retains its UI position."""

import json

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


@pytest.mark.parametrize("touch,capture_fails", [(False, False), (True, False), (False, True)])
def test_launcher_drag_persists_and_click_opens_separately(materials, tmp_path, monkeypatch, touch, capture_fails):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", "<title>Demo</title><button>Open</button>"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, has_touch=touch)
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            launcher = page.get_by_role("button", name="打开知识助手", exact=True)
            expect(launcher).to_be_visible(timeout=15000)
            if capture_fails:
                launcher.evaluate("button => { button.setPointerCapture = () => { throw new DOMException('Capture unavailable'); }; }")
            box = launcher.bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            if touch:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                expect(page.locator(".assistant-drag-shield")).to_have_count(1)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": 250, "y": 260}]})
                cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            else:
                page.mouse.move(x, y)
                page.mouse.down()
                # Cover the iframe before the first movement: capture can be
                # unavailable while a newly selected prototype is navigating.
                expect(page.locator(".assistant-drag-shield")).to_have_count(1)
                page.mouse.move(250, 260, steps=10)
                page.mouse.up()
            expect(page.locator(".assistant-drag-shield")).to_have_count(0)
            expect(page.get_by_role("complementary", name="知识助手")).to_have_count(0)
            page.wait_for_function(
                "() => { const b=document.querySelector('.assistant-launcher').getBoundingClientRect(); return b.x>200 && b.x<260 && b.y>200 && b.y<280; }"
            )
            moved = launcher.bounding_box()
            assert 200 < moved["x"] < 260 and 200 < moved["y"] < 280
            stored = json.loads(page.evaluate("localStorage.getItem('vks.studio.assistant-launcher-position.v1')"))
            assert 200 < stored["left"] < 260 and 200 < stored["top"] < 280
            page.reload()
            expect(launcher).to_be_visible(timeout=15000)
            restored = launcher.bounding_box()
            assert abs(restored["x"] - stored["left"]) < 3 and abs(restored["y"] - stored["top"]) < 3
            launcher.click()
            expect(page.get_by_role("complementary", name="知识助手")).to_be_visible()
            page.locator(".assistant-launcher").click()
            expect(page.get_by_role("complementary", name="知识助手")).to_have_count(0)
            expect(page.locator(".assistant-drag-shield")).to_have_count(0)
            page.set_viewport_size({"width": 220, "height": 230})
            expect(launcher).to_be_visible()
            page.wait_for_function(
                "() => { const b=document.querySelector('.assistant-launcher').getBoundingClientRect(); return b.right<=220 && b.bottom<=230; }"
            )
            assert launcher.bounding_box()["x"] + launcher.bounding_box()["width"] <= 220
            assert launcher.bounding_box()["y"] + launcher.bounding_box()["height"] <= 230
            launcher.focus()
            launcher.press("Enter")
            expect(page.get_by_role("complementary", name="知识助手")).to_be_visible()
            panel = page.get_by_role("complementary", name="知识助手")
            bounds = panel.bounding_box()
            assert bounds["x"] >= 8 and bounds["y"] >= 8, bounds
            assert bounds["x"] + bounds["width"] <= 212, bounds
            assert bounds["y"] + bounds["height"] <= 222, bounds
        finally:
            browser.close()


@pytest.mark.parametrize("touch", [False, True])
def test_window_resize_continues_without_releasing_pointer_and_remembers_size(materials, tmp_path, monkeypatch, touch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", '<title>Demo</title><button id="add">新增收费项</button>'
    )
    document = test_materials.stage(
        service, owner, manager, pid, aid, "documents",
        "# 业务规则\n\n| 项目 | 处理 |\n| --- | --- |\n" + "| **完整内容** | 保留长表格中的业务说明与字段内容。 |\n" * 160,
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000}, has_touch=touch)
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            source.get_by_role("button", name="新增收费项", exact=True).click()
            if not page.get_by_role("complementary", name="知识助手").is_visible():
                page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel).to_be_visible()
            panel.locator(".source-disclosure > summary").click()
            expect(panel.locator(".markdown table tbody tr")).to_have_count(160)
            box = panel.bounding_box()
            handle = page.get_by_role("button", name="调整助手大小 nw", exact=True).bounding_box()
            x, y = handle["x"] + 11, handle["y"] + 11
            if touch:
                cdp = page.context.new_cdp_session(page)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                move = lambda dx, dy: cdp.send(
                    "Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + dx, "y": y + dy}]}
                )
            else:
                page.mouse.move(x, y)
                page.mouse.down()
                move = lambda dx, dy: page.mouse.move(x + dx, y + dy, steps=8)
            for dx, dy in [(-60, -40), (-30, -20), (-90, -60)]:
                move(dx, dy)
                page.wait_for_function(
                    'width => Math.abs(document.querySelector("#knowledge-assistant").getBoundingClientRect().width-width)<3',
                    arg=box["width"] - dx,
                )
                assert page.locator(".assistant-drag-shield").count() == 1
                assert page.evaluate("localStorage.getItem('vks.studio.assistant-window.v1')") is None
            if touch:
                cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            else:
                page.mouse.up()
            expect(page.locator(".assistant-drag-shield")).to_have_count(0)
            saved = json.loads(page.evaluate("localStorage.getItem('vks.studio.assistant-window.v1')"))
            assert abs(saved["width"] - (box["width"] + 90)) < 3
            assert abs(saved["height"] - (box["height"] + 60)) < 3
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel).to_be_visible()
            page.wait_for_function(
                'width => Math.abs(document.querySelector("#knowledge-assistant").getBoundingClientRect().width-width)<3',
                arg=saved["width"],
            )
            page.get_by_role("button", name="调整助手大小", exact=True).focus()
            page.keyboard.press("ArrowLeft")
            page.wait_for_function(
                'width => Math.abs(document.querySelector("#knowledge-assistant").getBoundingClientRect().width-width)<3',
                arg=saved["width"] - 20,
            )
        finally:
            browser.close()
