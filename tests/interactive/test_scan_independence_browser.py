"""A partial scan remains usable while its earlier fixed AI batch runs."""

from copy import deepcopy

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api.models import Job
from vks_api.prototype_scan import PrototypeScanRun

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.interactive.test_scan_independence import partial_with_batch
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_partial_scan_can_analyse_then_associate_new_targets_while_old_ai_runs(materials, tmp_path, monkeypatch):
    service, _actor, _, pid, aid, _, _, _, run, calls, legacy, payload = partial_with_batch(materials, monkeypatch)
    web = materials[0]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        uploads, resumes, whole_batches, errors = [], [], [], []

        def observe(request):
            if request.method == "PUT" and request.url.endswith("/associations/inventory"):
                uploads.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/resume"):
                resumes.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/associations/batch"):
                whole_batches.append(request.post_data_json)

        page.on("request", observe)
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            panel = page.get_by_role("region", name="AI 预关联", exact=True)
            expect(panel.get_by_role("button", name="重试未完成页面", exact=True)).to_be_enabled()
            expect(panel.get_by_role("button", name="继续扫描分析", exact=True)).to_be_enabled()
            expect(panel.get_by_label("分析与关联并发", exact=True)).to_have_value("3")
            expect(panel).to_contain_text("扫描 3 路 · 关联 3 路")
            panel.get_by_role("button", name="继续扫描分析", exact=True).click()
            expect(panel).to_contain_text("已采集 3 / 3 页", timeout=20000)
            expect(panel.get_by_role("button", name="开始 AI 关联", exact=True)).to_be_enabled()
            assert len(uploads) == 2 and not resumes[0]["pipeline"] and not whole_batches
            with service.db() as s:
                jobs = list(s.scalars(select(Job).where(Job.kind == "associate_all")))
                assert len(jobs) == 1 and jobs[0].state == "running"
                assert not s.get(PrototypeScanRun, run["id"]).plan["pipeline"]
            panel.get_by_role("button", name="开始 AI 关联", exact=True).click()
            expect(panel.get_by_label("后台 AI 关联进度", exact=True)).to_be_visible()
            page.wait_for_function(
                "document.querySelector('[aria-label=\"后台 AI 关联进度\"]')?.textContent.includes('排队')"
            )
            assert len(resumes) == 2 and resumes[1]["pipeline"]
            expect(panel.get_by_text("正在预关联", exact=True)).to_have_count(0)
            expect(panel.get_by_role("button", name="停止当前关联", exact=True)).to_be_enabled()
            assert len(uploads) == 2 and not whole_batches and not calls
            with service.db() as s:
                jobs = list(s.scalars(select(Job).where(Job.kind == "associate_all")))
                assert len(jobs) > 1
                assert deepcopy(s.get(Job, legacy["id"]).input_json) == payload
                old_ids = {item["id"] for item in payload["items"]}
                assert all(
                    old_ids.isdisjoint(item["id"] for item in job.input_json["items"])
                    for job in jobs
                    if job.id != legacy["id"]
                )
            assert not errors
        finally:
            browser.close()


def test_legacy_association_can_stop_while_scan_acknowledgement_is_pending(materials, tmp_path, monkeypatch):
    service, _actor, _, pid, aid, _, _, _, _, calls, legacy, _ = partial_with_batch(materials, monkeypatch)
    web = materials[0]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        held = []

        def hold_inventory(route):
            if route.request.method == "PUT":
                held.append(route)
            else:
                route.continue_()

        page.route("**/associations/inventory", hold_inventory)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            panel = page.get_by_role("region", name="AI 预关联", exact=True)
            panel.get_by_role("button", name="继续扫描分析", exact=True).click()
            expect(panel.get_by_role("button", name="暂停扫描", exact=True)).to_be_visible()
            stop = panel.get_by_role("button", name="停止任务", exact=True).last
            expect(stop).to_be_enabled()
            with page.expect_response(
                lambda response: response.url.endswith(f"/jobs/{legacy['id']}/cancel")
            ) as cancelled:
                stop.click()
            assert cancelled.value.ok
            with service.db() as s:
                assert s.get(Job, legacy["id"]).cancel_requested
            page.unroute("**/associations/inventory", hold_inventory)
            for route in held:
                route.continue_()
            expect(panel).to_contain_text("已采集 3 / 3 页", timeout=20000)
            assert not calls
        finally:
            browser.close()
