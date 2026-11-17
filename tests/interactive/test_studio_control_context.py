"""Explicit assistant actions follow the displayed control, including editable fields."""

import json

from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_assistant
from tests.interactive.test_native_documents_browser import native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


@contextmanager
def control_studio(assistant, tmp_path, monkeypatch, *, saved_binding=False):
    (web, _service, _owner, pid, aid, grant, package, section, _), _body, model_calls = assistant
    requests = []
    if saved_binding:
        binding = test_assistant.form(aid, package, section)
        # This synthetic source renders its page directly in #material; it has
        # no nested actualPage frame from the older import fixture's API form.
        binding["target"]["frame_path"] = []
        binding["point"] = {"title": "Saved Add binding", "summary": "Synthetic manual Add explanation"}
        version = test_assistant.current(web, pid, aid, grant)["selection_version"]
        response = web.post(
            test_assistant.root(pid) + "/bindings",
            json=binding,
            headers=_headers(grant, version, "control-context-binding"),
        )
        assert response.status_code == 200, response.text

    def respond(route):
        endpoint = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
        if endpoint not in {"related", "explain", "conversation"}:
            route.continue_()
            return
        payload = route.request.post_data_json
        requests.append((endpoint, payload))
        label = payload["target"]["label"]
        if endpoint == "related":
            data = {
                "title": label,
                "sections": [],
                "binding_id": None,
                "excluded": False,
                "origin": "retrieval",
                "model_available": True,
                "vector_available": False,
                "selection_version": 0,
                "elapsed_ms": 0,
            }
        else:
            data = {
                "title": "Synthetic " + label,
                "explanation": "Synthetic explanation for " + label,
                "unknowns": [],
                "confidence": 0.95,
                "model_id": "synthetic",
                "cached": False,
                "saved": False,
                "binding_id": None,
                "sections": [],
                "selection_version": 0,
            }
        route.fulfill(json={"data": data})

    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("if(window.top === window) localStorage.setItem('vks.studio.auto-associate','off')")
            page.route("**/api/v3/projects/" + pid + "/interactive/*", respond)
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            add = source.get_by_role("button", name="Add", exact=True)
            expect(add).to_be_visible(timeout=30000)
            add.click()
            field = source.get_by_label("Code", exact=True)
            expect(field).to_be_visible()
            field.fill("Retain this entered value")
            field.click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.locator(".knowledge-context")).to_contain_text("Code")
            expect(panel.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
            # Ordinary field interaction must preserve native input and avoid a
            # background generation; only an explicit assistant action starts it.
            page.wait_for_timeout(450)
            assert not requests
            yield page, panel, source, field, requests
            assert not model_calls
        finally:
            browser.close()


def test_clicked_input_primary_explanation_sends_its_displayed_target(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, _source, field, requests):
        with page.expect_request(lambda request: request.url.endswith("/explain"), timeout=3000):
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("Synthetic explanation for Code")
        assert [endpoint for endpoint, _ in requests] == ["related", "explain"]
        assert all(payload["target"]["label"] == "Code" for _, payload in requests)
        assert all("input" in payload["target"]["selector"] for _, payload in requests)
        expect(field).to_have_value("Retain this entered value")
        panel.get_by_role("button", name="收起助手面板", exact=True).click()
        page.get_by_role("button", name="打开知识助手", exact=True).click()
        expect(panel.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
        assert [endpoint for endpoint, _ in requests] == ["related", "explain"]


@pytest.mark.parametrize("entrypoint", ["footer", "tab"])
def test_clicked_input_other_explanation_entries_send_its_displayed_target(
    assistant, tmp_path, monkeypatch, entrypoint
):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, _source, field, requests):
        with page.expect_request(lambda request: request.url.endswith("/explain"), timeout=3000):
            if entrypoint == "footer":
                panel.get_by_role("tab", name="AI 讲解", exact=True).click()
            else:
                panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("Synthetic explanation for Code")
        if entrypoint == "footer":
            with page.expect_request(lambda request: request.url.endswith("/explain")):
                panel.locator(".knowledge-footer").get_by_role("button", name="重新讲解", exact=True).click()
            expect(panel.locator(".knowledge-footer").get_by_role("button", name="重新讲解", exact=True)).to_be_enabled()
        assert [endpoint for endpoint, _ in requests] == ["related", "explain"] * (2 if entrypoint == "footer" else 1)
        assert all(payload["target"]["label"] == "Code" for _, payload in requests)
        assert all("input" in payload["target"]["selector"] for _, payload in requests)
        expect(field).to_have_value("Retain this entered value")


def test_clicked_input_questions_send_its_displayed_target(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, _source, field, requests):
        panel.get_by_role("tab", name="回答", exact=True).click()
        panel.get_by_label("向知识助手提问", exact=True).fill("Explain the selected field")
        with page.expect_request(lambda request: request.url.endswith("/conversation"), timeout=3000):
            panel.get_by_role("button", name="发送问题", exact=True).click()
        expect(panel.locator(".chat-answer")).to_contain_text("Synthetic explanation for Code")
        assert [endpoint for endpoint, _ in requests] == ["conversation"]
        assert requests[0][1]["target"]["label"] == "Code"
        assert "input" in requests[0][1]["target"]["selector"]
        expect(field).to_have_value("Retain this entered value")


def test_clicked_input_does_not_start_automatic_assistance_when_enabled(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, _source, field, requests):
        panel.get_by_role("button", name="助手选项", exact=True).click()
        panel.get_by_label("自动关联模块与控件", exact=True).check()
        panel.get_by_role("button", name="返回知识内容", exact=True).click()
        field.click()
        expect(panel.locator(".knowledge-context")).to_contain_text("Code")
        page.wait_for_timeout(600)
        assert not requests
        expect(field).to_have_value("Retain this entered value")


def test_cancelled_picker_keeps_new_control_as_explanation_target(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, source, field, requests):
        panel.get_by_role("button", name="助手选项", exact=True).click()
        panel.get_by_role("button", name="手动补充关联", exact=True).click()
        source.get_by_role("button", name="Close", exact=True).click()
        expect(panel.locator(".knowledge-context")).to_contain_text("Close")
        expect(panel.get_by_label("业务说明", exact=True)).to_be_visible()
        panel.locator(".knowledge-footer").get_by_role("button", name="取消", exact=True).click()
        expect(panel).to_have_count(0)
        page.get_by_role("button", name="打开知识助手", exact=True).click()
        with page.expect_request(lambda request: request.url.endswith("/explain"), timeout=3000):
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("Synthetic explanation for Close")
        assert [endpoint for endpoint, _ in requests] == ["related", "explain"]
        assert all(payload["target"]["label"] == "Close" for _, payload in requests)
        expect(field).to_have_value("Retain this entered value")


def test_cancelled_existing_binding_edit_uses_binding_control_for_explanation(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch, saved_binding=True) as (
        page,
        panel,
        _source,
        _field,
        requests,
    ):
        panel.get_by_role("button", name="助手选项", exact=True).click()
        panel.get_by_role("button", name="关联管理", exact=True).click()
        page.get_by_role("button", name="查看关联详情：Saved Add binding", exact=True).click()
        page.get_by_role("article", name="关联详情").get_by_role("button", name="编辑", exact=True).click()
        expect(panel.locator(".knowledge-context")).to_contain_text("Add")
        expect(panel.get_by_label("业务说明", exact=True)).to_have_value("Synthetic manual Add explanation")
        panel.locator(".knowledge-footer").get_by_role("button", name="取消", exact=True).click()
        expect(panel).to_have_count(0)
        page.get_by_role("article", name="关联详情").get_by_role("button", name="打开原型", exact=True).click()
        with page.expect_request(lambda request: request.url.endswith("/explain"), timeout=3000):
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("Synthetic explanation for Add")
        assert [endpoint for endpoint, _ in requests] == ["explain"]
        assert requests[0][1]["target"]["label"] == "Add"
        assert requests[0][1]["target"]["selector"] == "#add"


def test_explanation_reuses_target_after_context_tabs_and_switch_back(assistant, tmp_path, monkeypatch):
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, source, field, requests):
        panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        answer = panel.get_by_role("region", name="AI 功能讲解")
        expect(answer).to_contain_text("Synthetic explanation for Code")
        footer = panel.locator(".knowledge-footer")
        expect(footer.get_by_role("button", name="重新讲解", exact=True)).to_have_count(1)
        expect(panel.locator(".assistant-answer-title button")).to_have_count(0)
        expect(footer.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
        for tab in ("文档", "回答", "AI 讲解"):
            panel.get_by_role("tab", name=tab, exact=True).click()
            if tab == "回答":
                expect(footer.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
        field.fill("Changed nearby runtime context")
        page.wait_for_timeout(450)  # Let the runtime emit its debounced context update.
        panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(answer).to_contain_text("Synthetic explanation for Code")
        page.wait_for_timeout(450)
        assert len([p for endpoint, p in requests if endpoint == "explain"]) == 1
        source.get_by_role("button", name="Close", exact=True).click(button="right")
        page.get_by_role("menuitem", name="AI 讲解此功能", exact=True).press("Enter")
        expect(answer).to_contain_text("Synthetic explanation for Close")
        field.click()
        panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(answer).to_contain_text("Synthetic explanation for Code")
        assert len([p for endpoint, p in requests if endpoint == "explain"]) == 2
        with page.expect_request(lambda r: r.url.endswith("/explain")):
            footer.get_by_role("button", name="重新讲解", exact=True).click()
        expect(footer.get_by_role("button", name="重新讲解", exact=True)).to_be_enabled()
        explanations = [p for endpoint, p in requests if endpoint == "explain"]
        assert len(explanations) == 3 and explanations[-1]["refresh"] is True
        assert explanations[-1]["target"]["label"] == "Code"


def test_pending_explanation_is_not_restarted_by_tabs_or_same_target(assistant, tmp_path, monkeypatch):
    held = []
    with control_studio(assistant, tmp_path, monkeypatch) as (page, panel, _source, field, _requests):
        page.route("**/interactive/explain", lambda route: held.append(route))
        panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        expect(panel.get_by_role("button", name="重新讲解", exact=True)).to_be_disabled()
        field.click(button="right")
        page.get_by_role("menuitem", name="AI 讲解此功能", exact=True).press("Enter")
        for tab in ("文档", "回答", "AI 讲解"):
            panel.get_by_role("tab", name=tab, exact=True).click()
        page.set_viewport_size({"width": 1200, "height": 800})
        page.wait_for_timeout(450)
        assert len(held) == 1
        held[0].fulfill(json={"data": {"title": "One explanation", "explanation": "The in-flight explanation completed", "unknowns": [], "confidence": .9, "model_id": "synthetic", "cached": False, "saved": False, "binding_id": None, "sections": [], "selection_version": 0}})
        expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("The in-flight explanation completed")
        panel.get_by_role("tab", name="文档", exact=True).click()
        panel.get_by_role("tab", name="AI 讲解", exact=True).click()
        assert len(held) == 1
