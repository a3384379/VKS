"""Opening a document assistant starts questions without a prototype dependency."""

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_document_launcher_opens_question_composer_without_prototype(materials, tmp_path, monkeypatch):
    web, service, actor, manager, projects = materials
    pid, aid = projects[0]
    doc = test_materials.stage(service, actor, manager, pid, aid, "documents", "# 发票规则\n\n开票前核对抬头。")
    test_materials.activate(service, actor, manager, pid, aid, [doc])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            login(page, url, pid, aid)
            expect(page.locator(".document-reader").get_by_role("heading", name="发票规则", exact=True)).to_be_visible()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.get_by_role("tab", name="AI 问答", exact=True)).to_have_attribute("aria-selected", "true")
            expect(panel.get_by_label("向知识助手提问", exact=True)).to_be_visible()
            expect(panel.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
            expect(panel.locator(".knowledge-footer")).to_contain_text("根据项目文档回答")
        finally:
            browser.close()
