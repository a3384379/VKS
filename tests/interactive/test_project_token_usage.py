"""The project report reconciles settled model and embedding ledger entries."""

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api.ai_gateway import AiService, PolicyInput
from vks_api.models import Audit, uid
from vks_api.vector_rag import RagSettings, VectorStore

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_knowledge import root
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
materials = test_materials.materials


def test_report_reconciles_current_and_legacy_targets_and_checks_permissions(assistant):
    (web, service, owner, pid, aid, grant, *_), body, calls = assistant
    path = f"/api/v3/projects/{pid}/model-usage/summary"
    headers = _headers(grant, 0, "read")
    assert web.get(path, params={"analysis_set_id": aid}, headers=headers).status_code == 403
    login_response = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
    assert login_response.status_code == 200
    headers = {"X-CSRF-Token": login_response.json()["data"]["csrf_token"]}
    response = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "assist"))
    assert response.status_code == 200 and response.json()["data"]["saved"], response.text
    binding = web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=headers).json()["data"][0]
    response = web.post(
        root(pid) + "/reassociate",
        json={
            **body,
            "binding_id": binding["id"],
            "binding_version": binding["binding_version"],
            "point_revision_id": binding["point_revision_id"],
            "instruction": "核对适用前提",
        },
        headers=_headers(grant, 0, "reassociate"),
    )
    assert response.status_code == 200 and response.json()["data"]["preview_id"], response.text
    assert len(calls) == 2

    with service.db() as s:
        audit = list(s.scalars(select(Audit).where(Audit.project_id == pid, Audit.action == "model.reserve")))
        assert len(audit) == 2
        assert all(row.metadata_json["prototype_target"]["route"] == body["target"]["route"] for row in audit)

    model = AiService(service)
    settings = PolicyInput(model_id="synthetic").model_dump()
    unknown = model.reserve(owner, pid, settings, 400)
    model.settle(unknown, settings, 20, 9, "failed")
    restricted = model.reserve(
        owner,
        pid,
        settings,
        400,
        prototype_target={
            "package_id": uid(),
            "route": "/hidden",
            "page_title": "隐藏位置",
        },
    )
    model.settle(restricted, settings, 5, 2, "succeeded")
    model.reserve(owner, pid, settings, 400)
    unsent = model.reserve(owner, pid, settings, 400)
    model.settle(unsent, settings, 0, 0, "not_sent")
    other = service.create_project(owner, {"key": "token-other", "name": "Other"}, uid())["id"]
    unrelated = model.reserve(owner, other, settings, 400)
    model.settle(unrelated, settings, 1000, 1000, "succeeded")

    vectors = VectorStore(service.data_dir)
    embed = vectors.reserve(pid, RagSettings(), 100)
    vectors.settle(embed, 45, "succeeded")
    vectors.reserve(pid, RagSettings(), 100)
    vec_other = vectors.reserve(other, RagSettings(), 100)
    vectors.settle(vec_other, 90, "succeeded")

    def read():
        result = web.get(path, params={"analysis_set_id": aid})
        assert result.status_code == 200, result.text
        return result.json()["data"]

    data = read()
    assert data["model"] == {"input_tokens": 225, "output_tokens": 111}
    assert data["unattributed"] == {"input_tokens": 25, "output_tokens": 11}
    assert data["embedding_tokens"] == 45
    assert len(data["functions"]) == 1
    usage = data["functions"][0]
    assert usage["package_id"] == body["package_id"]
    assert usage["route"] == body["target"]["route"]
    assert (usage["input_tokens"], usage["output_tokens"]) == (200, 100)
    assert "隐藏位置" not in str(data)

    # Legacy receipts and immutable preview bytes identify the same two requests.
    with service.db() as s:
        for old in audit:
            s.get(Audit, old.id).metadata_json = {}
    assert read() == data


def test_report_layout_and_module_feature_switch_use_the_real_endpoint(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, package)
    model = AiService(service)
    settings = PolicyInput(model_id="synthetic").model_dump()
    target = {"package_id": package["artifact_id"], "route": "/actual/账务/billing.html", "page_title": "账单中心"}
    request_id = model.reserve(owner, pid, settings, 400, prototype_target=target)
    model.settle(request_id, settings, 700, 300, "succeeded")
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            report = page.get_by_role("complementary", name="项目 Token 用量", exact=True)
            expect(report.locator(".token-report-total strong")).to_have_text("1,000")
            manager_panel = page.get_by_role("region", name="关联管理", exact=True)
            manager_box = manager_panel.bounding_box()
            batch = page.get_by_role("region", name="AI 预关联", exact=True)
            task_box, report_box = batch.bounding_box(), report.bounding_box()
            assert abs(task_box["y"] - report_box["y"]) < 2
            assert abs(task_box["width"] - report_box["width"]) < 2
            assert report_box["x"] > task_box["x"] + task_box["width"]
            assert abs(manager_box["x"] - task_box["x"]) < 2
            assert abs(manager_box["x"] + manager_box["width"] - report_box["x"] - report_box["width"]) < 2
            assert manager_box["y"] > max(task_box["y"] + task_box["height"], report_box["y"] + report_box["height"])
            assert report_box["height"] < 160
            assert abs(batch.locator(".batch-actions").bounding_box()["x"] - manager_box["x"]) < 20
            report.get_by_role("button", name="用量排行", exact=True).click()
            ranking = page.get_by_role("dialog", name="项目 Token 用量排行", exact=True)
            expect(ranking.get_by_role("list", name="模块用量排行")).to_contain_text("账务")
            ranking.get_by_role("button", name="按功能", exact=True).click()
            expect(ranking.get_by_role("list", name="功能用量排行")).to_contain_text("账单中心")
            assert ranking.bounding_box()["width"] <= 600
            assert ranking.bounding_box()["height"] <= 540
            assert manager_panel.bounding_box() == manager_box
            page.keyboard.press("Escape")
            expect(ranking).not_to_be_visible()
            field_boxes = manager_panel.locator(".binding-filter-fields > *").evaluate_all(
                "fields=>fields.map(field=>Math.round(field.getBoundingClientRect().top))"
            )
            assert len(field_boxes) == 3 and len(set(field_boxes)) == 1
            table = manager_panel.locator(".binding-table-wrap")
            assert table.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            page.set_viewport_size({"width": 390, "height": 720})
            manager_box, report_box, task_box = (
                manager_panel.bounding_box(),
                report.bounding_box(),
                batch.bounding_box(),
            )
            assert report_box["y"] > task_box["y"] + task_box["height"]
            assert manager_box["y"] > report_box["y"] + report_box["height"]
            assert page.locator("body").evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            assert table.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            report.get_by_role("button", name="用量排行", exact=True).click()
            expect(ranking).to_be_visible()
            assert ranking.bounding_box()["width"] <= 390 - 32
            assert ranking.bounding_box()["height"] <= 720 - 32
        finally:
            browser.close()
