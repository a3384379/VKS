"""The reported scan → documents → AI settings → return → refresh journey."""

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_bulk_association, test_materials
from tests.interactive import test_runtime_inventory_browser as browser_inventory
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.interactive.test_scan_pipeline_audit import jobs as pipeline_jobs
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_three_route_pipeline_survives_navigation_and_refresh_without_duplicate_pages_or_model_jobs(
    materials, tmp_path, monkeypatch
):
    service, owner, manager, pid, aid, initial, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    pages = []
    for name in ("first", "second", "third"):
        pages.append(
            (
                name + ".html",
                f"""<!doctype html><title>收费项 {name}</title>
        <div id="app" aria-busy="true" style="min-height:20px">正在加载</div>
        <script>setTimeout(()=>{{const app=document.querySelector('#app');
        app.innerHTML='<h1>收费项 {name}</h1><button id="action">新增收费项</button>';
        app.setAttribute('aria-busy','false');}},1600)</script>""",
            )
        )
    prototype = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        zip_docs(pages),
        package_id=initial["package_id"],
        filename="navigation.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype])
    with native_studio(materials[0].app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            uploads, batches = [], []

            def observe(request):
                if request.method == "PUT" and request.url.endswith("/associations/inventory"):
                    uploads.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith("/associations/batch"):
                    batches.append(request.post_data_json)

            page.on("request", observe)
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.endswith("/associations/inventory")
                    and response.status == 200
                )
            ):
                browser_inventory.start_legacy_pipeline(page, service, owner, pid, aid, concurrency=1)
            endpoint = f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}"
            before = page.request.get(endpoint).json()["data"]["run"]
            assert before["pages_completed"] >= 1 and before["pages_total"] == 3
            assert before["pipeline"] and before["ai"]["targets"] == 2
            admitted = pipeline_jobs(service, before["id"])
            assert len(admitted) == 2 and not batches
            # Actually finish page one's synthetic AI while scanning continues.
            # Its new bindings advance the analysis counter, not source identity.
            for jid, _ in admitted:
                service.process_job(jid)
                assert service.get_job(owner, jid)["state"] == "succeeded"
            assert len(calls) == 2
            page.get_by_role("button", name="文档", exact=True).click()
            expect(page.locator(".document-reader").get_by_role("heading", name="收费项", level=1, exact=True)).to_be_visible()
            page.get_by_role("button", name="AI 设置", exact=True).click()
            page.get_by_role("button", name="关联管理", exact=True).click()
            returned = page.request.get(endpoint).json()["data"]["run"]
            assert returned["id"] == before["id"] and returned["pages_completed"] >= before["pages_completed"]
            assert returned["pages_completed"] < 3, (
                "Refresh must exercise an unfinished scan, not an already queued batch"
            )
            remaining = {entry["route"] for pack in returned["manifest"]["packages"] for entry in pack["pages"]}
            captured_before_refresh = {"/first.html", "/second.html", "/third.html"} - remaining
            upload_boundary = len(uploads)
            page.reload()
            saved = page.get_by_label("已保存的预扫描进度", exact=True)
            expect(saved).to_be_visible()
            restored = page.request.get(endpoint).json()["data"]["run"]
            assert restored["id"] == before["id"] and restored["pages_completed"] >= returned["pages_completed"]
            expect(saved).to_contain_text(f"{restored['pages_completed']} / 3 页")
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
            assert len(calls) == 2 and not batches
            page.screenshot(path=tmp_path / "scan-progress-after-navigation-refresh.png")
            page.get_by_role("button", name="恢复预扫描", exact=True).click()
            expect(saved.get_by_text("页面预扫描完成", exact=True)).to_be_visible(timeout=20000)
            done = page.request.get(endpoint).json()["data"]["run"]
            assert done["id"] == before["id"] and done["pages_completed"] == 3
            assert len({item["route"] for item in uploads}) == 3
            assert not captured_before_refresh.intersection(item["route"] for item in uploads[upload_boundary:])
            assert not batches and len(pipeline_jobs(service, done["id"])) == 6
            page.reload()
            expect(saved.get_by_text("页面预扫描完成", exact=True)).to_be_visible()
            assert not batches and len(calls) == 2 and len(pipeline_jobs(service, done["id"])) == 6
        finally:
            browser.close()


