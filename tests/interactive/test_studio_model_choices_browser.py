"""A large returned model catalog stays compact without losing exact selections."""

import json

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_model_choices_expand_page_search_and_preserve_current(materials, tmp_path, monkeypatch):
    web, _, _, _, projects = materials
    pid, aid = projects[0]
    ids = ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra", "gpt-6-sol"]
    ids += [f"gpt-5-{index:02d}" for index in range(35)]
    ids += ["gpt-image-2", "text-embedding-3-small"]
    models = [{"id": model, "name": model, "description": ""} for model in ids]
    settings = {
        "endpoint": "https://api.openai.com/v1",
        "protocol": "responses",
        "model_id": "gpt-5-34",
        "max_output_tokens": 2500,
        "enabled": True,
        "allow_external": True,
    }
    catalog = {
        "providers": [{"id": "openai", "name": "OpenAI", "description": "", **settings, "models": models}],
        "connections": [{"id": "fixture", "name": "OpenAI", "provider": "openai", "settings": settings,
                         "configured": True, "active": True}],
        "policy": {"row_version": 1, **settings},
        "legacy_configured": False,
    }
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.route("**/model-connections", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps({"data": catalog})
            ))
            page.route("**/model-connections/models", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps({"data": {"models": models}})
            ))
            login(page, url, pid, aid)
            page.get_by_role("button", name="AI 设置", exact=True).click()
            select = page.get_by_label("回答模型", exact=True)
            options = select.locator("optgroup option")
            expect(options).to_have_count(5)
            expect(select).to_have_value("gpt-5-34")
            expect(options).to_have_text([
                "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol"
            ])
            page.get_by_role("button", name="查看全部模型（42）", exact=True).click()
            expect(options).to_have_count(20)
            expect(select).to_have_value("gpt-5-34")
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(options).to_have_count(20)
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(options).to_have_text(["gpt-image-2", "text-embedding-3-small"])
            expect(select).to_have_value("gpt-5-34")
            page.get_by_label("搜索模型", exact=True).fill("gpt-5-12")
            expect(options).to_have_text(["gpt-5-12"])
            select.select_option("gpt-5-12")
            page.get_by_role("button", name="收起其他模型", exact=True).click()
            expect(options).to_have_count(5)
            expect(select).to_have_value("gpt-5-12")
            expect(page.get_by_role("button", name="保存并使用", exact=True)).to_be_enabled()
        finally:
            browser.close()
