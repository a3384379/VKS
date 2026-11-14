"""Compact association list preserves filtering, editing, exclusion and prototype navigation."""

import re

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
