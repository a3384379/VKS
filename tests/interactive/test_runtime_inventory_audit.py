"""Negative controls for isolated default-page inventory collection."""

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def install(materials, html):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(service, owner, manager, pid, aid, "prototype", html, filename="index.html")
    document = test_materials.stage(service, owner, manager, pid, aid, "documents", "# 收费说明\n\n维护收费项目。")
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    return web, pid, aid


def test_scanning_current_single_html_never_invokes_source_named_navigation_api(materials, tmp_path, monkeypatch):
    web, pid, aid = install(
        materials,
        """<!doctype html><title>Ordinary prototype</title><h1>Default page</h1>
        <button id="business">业务保存</button><script>
        document.querySelector('#business').onclick=()=>top.postMessage({type:'fixture.business-clicked'},'*');
        window.__prototypePack={show:()=>document.querySelector('#business').click()};
        </script>""",
    )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(
                "window.businessClicks=0;addEventListener('message',event=>{if(event.data?.type==='fixture.business-clicked')businessClicks++});localStorage.setItem('vks.studio.auto-associate','off')"
            )
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
            page.wait_for_function("!document.querySelector('[aria-label=\"动态原型预扫描\"]')", timeout=18000)
            assert page.evaluate("businessClicks") == 0, (
                "A source global name is not an owned page-navigation capability"
            )
        finally:
            browser.close()


def test_ambiguous_child_frame_identities_are_never_reported_as_complete(materials, tmp_path, monkeypatch):
    web, pid, aid = install(
        materials,
        """<!doctype html><title>Nested page</title><h1>Default page</h1><button id="parent">Parent</button>
        <iframe srcdoc="<button id='first'>First child</button>"></iframe>
        <iframe srcdoc="<button id='second'>Second child</button>"></iframe>""",
    )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
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
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
            page.wait_for_function("!document.querySelector('[aria-label=\"动态原型预扫描\"]')", timeout=18000)
            assert len(uploads) == 1
            capture = uploads[0]
            assert capture["status"] == "failed" or capture["unverified_frames"] > 0, (
                "Indistinguishable child frames must remain unverified instead of silently dropping a child's controls"
            )
        finally:
            browser.close()


def test_prescan_has_separate_persistent_origin_and_blocks_source_network(materials, tmp_path, monkeypatch):
    web, pid, aid = install(
        materials,
        """<!doctype html><title>Storage fixture</title><h1>Storage default page</h1><button id="action">维护</button>
        <script>
        top.postMessage({type:'fixture.storage-observation',value:localStorage.getItem('inventory-marker')||'empty'},'*');
        localStorage.setItem('inventory-marker','SOURCE_INITIALIZATION');
        fetch('https://network.invalid/fetch').catch(()=>{});
        const image=new Image();image.src='https://network.invalid/image';document.body.append(image);
        const nested=document.createElement('iframe');nested.src='https://network.invalid/frame';document.body.append(nested);
        </script>""",
    )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            network = []
            page.route("https://network.invalid/**", lambda route: (network.append(route.request.url), route.abort()))
            page.add_init_script(
                "window.observedStores=[];addEventListener('message',event=>{if(event.data?.type==='fixture.storage-observation')observedStores.push({origin:event.origin,value:event.data.value})});localStorage.setItem('vks.studio.auto-associate','off')"
            )
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            original = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(original.get_by_role("heading", name="Storage default page", exact=True)).to_be_visible()
            original.locator("body").evaluate("() => localStorage.setItem('inventory-marker','CURRENT_USER_SETTING')")
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
            page.wait_for_function("!document.querySelector('[aria-label=\"动态原型预扫描\"]')", timeout=18000)
            observations = page.evaluate("observedStores")
            assert len(observations) == 2 and {item["value"] for item in observations} == {"empty"}
            assert observations[0]["origin"] != observations[1]["origin"]
            assert (
                original.locator("body").evaluate("() => localStorage.getItem('inventory-marker')")
                == "CURRENT_USER_SETTING"
            )
            assert not network, "CSP must block fetch, images and frame navigation before any external request"
        finally:
            browser.close()
