"""The project report reconciles settled model and embedding ledger entries."""

import re
from datetime import UTC, datetime

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api.ai_gateway import AiService, ModelUsage, PolicyInput
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
    period_path = f"/api/v3/projects/{pid}/model-usage/period"
    headers = _headers(grant, 0, "read")
    assert web.get(path, params={"analysis_set_id": aid}, headers=headers).status_code == 403
    assert web.get(period_path, headers=headers).status_code == 403
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

    question = web.post(
        root(pid) + "/questions",
        json={**body, "question": "What are the prerequisites?"},
        headers=_headers(grant, 0, "usage-question"),
    )
    assert question.status_code == 200, question.text
    with service.db() as s:
        for usage in s.scalars(select(ModelUsage).where(ModelUsage.project_id == pid)):
            if usage.id == unknown:
                usage.created_at = "2026-10-31T15:59:59.999999Z"
            elif usage.id == restricted:
                usage.created_at = "2026-11-01T16:00:00.000001Z"
                old = s.scalar(select(Audit).where(Audit.resource_id == restricted, Audit.action == "model.reserve"))
                old.metadata_json = {"prototype_target": old.metadata_json["prototype_target"]}
            else:
                usage.created_at = "2026-10-31T16:00:00.000001Z"
    with vectors.db() as db:
        db.execute("UPDATE usage SET day=? WHERE id=?", ("2026-11-01", embed))

    def by_period(period, day):
        response = web.get(period_path, params={"period": period, "date": day})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    november = by_period("month", "2026-11-24")
    assert november == {
        "period": "month", "start_date": "2026-11-01", "total_tokens": 502,
        "answer_tokens": 150, "association_tokens": 300, "embedding_tokens": 45,
        "unclassified_tokens": 7, "other_tokens": 0,
    }
    assert by_period("day", "2026-11-01")["total_tokens"] == 495
    assert by_period("day", "2026-11-02")["unclassified_tokens"] == 7
    assert by_period("day", "2026-11-03")["total_tokens"] == 0
    assert by_period("day", "2026-10-31")["other_tokens"] == 29


def test_report_layout_and_day_month_switch_use_the_real_endpoint(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, package)
    model = AiService(service)
    settings = PolicyInput(model_id="synthetic").model_dump()
    target = {"package_id": package["artifact_id"], "route": "/actual/账务/billing.html", "page_title": "账单中心"}
    request_id = model.reserve(owner, pid, settings, 400, prototype_target=target, purpose="association")
    model.settle(request_id, settings, 700, 300, "succeeded")
    answer_id = model.reserve(owner, pid, settings, 400, purpose="answer")
    model.settle(answer_id, settings, 300, 100, "succeeded")
    other_id = model.reserve(owner, pid, settings, 400)
    model.settle(other_id, settings, 20, 9, "succeeded")
    with service.db() as s:
        s.get(ModelUsage, request_id).created_at = "2026-11-02T01:00:00Z"
        s.get(ModelUsage, answer_id).created_at = "2026-11-01T01:00:00Z"
        s.get(ModelUsage, other_id).created_at = "2026-11-02T01:00:00Z"
    vectors = VectorStore(service.data_dir)
    embed = vectors.reserve(pid, RagSettings(), 100)
    vectors.settle(embed, 45, "succeeded")
    with vectors.db() as db:
        db.execute("UPDATE usage SET day=? WHERE id=?", ("2026-11-01", embed))
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.clock.set_fixed_time(datetime(2026, 11, 21, tzinfo=UTC))
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            report = page.get_by_role("complementary", name="项目 Token 用量", exact=True)
            expect(report.get_by_role("button", name="按月", exact=True)).to_have_attribute("aria-pressed", "true")
            report.get_by_label("统计月份").fill("2026-11")
            expect(report.locator(".token-report-metrics dd")).to_have_text(["1,474", "400", "1,000", "45"])
            expect(report.get_by_role("status")).to_have_text("其他 29")
            manager_panel = page.get_by_role("region", name="关联管理", exact=True)
            manager_box = manager_panel.bounding_box()
            batch = page.get_by_role("region", name="AI 预关联", exact=True)
            task_box, report_box = batch.bounding_box(), report.bounding_box()

            def layout():
                return (report.bounding_box(), report.locator("input").bounding_box(), manager_panel.bounding_box()["y"])

            desktop_layout = layout()
            assert abs(task_box["y"] - report_box["y"]) < 2
            assert abs(task_box["width"] - report_box["width"]) < 2
            assert report_box["x"] > task_box["x"] + task_box["width"]
            assert abs(manager_box["x"] - task_box["x"]) < 2
            assert abs(manager_box["x"] + manager_box["width"] - report_box["x"] - report_box["width"]) < 2
            assert manager_box["y"] > max(task_box["y"] + task_box["height"], report_box["y"] + report_box["height"])
            assert report_box["height"] < 160
            assert abs(batch.locator(".batch-actions").bounding_box()["x"] - manager_box["x"]) < 20
            delayed = []
            day_report = re.compile(r"/model-usage/period\?period=day&date=2026-11-01$")
            page.route(day_report, lambda route: delayed.append(route))
            report.get_by_role("button", name="按日", exact=True).click()
            with page.expect_request(day_report):
                report.get_by_label("统计日期").fill("2026-11-01")
            expect(report.get_by_role("status")).to_have_text("正在读取项目用量…")
            expect(report.locator(".token-report-metrics dd")).to_have_text(["—"] * 4)
            assert layout() == desktop_layout
            delayed.pop().continue_()
            expect(report.locator(".token-report-metrics dt")).to_have_text(["本期合计", "回答", "文档关联", "向量"])
            expect(report.locator(".token-report-metrics dd")).to_have_text(["445", "400", "0", "45"])
            expect(report.get_by_role("status")).to_be_empty()
            assert layout() == desktop_layout
            assert manager_panel.bounding_box() == manager_box
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
            mobile_layout = layout()
            refresh = report.get_by_role("button", name="刷新 Token 用量", exact=True)
            with page.expect_request(day_report):
                refresh.click()
            expect(refresh).to_be_disabled()
            assert layout() == mobile_layout
            delayed.pop().fulfill(status=503, json={})
            expect(report.get_by_role("status")).to_have_text("无法读取 Token 用量，请刷新重试。")
            assert layout() == mobile_layout
            report.get_by_role("button", name="按月", exact=True).click()
            expect(report.locator(".token-report-metrics dd")).to_have_text(["1,474", "400", "1,000", "45"])
            assert layout() == mobile_layout
            expect(report.get_by_role("button", name="用量排行", exact=True)).to_have_count(0)
        finally:
            browser.close()
