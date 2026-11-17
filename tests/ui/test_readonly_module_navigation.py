"""Module identity is stable across first navigation, menu and tab reentry."""

from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader.reader_file


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
