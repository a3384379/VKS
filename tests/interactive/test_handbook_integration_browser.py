"""Real Studio integration: persisted template, scoped search, document-only AI."""

import json

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.document_presentation import DocumentPresentation, PresentationService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import uid

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_template_persistence_indexed_search_document_only_ai_and_update(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs(
            [
                (
                    "guide/overview.md",
                    "# Overview\n\n## Introduction\n\n**Source emphasis.**\n\nA secret-jellyfish is present only in the article body.",
                ),
                ("business/rules.md", "# Rules\n\n## Billing policy\n\nVerify the invoice total."),
            ]
        ),
        filename="synthetic-docs.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    before = links.catalog(owner, pid, aid)
    assert not [item for item in before["packages"] if item.get("kind") == "prototype"]
    with service.db() as db:
        db.add(DocumentPresentation(analysis_set_id=aid, settings={"mode": "handbook"}))
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    model_calls = []

    def model(_config, body):
        payload = json.loads(body["input"][1]["content"])
        model_calls.append(payload)
        answer = test_assistant.response(payload["chapters"][0]["revision_id"])
        output = answer["output"][0]["content"][0]
        content = json.loads(output["text"])
        content["explanation"] = "**Document-only answer.** Grounded in the selected project documents."
        output["text"] = json.dumps(content)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request))
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            expect(page.get_by_label("文档阅读方式", exact=True)).to_have_count(0)
            book = page.get_by_role("region", name="知识手册阅读模板")
            expect(book).to_be_visible()
            assert PresentationService(service).get(owner, pid, aid)["settings"]["mode"] == "handbook"
            assert links.catalog(owner, pid, aid)["documents"] == before["documents"]
            page.reload()
            expect(book).to_be_visible()
            expect(page.get_by_label("文档阅读方式", exact=True)).to_have_count(0)
            book.get_by_role("button", name="搜索全文", exact=True).click()
            dialog = page.get_by_role("dialog", name="手册全文搜索")
            dialog.get_by_label("手册搜索内容", exact=True).fill("secret-jellyfish")
            dialog.locator(".hb-search-results button").first.click()
            expect(book.locator(".document-reader").get_by_role("heading", name="Overview", level=1, exact=True)).to_be_visible()
            expect(book.locator(".hb-article strong").get_by_text("Source emphasis.", exact=True)).to_be_visible()
            assert any("/document-presentation/search" in request.url for request in requests)
            book.get_by_role("button", name="总览", exact=True).click()
            expect(book.locator(".hb-cover")).to_be_visible()
            book.get_by_role("button", name="向手册 AI 提问", exact=True).click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            panel.locator("#fh-ai-question").fill("What should I verify before billing?")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant strong").first).to_have_text("Document-only answer.")
            assert len(model_calls) == 1 and model_calls[0]["chapters"]
            assert not links.bindings(owner, pid, aid)
            question = next(
                request.post_data_json for request in requests if request.url.endswith("/interactive/conversation")
            )
            assert question["package_id"] is None and question["document_scope"] == "all"
            panel.locator("#fh-ai-close").click()
            book.get_by_role("button", name="更新文档", exact=True).click()
            expect(page.get_by_role("heading", name="原型与文档", exact=True)).to_be_visible()
            export_mode = page.get_by_label("导出文档呈现", exact=True)
            expect(export_mode).to_have_value("project")
            export_mode.select_option("original")
            expect(export_mode).to_have_value("original")
            assert not errors, errors
        finally:
            browser.close()
