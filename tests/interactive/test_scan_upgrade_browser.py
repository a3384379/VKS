"""A completed pre-pipeline inventory upgrades through explicit user intent."""

from playwright.sync_api import expect, sync_playwright
from vks_api.models import Analysis

from tests.interactive import test_materials, test_scan_runs
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_completed_legacy_scan_uses_origin_version_to_upgrade_without_rescanning(materials, tmp_path, monkeypatch):
    web, service, actor, pid, aid, calls, scans, bulk, run = test_scan_runs.start(materials, monkeypatch)
    test_scan_runs.save(bulk, actor, pid, run, 0)
    complete = test_scan_runs.save(bulk, actor, pid, run, 1)["scan_run"]
    assert complete["state"] == "completed" and not complete["pipeline"]
    # Knowledge changes advance the mutable selection counter without changing
    # the exact prototype/document composition captured by this legacy scan.
    with service.db() as db:
        db.get(Analysis, aid).selection_version += 1
    uploads, resumes, whole_batches = [], [], []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        page.on(
            "request",
            lambda request: (
                uploads.append(request.url)
                if request.method == "PUT" and request.url.endswith("/associations/inventory")
                else None
            ),
        )
        page.on(
            "request",
            lambda request: (
                resumes.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith(f"/scans/{run['id']}/resume")
                else None
            ),
        )
        page.on(
            "request",
            lambda request: (
                whole_batches.append(request.url)
                if request.method == "POST" and request.url.endswith("/associations/batch")
                else None
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.locator(".batch-task-details > summary").click()
            page.get_by_role("button", name="开始 AI 关联", exact=True).click()
            expect(page.get_by_label("后台 AI 关联进度", exact=True)).to_contain_text("排队")
            assert resumes and resumes[0]["pipeline"] is True
            assert not uploads and not whole_batches and not calls
            current = scans.status(actor, pid, aid)["run"]
            assert current["id"] == run["id"] and current["pipeline"]
            assert current["pages_completed"] == 2 and current["ai"]["queued"] > 0
        finally:
            browser.close()
