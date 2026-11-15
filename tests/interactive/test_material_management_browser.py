"""Materials remain usable while history loads; imports and recycle actions work in Studio."""

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_materials_progressive_loading_deduplication_and_recycle_ui(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = b"# Guide\n\nExact original knowledge."
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", raw, name="Guide", filename="guide.md"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    manager.checkpoint(owner, pid, aid, "Saved original", "browser-snapshot")
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1100})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            errors, history, gets = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: (
                    gets.append(request.url) if request.method == "GET" and "/materials?" in request.url else None
                ),
            )
            page.route("**/materials/versions?*", lambda route: history.append(route))
            login(page, url, pid, aid)
            page.get_by_role("button", name="资料管理", exact=True).click()
            card = page.locator(".material-card").filter(has=page.get_by_role("heading", name="Guide", exact=True))
            expect(card).to_be_visible(timeout=10000)
            assert history
            expect(page.get_by_text("正在读取历史版本，可先管理资料包…", exact=True)).to_be_visible()
            expect(card.get_by_role("button", name="更新资料", exact=True)).to_be_enabled()
            expect(card.get_by_role("button", name="删除", exact=True)).to_be_disabled()
            assert all("include_versions=false" in request for request in gets)
            for route in history:
                route.fulfill(response=route.fetch())
            page.unroute("**/materials/versions?*")
            row = page.locator(".material-history article").filter(has_text="Saved original")
            expect(row).to_be_visible()
            row.get_by_role("button", name="删除", exact=False).click()
            expect(row).to_have_count(0)
            recycle = page.locator("details").filter(has=page.locator("summary").filter(has_text="回收站"))
            recycle.locator("summary").click()
            removed = recycle.locator("article").filter(has_text="Saved original")
            expect(removed).to_be_visible()
            removed.get_by_role("button", name="从回收站恢复 Saved original", exact=True).click()
            expect(row).to_be_visible()
            expect(removed).to_have_count(0)

            # Reimporting unchanged content is an immediate no-op, and does not
            # perform a full list fetch before uploading or stage another copy.
            before = len(gets)
            card.get_by_role("button", name="更新资料", exact=True).click()
            picker = page.get_by_label("选择资料文件", exact=True)
            picker.set_input_files({"name": "guide.md", "mimeType": "text/markdown", "buffer": raw})
            page.get_by_role("button", name="解析并预览", exact=True).click()
            expect(
                page.get_by_text("资料内容未变化，已保留当前版本，没有重复导入或重建知识库。", exact=True)
            ).to_be_visible()
            expect(page.locator(".material-staging")).to_have_count(0)
            assert len(gets) == before + 1
            page.get_by_label("导入方式", exact=True).select_option("")
            picker.set_input_files({"name": "copy.md", "mimeType": "text/markdown", "buffer": raw})
            page.get_by_role("button", name="解析并预览", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("相同资料已存在")
            expect(page.locator(".material-card")).to_have_count(1)
            # The overlap response preserves the selected file and offers an
            # explicit switch to the existing logical package, then updates it.
            picker.set_input_files(
                {"name": "new-guide.md", "mimeType": "text/markdown", "buffer": raw + b"\nUpdated source."}
            )
            expect(page.get_by_label("资料包名称", exact=True)).to_have_value("new-guide")
            page.route(
                "**/materials/prepare",
                lambda route: route.fulfill(
                    status=409,
                    json={
                        "error": {
                            "code": "VKS_MATERIAL_UPDATE_REQUIRED",
                            "message": "已有手册，请更新已有资料包",
                            "details": {"package_id": package["package_id"], "name": "Guide"},
                        }
                    },
                ),
            )
            page.get_by_role("button", name="解析并预览", exact=True).click()
            page.get_by_role("button", name="改为更新：Guide", exact=True).click()
            expect(page.get_by_label("导入方式", exact=True)).to_have_value(package["package_id"])
            assert picker.evaluate("input => input.files.length") == 1
            page.unroute("**/materials/prepare")
            page.get_by_role("button", name="解析并预览", exact=True).click()
            expect(page.locator(".material-staging")).to_contain_text("1 篇文档")
            expect(page.locator(".material-card")).to_have_count(1)
            assert not errors, errors
        finally:
            browser.close()


def test_double_submit_import_only_prepares_one_package(materials, tmp_path, monkeypatch):
    web, _service, _owner, _manager, projects = materials
    pid, aid = projects[0]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            held = []
            login(page, url, pid, aid)
            page.get_by_role("button", name="资料管理", exact=True).click()
            page.get_by_label("导入方式", exact=True).select_option("")
            page.get_by_label("选择资料文件", exact=True).set_input_files(
                {"name": "new.html", "mimeType": "text/html", "buffer": b"<title>New</title><button>Open</button>"}
            )
            page.route("**/materials/prepare", lambda route: held.append(route))
            page.locator("form.material-import-form").evaluate(
                "form => { form.requestSubmit(); form.requestSubmit(); }"
            )
            expect(page.get_by_text("正在解析资料并准备版本…", exact=True)).to_be_visible()
            page.wait_for_timeout(200)
            assert len(held) == 1
            held[0].fulfill(response=held[0].fetch())
            expect(page.locator(".material-staging article")).to_have_count(1)
            expect(page.locator(".material-card")).to_have_count(1)
        finally:
            browser.close()


def test_current_migration_and_disabled_retry_are_separate_from_history(materials, tmp_path, monkeypatch):
    from vks_api.models import Job

    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Guide\n\nExact source.")
    activated = test_materials.activate(service, owner, manager, pid, aid, [package])
    with service.db() as db:
        job = db.get(Job, activated["job_id"])
        job.state, job.progress, job.stage = "running", 27, "ASSOCIATE"
        job.result_json = {
            "total": 11,
            "saved": 3,
            "completed": {str(i): {"label": str(i), "saved": True, "message": "Done"} for i in range(3)},
        }
        service._new_job(
            db, owner, pid, "material_migrate", {"analysis_set_id": aid, "composition": "old-version", "targets": []}
        )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1100})
            login(page, url, pid, aid)
            page.get_by_role("button", name="资料管理", exact=True).click()
            current = page.locator(".material-job[data-current-scope=true]")
            expect(current).to_have_count(1)
            expect(current).to_contain_text("正在核对关联")
            expect(current).to_contain_text("已处理 3 / 11")
            expect(page.get_by_role("button", name="迁移进行中", exact=True)).to_be_disabled()
            history = page.locator(".material-job-history")
            expect(history).to_contain_text("迁移历史（1）")
            expect(history.locator(".material-job")).not_to_be_visible()
            history.locator("summary").click()
            expect(history).to_contain_text("旧资料版本")
        finally:
            browser.close()
