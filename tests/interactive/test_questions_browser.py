"""Contextual chat uses the configured gateway, citations and isolated UI state."""

import json
from threading import Event

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway

from tests.interactive import test_assistant
from tests.interactive.test_native_documents_browser import native_studio
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_contextual_questions_followup_citations_cancel_and_clear(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _), _body, calls = assistant
    release = Event()

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        if data["question"] == "等待回答":
            release.wait(15)
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["explanation"] = (
            "**办理依据**\n\n第一步先核对资料。\n\n```mermaid\nflowchart LR\n A[核对资料] --> B[办理业务]\n```"
        )
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    try:
        with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.add_init_script(
                    "if(window.top === window) localStorage.setItem('vks.studio.auto-associate','off')"
                )
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
                page.get_by_label("密码", exact=True).fill("owner")
                page.get_by_role("button", name="打开交互工作台").click()
                source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
                add = source.get_by_role("button", name="Add", exact=True)
                expect(add).to_be_visible(timeout=30000)
                add.click(button="right")
                page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
                panel = page.get_by_role("complementary", name="知识助手")
                expect(panel.get_by_role("tab", name="AI 问答", exact=True)).to_have_attribute("aria-selected", "true")
                question = panel.get_by_label("向知识助手提问", exact=True)
                question.fill("需要什么条件？")
                question.press("Shift+Enter")
                assert not calls
                question.press("Enter")
                expect(panel.locator(".chat-answer .markdown > p > strong")).to_have_text("办理依据")
                expect(panel.locator(".chat-answer .diagram-image img")).to_be_visible(timeout=30000)
                question_box = panel.locator(".chat-question").bounding_box()
                answer_box = panel.locator(".chat-answer").bounding_box()
                assert question_box["x"] > answer_box["x"] + 20
                assert abs(question_box["x"] + question_box["width"] - answer_box["x"] - answer_box["width"]) < 2
                page.screenshot(path=str(tmp_path / "knowledge-chat.png"))
                assert calls[0]["question"] == "需要什么条件？"
                assert calls[0]["control"]["selector"] == "#add"
                question.fill("然后呢？")
                panel.get_by_role("button", name="发送问题", exact=True).click()
                expect(panel.locator(".chat-answer")).to_have_count(2)
                assert [m["role"] for m in calls[1]["history"]] == ["user", "assistant"]
                add.click(button="right", position={"x": 2, "y": 2})
                page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
                expect(panel.locator(".chat-answer")).to_have_count(2)
                page.get_by_role("button", name="收起助手面板", exact=True).click()
                page.get_by_role("button", name="打开知识助手", exact=True).click()
                expect(panel.locator(".chat-answer")).to_have_count(2)
                panel.get_by_role("tab", name="相关文档", exact=True).click()
                panel.get_by_role("tab", name="AI 问答", exact=True).click()
                expect(panel.locator(".chat-answer")).to_have_count(2)
                question.fill("等待回答")
                question.press("Enter")
                expect(panel.get_by_role("button", name="停止等待", exact=True)).to_be_visible()
                panel.get_by_role("button", name="停止等待", exact=True).click()
                expect(question).to_have_value("等待回答")
                release.set()
                expect(panel.locator(".chat-answer")).to_have_count(2)
                panel.locator(".chat-sources button").first.click()
                expect(page.locator(".document-reader .chapter-selected")).to_be_visible()
                page.get_by_role("button", name="打开知识助手", exact=True).click()
                panel.get_by_role("button", name="清空问答", exact=True).click()
                expect(panel.locator(".chat-answer")).to_have_count(0)
                page.set_viewport_size({"width": 360, "height": 740})
                expect(question).to_be_in_viewport(ratio=1)
                expect(panel.get_by_role("button", name="发送问题", exact=True)).to_be_in_viewport(ratio=1)
                assert not errors, errors
            finally:
                release.set()
                browser.close()
    finally:
        release.set()


def test_abandoned_question_does_not_resume_waiting_when_returning_to_control(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _), _body, calls = assistant
    release = Event()

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        if data.get("question") == "等待后切换上下文":
            release.wait(15)
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    try:
        with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.add_init_script(
                    "if(window.top === window) localStorage.setItem('vks.studio.auto-associate','off')"
                )
                page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
                page.get_by_label("密码", exact=True).fill("owner")
                page.get_by_role("button", name="打开交互工作台").click()
                add = (
                    page.frame_locator('iframe[title="原 HTML 原型"]')
                    .frame_locator("#material")
                    .get_by_role("button", name="Add", exact=True)
                )
                expect(add).to_be_visible(timeout=30000)
                add.click(button="right")
                page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
                panel = page.get_by_role("complementary", name="知识助手")
                question = panel.get_by_label("向知识助手提问", exact=True)
                question.fill("等待后切换上下文")
                question.press("Enter")
                expect(question).to_be_disabled()
                panel.get_by_role("button", name="助手选项", exact=True).click()
                panel.get_by_role("button", name="模块知识", exact=True).click()
                panel.get_by_role("tab", name="AI 问答", exact=True).click()
                expect(question).to_be_enabled()
                add.click(button="right")
                page.get_by_role("menuitem", name="向 AI 提问", exact=True).click()
                expect(question).to_be_enabled(timeout=2000)
                expect(panel.locator(".chat-status")).to_have_count(0)
                release.set()
                expect(panel.locator(".chat-answer")).to_have_count(0)
            finally:
                release.set()
                browser.close()
    finally:
        release.set()
