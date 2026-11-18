"""Management pages hide inline assistants without dropping either page's draft."""

import re

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


@pytest.mark.parametrize("docked", [False, True], ids=["floating", "docked"])
def test_management_pages_hide_assistants_and_preserve_document_and_prototype_drafts(
    materials, tmp_path, monkeypatch, docked
):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)

    def no_model(*_args):
        pytest.fail("Changing page or typing a draft must not call a model")

    monkeypatch.setattr(ai_gateway, "request_model", no_model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        errors, writes = [], []
        page.add_init_script(
            f"if(window===window.top){{localStorage.setItem('vks.studio.auto-associate','off');localStorage.setItem('vks.studio.{pid}.auto-associate','off')}}"
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                writes.append(request.url)
                if request.method in ("POST", "PUT", "PATCH", "DELETE")
                and (
                    request.url.endswith("/conversation")
                    or "/interactive/bindings" in request.url
                    or request.url.endswith("/documents/build")
                    or request.url.endswith("/test")
                )
                else None
            ),
        )

        def management_pages_have_no_assistant_or_empty_dock():
            for button, heading in (("资料管理", "原型与文档"), ("关联管理", "关联管理"), ("AI 设置", "AI 设置")):
                page.get_by_role("button", name=button, exact=True).click()
                expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                expect(page.locator(".knowledge-window:visible,.document-ai-window:visible")).to_have_count(0)
                expect(page.locator(".assistant-launcher:visible")).to_have_count(0)
                expect(page.locator(".workspace")).not_to_have_class(re.compile(r"\bwith-dock\b"))
                expect(page.locator(".studio")).not_to_have_class(re.compile(r"\bdocument-ai-docked\b"))

        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            document = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(document.locator('#fh-ai-panel[data-history-ready="true"]')).to_be_visible()
            document.get_by_role("tab", name="助手设置", exact=True).click()
            document.get_by_role("button", name="侧边展开" if docked else "浮动小窗", exact=True).click()
            expect(page.locator(".document-ai-window")).to_have_attribute(
                "data-window-mode", "docked" if docked else "floating"
            )
            document.locator('[data-ai-tab="chat"]').click()
            document.locator("#fh-ai-question").fill("文档页未发送草稿")
            if docked:
                expect(page.locator(".studio")).to_have_class(re.compile(r"\bdocument-ai-docked\b"))
            management_pages_have_no_assistant_or_empty_dock()
            page.get_by_role("button", name="文档", exact=True).click()
            expect(document.locator("#fh-ai-question")).to_be_visible()
            expect(document.locator("#fh-ai-question")).to_have_value("文档页未发送草稿")

            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("article", name="关联详情").get_by_role("button", name="打开原型", exact=True).click()
            original = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(original.get_by_role("button", name="新增收费项", exact=True)).to_be_visible(timeout=15000)
            assistant = page.get_by_role("complementary", name="知识助手", exact=True)
            expect(assistant).to_be_visible()
            assistant.get_by_role("button", name="助手选项", exact=True).click()
            assistant.get_by_role("button", name="侧边展开" if docked else "浮动小窗", exact=True).click()
            assistant.get_by_role("button", name="助手选项", exact=True).click()
            assistant.get_by_role("tab", name="回答", exact=True).click()
            assistant.get_by_label("向知识助手提问", exact=True).fill("原型页未发送草稿")
            if docked:
                expect(page.locator(".workspace")).to_have_class(re.compile(r"\bwith-dock\b"))
            management_pages_have_no_assistant_or_empty_dock()
            page.get_by_role("button", name="原型", exact=True).click()
            expect(assistant.get_by_label("向知识助手提问", exact=True)).to_be_visible()
            expect(assistant.get_by_label("向知识助手提问", exact=True)).to_have_value("原型页未发送草稿")
            page.get_by_role("button", name="文档", exact=True).click()
            expect(document.locator("#fh-ai-question")).to_have_value("文档页未发送草稿")
            assert not errors and not writes
        finally:
            browser.close()
