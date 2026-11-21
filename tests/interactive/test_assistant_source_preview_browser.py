"""Every prototype assistant source opens a preview before changing the reader."""

import json
import re

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway

from tests.interactive import test_assistant
from tests.interactive.test_native_documents_browser import native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


@pytest.mark.parametrize("tab", ["文档", "AI 讲解"])
@pytest.mark.parametrize("detached", [False, True])
def test_numbered_and_card_sources_preview_before_navigation(assistant, tmp_path, monkeypatch, tab, detached):
    (web, _service, _owner, pid, aid, grant, package, section, _), _body, calls = assistant
    binding = test_assistant.form(aid, package, section)
    binding["target"]["frame_path"] = []
    revision = section["revision_id"]
    binding["point"] = {
        "title": "Saved Add binding",
        "summary": (
            f"## 核对依据\n\n[1]《办理规则》（revision ID：`{revision}`）\n\n"
            f"[1]《适用条件》 (revision ID: {revision})\n\n"
            "适用于业务规则 V2.1。代码中的 `[1]` 保持原样。\n\n"
            f"```text\n示例（revision ID：{revision}）\n```"
        ),
    }
    result = web.post(
        test_assistant.root(pid) + "/bindings",
        json=binding,
        headers=_headers(grant, test_assistant.current(web, pid, aid, grant)["selection_version"], "preview-binding"),
    )
    assert result.status_code == 200, result.text

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        response = test_assistant.response(section["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["explanation"] = "讲解准确规则。[1][1]\n\n代码中的 `[1]` 保持原样。"
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            add = source.get_by_role("button", name="Add", exact=True)
            expect(add).to_be_visible(timeout=30000)
            add.click(button="right")
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            host = page
            if detached:
                with page.expect_popup() as opened:
                    page.get_by_role("button", name="弹出独立窗口", exact=True).click()
                host = opened.value
                host.on("pageerror", lambda error: errors.append(str(error)))
            panel = host.get_by_role("complementary", name="知识助手", exact=True)
            expect(panel.get_by_role("tab")).to_have_text(["文档", "AI 讲解", "回答"])
            panel.get_by_role("tab", name=tab, exact=True).click()
            content = panel.get_by_label("关联说明" if tab == "文档" else "AI 功能讲解", exact=True)
            expect(content.locator(".markdown-reference")).to_have_count(2)
            expect(content.locator("p > code")).to_have_text("[1]")
            if tab == "文档":
                expect(content.get_by_text("[1]《办理规则》", exact=True)).to_be_visible()
                expect(content.get_by_text("[1]《适用条件》", exact=True)).to_be_visible()
                expect(content.get_by_text("适用于业务规则 V2.1。", exact=False)).to_be_visible()
                expect(content.locator("p", has_text="revision ID")).to_have_count(0)
                expect(content.locator("pre")).to_contain_text(f"示例（revision ID：{revision}）")
            original_url = page.url
            disclosure = panel.locator(".source-disclosure:visible")
            expect(disclosure).not_to_have_attribute("open", "")

            def preview_stays_on_prototype():
                preview = host.get_by_role("dialog", name="引用原文预览", exact=True)
                expect(preview).to_be_visible()
                expect(preview.locator("header small")).to_have_text(re.compile(r"\S"))
                expect(preview.locator("header").get_by_role("heading")).to_have_count(0)
                expect(preview.locator(".markdown")).to_contain_text(section["content"].splitlines()[-1].lstrip("# "))
                assert page.url == original_url
                expect(panel.locator(".knowledge-context")).to_contain_text("Add")
                expect(page.locator(".document-reader:visible")).to_have_count(0)
                return preview

            content.locator(".markdown-reference").first.click()
            preview = preview_stays_on_prototype()
            preview.get_by_role("button", name="返回", exact=True).click()
            expect(preview).to_have_count(0)
            content.locator(".markdown-reference").last.click()
            preview_stays_on_prototype()
            host.keyboard.press("Escape")
            expect(preview).to_have_count(0)
            expect(panel).to_be_visible()
            disclosure.locator("summary").first.click()
            panel.locator("button.citation:visible").first.click()
            preview_stays_on_prototype()
            preview.get_by_role("button", name="关闭原文预览", exact=True).click()
            panel.locator("button.citation:visible").first.click()
            preview_stays_on_prototype().get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.locator(".document-reader .chapter-selected")).to_be_visible()
            expect(page.locator(".document-reader .reverse-link")).to_have_count(0)
            # The same article also serves the handbook layout.
            page.evaluate("() => {const hash=new URLSearchParams(location.hash.slice(1));hash.set('reader','handbook');location.hash=hash.toString()}")
            expect(page.locator(".hb-article .document-reader")).to_be_visible()
            expect(page.locator(".hb-article .reverse-link")).to_have_count(0)
            assert len(calls) == (1 if tab == "AI 讲解" else 0)
            assert not errors
        finally:
            browser.close()
