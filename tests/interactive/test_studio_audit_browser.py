"""Browser regressions for bounded document caches and asynchronous project settings."""

from playwright.sync_api import expect, sync_playwright
from vks_api.model_connections import ConnectionService

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials
workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_selected_article_survives_document_cache_eviction(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = zip_docs(
        [
            (f"article-{i:02}.md", f"# Article {i:02}\n\nBody {i:02}.\n\n## Section {i:02}\n\nDetails.")
            for i in range(66)
        ]
    )
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="articles.zip")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            login(page, url, pid, aid)
            article = page.locator(".document-reader")
            sidebar = page.locator(".doc-sidebar")
            expect(article).to_contain_text("Body 00.")
            for i in range(1, 66):
                sidebar.get_by_role("button", name=f"展开章节 Article {i:02}", exact=True).click()
                expect(sidebar.get_by_role("button", name=f"Section {i:02}", exact=True)).to_be_visible()
            # Search is owned by App, causing the reader to render after FIFO eviction.
            sidebar.get_by_label("搜索文档", exact=True).fill("Article")
            expect(article).to_contain_text("Body 00.", timeout=2000)
            expect(article.get_by_text("正在读取正文…", exact=True)).to_have_count(0)
        finally:
            browser.close()


def test_model_save_freezes_edited_fields_until_server_result(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _), _body, _calls = assistant
    monkeypatch.setattr(ConnectionService, "_request", lambda *args: {"data": [{"id": "synthetic"}]})
    held = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            login(page, url, pid, aid)
            page.get_by_role("button", name="AI 设置", exact=True).click()
            key = page.get_by_label("API Key", exact=True)
            expect(key).to_be_visible()
            key.fill("synthetic-audit-key-not-real")

            def hold_save(route):
                if route.request.method == "POST":
                    held.append((route, route.fetch()))
                else:
                    route.continue_()

            page.route("**/api/v1/projects/" + pid + "/model-connections", hold_save)
            page.get_by_role("button", name="保存并使用", exact=True).click()
            expect(page.get_by_role("button", name="正在保存…", exact=True)).to_be_visible()
            expect(key).to_be_disabled(timeout=2000)
            expect(page.get_by_label("回答模型", exact=True)).to_be_disabled()
            expect(page.get_by_role("button", name="DeepSeek", exact=True)).to_be_disabled()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(20)
            assert held
            route, response = held.pop()
            route.fulfill(response=response)
            expect(page.get_by_text("已保留密钥", exact=True)).to_be_visible()
            expect(key).to_have_value("")
        finally:
            for route, response in held:
                route.fulfill(response=response)
            browser.close()


def test_project_search_drops_late_results_from_previous_query(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, _grant, _package, _section, _), _body, _calls = assistant
    old = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            login(page, url, pid, aid)
            page.get_by_role("button", name="项目中心", exact=True).click()
            query = page.get_by_label("搜索项目", exact=True)

            def results(route):
                from urllib.parse import parse_qs, urlparse

                params = parse_qs(urlparse(route.request.url).query)
                if params.get("limit") != ["30"]:
                    route.continue_()
                    return
                q = params.get("q", [""])[0]
                if q == "Earlier":
                    old.append(route)
                    return
                route.fulfill(
                    json={
                        "data": [
                            {
                                "id": "new-project",
                                "key": "new-project",
                                "name": "Latest result",
                                "description": "",
                                "status": "active",
                                "row_version": 1,
                            }
                        ],
                        "meta": {},
                    }
                )

            page.route("**/api/v1/projects?*", results)
            with page.expect_request(lambda r: "q=Earlier" in r.url):
                query.fill("Earlier")
            query.fill("Latest")
            expect(page.locator(".project-card h2")).to_have_text(["Latest result"])
            for route in old:
                route.fulfill(
                    json={
                        "data": [
                            {
                                "id": "old-project",
                                "key": "old-project",
                                "name": "Earlier result",
                                "description": "",
                                "status": "active",
                                "row_version": 1,
                            }
                        ],
                        "meta": {},
                    }
                )
            expect(page.locator(".project-card h2")).to_have_text(["Latest result"])
        finally:
            browser.close()


def test_rag_probe_does_not_continue_after_switching_version(assistant, tmp_path, monkeypatch):
    (web, service, owner, pid, aid, _grant, _package, _section, _), _body, _calls = assistant
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "Audit reader snapshot")
    monkeypatch.setattr(ConnectionService, "_request", lambda *args: {"data": [{"id": "synthetic"}]})
    held, test_calls = [], []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            login(page, url, pid, aid)
            page.get_by_role("button", name="AI 设置", exact=True).click()
            panel = page.get_by_role("region", name="RAG 向量知识库")
            expect(panel.get_by_role("button", name="测试嵌入连接", exact=True)).to_be_enabled()

            def intercept(route):
                if route.request.url.endswith("/settings") and route.request.method == "POST":
                    held.append(route)
                elif route.request.url.endswith("/test"):
                    test_calls.append(route.request.url)
                    route.fulfill(json={"data": {"dimensions": 512, "elapsed_ms": 1}})
                else:
                    route.continue_()

            page.route("**/interactive/rag/*", intercept)
            with page.expect_request(lambda r: r.url.endswith("/rag/settings") and r.method == "POST"):
                panel.get_by_role("button", name="测试嵌入连接", exact=True).click()
            page.evaluate(
                "release => { const p=new URLSearchParams(location.hash.slice(1));p.set('release',release);p.delete('analysis');location.hash=p.toString(); }",
                release["id"],
            )
            expect(panel).to_contain_text("当前为只读快照")
            assert held
            held.pop().fulfill(json={"data": {}})
            with page.expect_response(lambda r: r.url.endswith("/rag/documents/status")):
                panel.get_by_role("button", name="刷新状态", exact=True).click()
            assert not test_calls
            expect(panel).not_to_contain_text("嵌入连接验证成功")
        finally:
            for route in held:
                route.fulfill(json={"data": {}})
            browser.close()
