"""Original materials start independently and only mount after their first visit."""

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import NATIVE_HTML, login, native_studio, source_frame
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_native_reading_does_not_wait_for_links_or_download_unvisited_prototype(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>Prototype</title><input aria-label="Prototype memo"><button id="add">新增收费项</button>',
        filename="prototype.html",
    )
    documents = test_materials.stage(service, owner, manager, pid, aid, "documents", NATIVE_HTML, filename="guide.html")
    test_materials.activate(service, owner, manager, pid, aid, [prototype, documents])
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
            requests, held = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.route("**/interactive/bindings?*", lambda route: held.append(route))
            login(page, url, pid, aid)
            native = source_frame(page)
            expect(native.get_by_text("Original entry home", exact=True)).to_be_visible(timeout=15000)
            assert held, "Links must still be in flight while the original document is usable"
            prototype_path = f"/packages/{prototype['artifact_id']}/content"
            document_path = f"/packages/{documents['artifact_id']}/content"
            assert not any(prototype_path in request for request in requests)
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            native.get_by_label("Original note", exact=True).fill("Preserved native state")

            # Visiting the prototype loads it once; subsequent view switches
            # preserve both original runtimes and their authored form state.
            page.get_by_role("button", name="关联知识", exact=True).click()
            page.get_by_role("button", name="助手选项", exact=True).click()
            manual = page.get_by_role("button", name="手动补充关联", exact=True)
            expect(manual).to_be_disabled()
            expect(page.get_by_label("知识标题", exact=True)).to_have_count(0)
            expect(native.get_by_label("Original note", exact=True)).to_have_value("Preserved native state")
            for route in held:
                route.fulfill(response=route.fetch())
            page.unroute("**/interactive/bindings?*")
            expect(manual).to_be_enabled()
            page.get_by_label("自动关联模块与控件", exact=True).check()
            page.route(
                "**/interactive/related",
                lambda route: route.fulfill(status=503, json={"error": {"message": "Unexpected automatic lookup"}}),
            )
            manual.click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.get_by_label("Prototype memo", exact=True)).to_be_visible(timeout=15000)
            source.get_by_label("Prototype memo", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.get_by_label("知识标题", exact=True)).to_have_value("Prototype memo")
            assert not any("/interactive/related" in request for request in requests)
            panel.get_by_role("button", name="取消", exact=True).click()
            source.get_by_label("Prototype memo", exact=True).fill("Preserved prototype state")

            # Once links are ready, selecting an existing control retains the
            # saved identity and wording instead of opening a blank new link.
            page.get_by_role("button", name="助手选项", exact=True).click()
            page.get_by_role("button", name="手动补充关联", exact=True).click()
            source.get_by_role("button", name="新增收费项", exact=True).click()
            expect(panel.get_by_label("知识标题", exact=True)).to_have_value("新增收费项说明")
            expect(panel.get_by_label("业务说明", exact=True)).to_have_value("必须保留的人工说明")
            expect(panel.get_by_label("关联文档", exact=True)).not_to_have_value("")
            expect(panel.get_by_label("关联章节", exact=True)).not_to_have_value("")
            panel.get_by_label("业务说明", exact=True).fill("等待关联加载后更新人工说明")
            with page.expect_response(
                lambda response: "/interactive/bindings" in response.url and response.request.method == "POST"
            ) as saved:
                panel.get_by_role("button", name="保存关联", exact=True).click()
            assert saved.value.status == 200, saved.value.text()
            expect(panel.get_by_label("知识标题", exact=True)).to_have_count(0)
            page.get_by_role("button", name="文档", exact=True).click()
            expect(native.get_by_label("Original note", exact=True)).to_have_value("Preserved native state")
            page.get_by_role("button", name="原型", exact=True).click()
            expect(source.get_by_label("Prototype memo", exact=True)).to_have_value("Preserved prototype state")
            assert sum(prototype_path in request for request in requests) == 1
            assert sum(document_path in request for request in requests) == 1
        finally:
            browser.close()


def test_implicit_prototype_entry_is_retained_after_leaving_view(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>Default</title><input aria-label="Default memo">',
        filename="prototype.html",
    )
    document = test_materials.stage(service, owner, manager, pid, aid, "documents", NATIVE_HTML, filename="guide.html")
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("if(window.top===window)localStorage.setItem('vks.studio.auto-associate','off')")
            requests = []
            held = []
            page.on("request", lambda request: requests.append(request.url))
            page.route("**/interactive/bindings?*", lambda route: held.append(route))
            page.goto(url + f"/#project={pid}&analysis={aid}")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.get_by_label("Default memo", exact=True)).to_be_visible(timeout=15000)
            expect(page.locator('iframe[title="原 HTML 手册"]')).to_have_count(0)
            assert not any(f"/packages/{document['artifact_id']}/content" in request for request in requests)
            source.get_by_label("Default memo", exact=True).fill("Keep default entry state")
            page.get_by_role("button", name="关联管理", exact=True).click()
            expect(page.get_by_role("heading", name="正在加载关联…", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="选择控件并关联知识", exact=True)).to_be_disabled()
            page.get_by_role("button", name="原型", exact=True).click()
            source.get_by_label("Default memo", exact=True).click(button="right")
            menu = page.get_by_role("menu", name="控件知识菜单", exact=True)
            expect(menu.get_by_role("menuitem", name="手动补充关联", exact=True)).to_be_disabled()
            expect(menu.get_by_role("menuitem", name="查看关联文档", exact=True)).to_be_enabled()
            expect(menu.get_by_role("menuitem", name="AI 讲解此功能", exact=True)).to_be_enabled()
            assert held
            for route in held:
                route.fulfill(response=route.fetch())
            page.unroute("**/interactive/bindings?*")
            expect(menu.get_by_role("menuitem", name="手动补充关联", exact=True)).to_be_enabled()
            menu.get_by_role("menuitem", name="手动补充关联", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            expect(panel.get_by_label("知识标题", exact=True)).to_have_value("Default memo")
            panel.get_by_role("button", name="取消", exact=True).click()
            page.get_by_role("button", name="文档", exact=True).click()
            expect(source_frame(page).get_by_text("Original entry home", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="原型", exact=True).click()
            expect(source.get_by_label("Default memo", exact=True)).to_have_value("Keep default entry state")
            assert sum(f"/packages/{prototype['artifact_id']}/content" in request for request in requests) == 1
        finally:
            browser.close()
