"""Compact association list preserves filtering, editing, exclusion and prototype navigation."""

import re
from copy import deepcopy

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_association_list_detail_search_remove_restore_and_reveal(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _docs = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            row = page.get_by_role("button", name="查看关联详情：新增收费项说明", exact=True)
            expect(row).to_be_visible()
            detail = page.get_by_role("article", name="关联详情")
            expect(detail).to_contain_text("必须保留的人工说明")
            expect(page.locator(".binding-list-panel .markdown")).to_have_count(0)
            page.get_by_label("搜索关联", exact=True).fill("missing-test-value")
            expect(row).to_have_count(0)
            expect(page.get_by_text("没有符合条件的关联，可调整筛选或搜索内容。", exact=True)).to_be_visible()
            page.get_by_label("搜索关联", exact=True).fill("人工说明")
            expect(row).to_be_visible()
            page.get_by_label("搜索关联", exact=True).fill("")
            detail.get_by_role("button", name="编辑", exact=True).click()
            assistant = page.get_by_role("complementary", name="知识助手")
            expect(assistant.get_by_label("知识标题", exact=True)).to_have_value("新增收费项说明")
            assistant.get_by_role("button", name="取消", exact=True).click()
            detail.get_by_role("button", name="移除关联", exact=True).click()
            expect(row).to_have_count(0)
            page.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^已移除")).click()
            expect(row).to_be_visible()
            detail.get_by_role("button", name="恢复关联", exact=True).click()
            page.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^全部当前")).click()
            expect(row).to_be_visible()
            detail.get_by_role("button", name="打开原型", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.get_by_role("button", name="新增收费项", exact=True)).to_be_visible(timeout=15000)
        finally:
            browser.close()


def test_association_pagination_filters_and_background_refresh_preserve_current_page(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    phase = {"finished": False}
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            items = []
            for index in range(1, 27 if phase["finished"] else 26):
                row = deepcopy(original)
                row.update(id=f"fixture-{index}", title=f"示例关联{index:02}", summary=f"完整说明{index:02}")
                if index % 10 == 0:
                    row["validity_status"] = "stale"
                items.append(row)
            payload["data"] = items
            route.fulfill(response=response, json=payload)

        def batch(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["jobs"] = [
                {
                    "id": "fixture-job",
                    "state": "succeeded" if phase["finished"] else "running",
                    "stage": "ASSOCIATE",
                    "progress": 100 if phase["finished"] else 0,
                    "current_scope": True,
                    "concurrency": 3,
                    "mode": "missing",
                    "result": {
                        "total": 25,
                        "processed": 25 if phase["finished"] else 0,
                        "saved": 1 if phase["finished"] else 0,
                    },
                }
            ]
            route.fulfill(response=response, json=payload)

        page.route("**/api/v3/projects/*/interactive/bindings?*", bindings)
        page.route("**/associations/batch?*", batch)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            panel = page.get_by_role("region", name="关联管理", exact=True)
            nav = panel.get_by_role("navigation", name="关联列表分页")
            rows = panel.locator(".binding-table tbody tr")
            expect(rows).to_have_count(10)
            expect(nav).to_contain_text("第 1 / 3 页")
            nav.get_by_role("button", name="下一页", exact=True).click()
            expect(panel.get_by_role("article", name="关联详情")).to_contain_text("完整说明11")
            phase["finished"] = True
            expect(panel.locator(".binding-list-caption")).to_contain_text("26 条关联", timeout=15000)
            expect(nav).to_contain_text("第 2 / 3 页")
            expect(rows).to_have_count(10)
            nav.get_by_role("button", name="末页", exact=True).click()
            expect(rows).to_have_count(6)
            expect(nav.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
            panel.get_by_label("搜索关联", exact=True).fill("示例关联02")
            expect(rows).to_have_count(1)
            expect(nav).to_contain_text("第 1 / 1 页")
            panel.get_by_label("搜索关联", exact=True).fill("")
            nav.get_by_role("button", name="下一页", exact=True).click()
            panel.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^待处理")).click()
            expect(rows).to_have_count(2)
            expect(nav).to_contain_text("第 1 / 1 页")
            panel.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^全部当前")).click()
            panel.get_by_label("每页关联条数", exact=True).select_option("20")
            expect(rows).to_have_count(20)
            expect(nav).to_contain_text("第 1 / 2 页")
            assert panel.locator(".binding-table-wrap").evaluate("el=>el.scrollHeight<=el.clientHeight+1")
            page.set_viewport_size({"width": 390, "height": 720})
            assert panel.locator(".binding-table-wrap").evaluate("el=>el.scrollHeight<=el.clientHeight+1")
            assert not errors
        finally:
            browser.close()
