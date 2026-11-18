"""The diff and confirmed association show one total title after a repeated model header."""

import json

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.association_inference import response_for

from tests.interactive import test_assistant, test_knowledge
from tests.interactive.test_association_text import BODY, TITLE
from tests.interactive.test_native_documents_browser import native_studio
from tests.interactive.test_reassociation_preview import bindings
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_diff_markdown_and_confirmation_keep_title_out_of_generated_body(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, grant, package, section, _), _request, calls = assistant
    initial = test_knowledge.form(aid, package, section)
    initial["target"]["frame_path"] = []
    result = web.post(
        test_knowledge.root(pid) + "/bindings",
        json=initial,
        headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "initial"),
    )
    assert result.status_code == 200
    old = bindings(web, pid, aid, grant)

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        return response_for(
            {
                "title": TITLE,
                "explanation": f"# {TITLE}\n\n# {TITLE}\n\n" + BODY,
                "section_revision_ids": [data["chapters"][0]["revision_id"]],
                "confidence": 0.95,
                "unknowns": [],
            },
            "responses",
            (100, 50),
            [],
        )

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            add = (
                page.frame_locator('iframe[title="原 HTML 原型"]')
                .frame_locator("#material")
                .get_by_role("button", name="Add", exact=True)
            )
            expect(add).to_be_visible(timeout=30000)
            add.click(button="right")
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手", exact=True)
            panel.get_by_role("button", name="AI 重新关联", exact=True).click()
            review = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(review.get_by_role("heading", name=TITLE, exact=True)).to_be_visible(timeout=15000)
            after = review.locator(".association-diff-row > div:last-child code")
            assert after.all_text_contents().count("# " + TITLE) == 1
            assert bindings(web, pid, aid, grant) == old
            page.screenshot(path=str(tmp_path / "single-title-diff.png"))
            review.get_by_role("tab", name="Markdown 预览", exact=True).click()
            expect(review.locator(".markdown h1")).to_have_count(0)
            expect(review.locator(".markdown h3")).to_have_text("功能与适用范围")
            expect(review.locator("table th")).to_have_text(["参数", "计算规则"])
            review.get_by_role("button", name="确认保存关联", exact=True).click()
            expect(panel.locator(".related-results > h2")).to_have_text(TITLE)
            expect(panel.locator(".related-results .markdown h1")).to_have_count(0)
            saved = bindings(web, pid, aid, grant)[0]
            assert saved["title"] == TITLE and saved["summary"] == BODY
            assert len(calls) == 1 and not errors
        finally:
            browser.close()
