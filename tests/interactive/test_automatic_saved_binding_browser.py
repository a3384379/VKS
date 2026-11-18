"""Opening saved associations never regenerates them while links load or context drifts."""

from copy import deepcopy

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.materials import MaterialService

from tests.interactive import test_assistant
from tests.interactive.test_explanation_separation import knowledge_snapshot
from tests.interactive.test_native_documents_browser import native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_saved_associations_are_reused_with_default_automatic_and_delayed_binding_list(
    assistant, tmp_path, monkeypatch
):
    (web, service, owner, pid, aid, grant, package, _section, _raw), body, calls = assistant
    MaterialService(service).open(owner, pid, aid)
    control = deepcopy(body)
    control["target"]["frame_path"] = []
    module = deepcopy(control)
    module["target"].update(selector="body", label="收费项管理")
    module["context"]["scope"] = "module"
    # Stored wording has a different captured context from the runtime's current
    # nearby labels. An ordinary assist cannot rely on an exact cache-key hit.
    engine = InteractiveAssistant(service)
    for record in (module, control):
        result = engine.run(owner, pid, AssistanceInput.model_validate(record))
        assert result["saved"] and result["binding_id"]
    baseline = knowledge_snapshot(service, aid)
    initial_calls = len(calls)
    assert initial_calls == 2
    requests, held = [], []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                requests.append((request.url.rsplit("/", 1)[-1], request.post_data_json))
                if request.method == "POST"
                and request.url.rsplit("/", 1)[-1] in {"related", "assist", "reassociate", "explain"}
                else None
            ),
        )

        def hold_bindings(route):
            response = route.fetch()
            held.append((route, response))

        page.route("**/interactive/bindings?*", hold_bindings)
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台").click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            add = source.get_by_role("button", name="Add", exact=True)
            expect(add).to_be_visible(timeout=30000)
            page.wait_for_timeout(800)  # Exceeds the 350ms automatic-generation debounce.
            assert held and page.evaluate("localStorage.getItem('vks.studio.auto-associate')") is None
            assert not [endpoint for endpoint, _ in requests if endpoint in {"assist", "reassociate"}], requests
            assert len(calls) == initial_calls
            for route, response in held:
                route.fulfill(response=response)
            held.clear()
            page.unroute("**/interactive/bindings?*", hold_bindings)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.locator(".knowledge-context")).to_contain_text("收费项管理")
            expect(panel).to_contain_text("Document-grounded explanation")
            panel.get_by_role("button", name="收起助手面板", exact=True).click()
            # Right-click reads the same exact control without opening its dialog.
            add.click(button="right", position={"x": 2, "y": 2})
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            expect(panel.locator(".knowledge-context")).to_contain_text("Add")
            expect(panel).to_contain_text("Document-grounded explanation")
            page.wait_for_timeout(450)
            assert not [endpoint for endpoint, _ in requests if endpoint in {"assist", "reassociate"}], requests
            assert len(calls) == initial_calls and knowledge_snapshot(service, aid) == baseline
            source.locator("body").evaluate(
                "el=>{const blank=document.createElement('div');blank.id='blank-module-space';blank.style.cssText='height:150px;width:600px';el.append(blank)}"
            )
            source.locator("#blank-module-space").click()
            expect(panel.locator(".knowledge-context")).to_contain_text("收费项管理")
            page.wait_for_timeout(450)
            assert not [endpoint for endpoint, _ in requests if endpoint in {"assist", "reassociate"}], requests
            assert len(calls) == initial_calls and knowledge_snapshot(service, aid) == baseline
            assert not errors
        finally:
            for route, response in held:
                try:
                    route.fulfill(response=response)
                except Exception:
                    pass
            page.unroute("**/interactive/bindings?*", hold_bindings)
            browser.close()


@pytest.mark.parametrize("control_state", ["missing", "manual", "excluded"])
def test_default_automatic_only_fills_missing_targets(assistant, tmp_path, monkeypatch, control_state):
    (web, service, owner, pid, aid, grant, package, section, _raw), body, calls = assistant
    MaterialService(service).open(owner, pid, aid)
    module = deepcopy(body)
    module["target"].update(frame_path=[], selector="body", label="收费项管理")
    module["context"]["scope"] = "module"
    result = InteractiveAssistant(service).run(owner, pid, AssistanceInput.model_validate(module))
    assert result["saved"]
    if control_state != "missing":
        binding = test_assistant.form(aid, package, section)
        binding["target"]["frame_path"] = []
        binding["point"] = {"title": "Manual remains", "summary": "Keep my manual rule"}
        version = test_assistant.current(web, pid, aid, grant)["selection_version"]
        response = web.post(
            test_assistant.root(pid) + "/bindings", json=binding, headers=_headers(grant, version, "manual-control")
        )
        assert response.status_code == 200, response.text
        if control_state == "excluded":
            links = web.get(
                test_assistant.root(pid) + "/bindings",
                params={"analysis_set_id": aid},
                headers=_headers(grant, 0, "read"),
            ).json()["data"]
            control = next(link for link in links if link["target"]["selector"] == "#add")
            version = test_assistant.current(web, pid, aid, grant)["selection_version"]
            response = web.post(
                test_assistant.root(pid) + f"/bindings/{control['id']}/exclude",
                json={"analysis_set_id": aid, "binding_version": control["binding_version"], "excluded": True},
                headers=_headers(grant, version, "excluded-control"),
            )
            assert response.status_code == 200, response.text
    baseline, initial_calls = knowledge_snapshot(service, aid), len(calls)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.get_by_role("button", name="Add", exact=True)).to_be_visible(timeout=30000)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.locator(".knowledge-context")).to_contain_text("收费项管理")
            panel.get_by_role("button", name="收起助手面板", exact=True).click()
            if control_state == "missing":
                with page.expect_response(
                    lambda response: response.url.endswith("/interactive/assist"), timeout=10000
                ) as generated:
                    source.get_by_role("button", name="Add", exact=True).click()
                assert generated.value.json()["data"]["saved"]
                assert len(calls) == initial_calls + 1
                assert knowledge_snapshot(service, aid) != baseline
            else:
                source.get_by_role("button", name="Add", exact=True).click()
                page.wait_for_timeout(700)
                assert len(calls) == initial_calls
                assert knowledge_snapshot(service, aid) == baseline
        finally:
            browser.close()