def test_partial_pipeline_refresh_keeps_failures_and_only_admits_captured_page_controls(
    materials, tmp_path, monkeypatch
):
    ordinary = browser_inventory.dynamic_page
    monkeypatch.setattr(
        browser_inventory,
        "dynamic_page",
        lambda name, *args, **kwargs: (
            ordinary(name, *args, **kwargs)
            if name.endswith("A")
            else '<!doctype html><title>Unready page</title><div id="app" aria-busy="true" style="min-height:24px">正在加载</div>'
        ),
    )
    web, service, owner, pid, aid, calls = browser_inventory.setup(materials, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            batches = []
            page.on(
                "request",
                lambda request: (
                    batches.append(request.post_data_json)
                    if request.method == "POST" and request.url.endswith("/associations/batch")
                    else None
                ),
            )
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            browser_inventory.start_legacy_pipeline(page, service, owner, pid, aid)
            saved = page.get_by_label("已保存的预扫描进度", exact=True)
            expect(saved.get_by_text("预扫描部分完成", exact=True)).to_be_visible(timeout=20000)
            expect(saved).to_contain_text("1 / 2 页")
            expect(saved).to_contain_text("1 页待重试")
            endpoint = f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}"
            current = page.request.get(endpoint).json()["data"]["run"]
            admitted = pipeline_jobs(service, current["id"])
            assert (
                current["pipeline"]
                and current["ai"]["targets"] == sum(len(payload["items"]) for _, payload in admitted) >= 1
            )
            assert not batches and not calls
            assert all(item["target"]["route"] == "/a.html" for _, payload in admitted for item in payload["items"]), (
                "a permanently busy page must not send static or unseen controls to AI"
            )
            expected_calls = len(admitted[0][1]["groups"])
            service.process_job(admitted[0][0])
            assert service.get_job(owner, admitted[0][0])["state"] == "succeeded" and len(calls) == expected_calls
            page.reload()
            page.locator(".batch-task-details > summary").click()
            expect(saved.get_by_text("预扫描部分完成", exact=True)).to_be_visible()
            expect(saved).to_contain_text("1 / 2 页")
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
            assert not batches and len(calls) == expected_calls
            assert len(pipeline_jobs(service, current["id"])) == len(admitted)
            expect(page.get_by_role("button", name="重试未完成页面", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="仅关联已采集内容", exact=True)).to_have_count(0)
        finally:
            browser.close()


def test_three_parallel_workers_have_distinct_storage_origins_and_stop_before_late_uploads(
    materials, tmp_path, monkeypatch
):
    service, owner, manager, pid, aid, initial, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    source = zip_docs(
        [(name + ".html", browser_inventory.dynamic_page("账单 " + name, 6500)) for name in ("a", "b", "c")]
    )
    replacement = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        source,
        package_id=initial["package_id"],
        filename="isolated-workers.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [replacement])
    with native_studio(materials[0].app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("Object.defineProperty(navigator,'hardwareConcurrency',{value:8})")
            uploads = []
            page.on(
                "request",
                lambda request: (
                    uploads.append(request.post_data_json)
                    if request.method == "PUT" and request.url.endswith("/associations/inventory")
                    else None
                ),
            )
            login(page, url, pid, aid)
            page.evaluate("localStorage.setItem('audit-worker-isolation','USER')")
            page.get_by_role("button", name="关联管理", exact=True).click()
            browser_inventory.start_legacy_pipeline(page, service, owner, pid, aid)
            frames = page.locator('iframe[title="预扫描原型"]')
            expect(frames).to_have_count(3)
            page.wait_for_function(
                r"""() => {const nodes=[...document.querySelectorAll('iframe[title="预扫描原型"]')];
                return nodes.length===3 && nodes.every(node=>/^http:\/\/vks-/.test(node.src));}"""
            )
            origins = frames.evaluate_all("nodes=>nodes.map(node=>new URL(node.src).origin)")
            assert len(set(origins)) == 3
            runtime_frames = [frames.nth(index).element_handle().content_frame() for index in range(3)]
            for index, origin in enumerate(origins):
                frame = runtime_frames[index]
                frame.wait_for_url(origin + "/**", timeout=5000)
                assert frame.evaluate("localStorage.getItem('audit-worker-isolation')") is None
                frame.evaluate("value=>localStorage.setItem('audit-worker-isolation',value)", str(index))
            for index, origin in enumerate(origins):
                frame = runtime_frames[index]
                assert frame.evaluate("localStorage.getItem('audit-worker-isolation')") == str(index)
            assert page.evaluate("localStorage.getItem('audit-worker-isolation')") == "USER"
            assert not uploads
            page.get_by_label("动态原型预扫描", exact=True).get_by_role("button", name="停止任务", exact=True).click()
            expect(frames).to_have_count(0)
            saved = page.get_by_label("已保存的预扫描进度", exact=True)
            expect(saved).to_contain_text("任务已停止")
            if page.locator(".batch-task-details").get_attribute("open") is None:
                page.locator(".batch-task-details > summary").click()
            expect(saved.get_by_text("任务已停止", exact=True)).to_be_visible()
            page.wait_for_timeout(4700)
            run = page.request.get(f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}").json()[
                "data"
            ]["run"]
            assert run["state"] == "cancelled" and run["pages_completed"] == 0
            assert not uploads and not calls and not pipeline_jobs(service, run["id"])
        finally:
            browser.close()
