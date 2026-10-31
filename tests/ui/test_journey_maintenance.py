"""Saving and URL normalization retain maintenance only for the same actual baseline."""

from test_frontend_browser import site as site  # pytest fixture registration

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from test_frontend_browser import chromium_path
from vks_api.knowledge import KnowledgeService
from vks_api.models import Audit

pass


def test_default_baseline_save_keeps_maintenance_and_actual_baseline_changes_restore_reading(site, monkeypatch):
    monkeypatch.delenv("VKS_DEFAULT_MANUAL_REVIEW", raising=False)
    service, url = site.service, site[0]
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(
        owner, dict(key="maintenance-baseline", name="Maintenance lifecycle"), "maintenance-baseline"
    )["id"]
    imported = service.demo_import(owner, pid, "maintenance-material")
    service.process_job(imported["id"])
    aid = service.workspace(owner, pid)["selected_analysis_set"]["id"]
    with service.db() as db:
        polling = service._new_job(db, owner, pid, "index", {"analysis_set_id": aid})
        polling_id = polling.id
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            try:
                page.goto(url + f"/#view=journeys&project={pid}")
                page.get_by_label("登录名", exact=True).fill("owner")
                page.get_by_label("密码", exact=True).fill("owner")
                page.get_by_role("button", name="打开工作空间").click()
                page.get_by_role("button", name="维护导览与结构", exact=True).click()
                page.get_by_role("button", name="新建流程", exact=True).click()
                title = page.get_by_label("流程名称", exact=True)
                title.fill("Saved without leaving maintenance")
                with page.expect_response(
                    lambda response: response.request.method == "POST" and response.url.endswith("/journeys")
                ) as created:
                    page.get_by_role("button", name="保存流程", exact=True).click()
                assert created.value.ok
                row = created.value.json()["data"]
                expect(page.get_by_text("流程已保存为不可变修订", exact=True)).to_be_visible()
                expect(title).to_have_value("Saved without leaving maintenance")
                expect(page.get_by_text("流程已保存为不可变修订，等待人员审核", exact=True)).to_have_count(0)
                workspace = page.request.get(url + f"/api/v1/projects/{pid}/workspace").json()["data"]
                assert any(entity["entity_id"] == row["journey_id"] for entity in workspace["entities"])
                with service.db() as db:
                    approval = db.scalar(
                        select(Audit).where(Audit.resource_id == row["revision_id"], Audit.action == "journey.save")
                    )
                    assert approval
                # Pinning the already selected default is URL normalization,
                # so it must preserve the current editor and its next undo.
                with page.expect_response(
                    lambda response: "/workspace?" in response.url and f"analysis_set_id={aid}" in response.url
                ):
                    page.evaluate(
                        "baseline => { const query = new URLSearchParams(location.hash.slice(1)); query.set('baseline', baseline); location.hash = query.toString(); }",
                        "draft:" + aid,
                    )
                expect(title).to_have_value("Saved without leaving maintenance")
                title.fill("Next local edit")
                page.get_by_role("button", name="撤销", exact=True).click()
                expect(title).to_have_value("Saved without leaving maintenance")
                # A new default is a different actual baseline and must enter reading.
                first = KnowledgeService(service).clone(owner, pid, aid, "New default", "maintenance-default-one")
                page.get_by_label("知识基线", exact=True).select_option("")
                expect(page.get_by_role("button", name="维护导览与结构", exact=True)).to_be_visible()
                expect(title).to_have_count(0)
                expect(page.get_by_label("当前导览", exact=True)).to_be_visible()
                page.get_by_role("button", name="维护导览与结构", exact=True).click()
                expect(title).to_be_visible()
                # The default can also move during the normal workspace poll
                # without changing the URL baseline parameter at all.
                expected = {}
                with page.expect_response(
                    lambda response: (
                        "/workspace?" in response.url
                        and response.status == 200
                        and expected.get("id")
                        and response.json()["data"].get("selected_analysis_set", {}).get("id") == expected["id"]
                    )
                ):
                    second = KnowledgeService(service).clone(
                        owner, pid, first["id"], "Later default", "maintenance-default-two"
                    )
                    expected["id"] = second["id"]
                expect(page.get_by_role("button", name="维护导览与结构", exact=True)).to_be_visible()
                expect(title).to_have_count(0)
                # A dirty editor is retained in its original fixed baseline;
                # a newly selected default must never receive its pending DSL.
                page.get_by_role("button", name="维护导览与结构", exact=True).click()
                expect(title).to_have_value("Saved without leaving maintenance")
                fixed = service.workspace(owner, pid, second["id"])
                original_revision = next(
                    entity["entity_revision_id"]
                    for entity in fixed["entities"]
                    if entity["entity_id"] == row["journey_id"]
                )
                title.fill("Unsaved draft belongs to the former default")
                third = KnowledgeService(service).clone(
                    owner, pid, second["id"], "Newest default", "maintenance-default-three"
                )
                expect(page.get_by_label("知识基线", exact=True)).to_have_value("draft:" + second["id"], timeout=10000)
                expect(title).to_have_value("Unsaved draft belongs to the former default")
                with page.expect_response(
                    lambda response: response.request.method == "POST" and response.url.endswith("/revisions")
                ) as saved:
                    page.get_by_role("button", name="保存流程", exact=True).click()
                assert saved.value.ok
                assert saved.value.request.post_data_json["analysis_set_id"] == second["id"]
                assert saved.value.request.headers["if-match"].strip('"') == original_revision
                expect(title).to_have_value("Unsaved draft belongs to the former default")
                newest = service.workspace(owner, pid, third["id"])
                assert (
                    next(entity["name"] for entity in newest["entities"] if entity["entity_id"] == row["journey_id"])
                    == "Saved without leaving maintenance"
                )
            finally:
                browser.close()
    finally:
        service.cancel_job(owner, polling_id)
