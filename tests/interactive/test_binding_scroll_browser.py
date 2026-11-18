"""Wheel gestures stay with the fixed association card, including its header."""

from copy import deepcopy

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_association_card_keeps_large_reading_area_and_one_scroll_owner(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("if(top===window)localStorage.setItem('vks.studio.auto-associate','off')")

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            payload["data"] = []
            for i in range(25):
                row = deepcopy(original)
                row.update(
                    id=f"scroll-{i}",
                    title=f"模块{i:02}",
                    summary="\n\n".join(
                        f"## 业务段落 {n}\n\n完整的业务说明与条件。" for n in range(80 if i == 0 else 3)
                    ),
                )
                payload["data"].append(row)
            route.fulfill(response=response, json=payload)

        page.route("**/interactive/bindings?*", bindings)
        page.route(
            "**/interactive/reassociate/targets?*",
            lambda r: r.fulfill(json={"data": {"items": [], "next_offset": None}}),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            card = page.get_by_role("article", name="关联详情", exact=True)
            expect(card.get_by_role("heading", name="模块00", exact=True)).to_be_visible()
            expect(page.locator(".binding-inventory-status")).to_be_empty()
            toolbar = page.get_by_role("region", name="AI 预关联", exact=True)
            assert toolbar.bounding_box()["height"] < 100
            settings = toolbar.locator(".batch-settings")
            expect(settings).not_to_have_attribute("open", "")
            initial_height = toolbar.bounding_box()["height"]
            settings.locator("summary").click()
            concurrency = toolbar.get_by_label("分析与关联并发", exact=True)
            expect(concurrency).to_be_visible()
            concurrency.select_option("2")
            expect(settings.locator("summary")).to_contain_text("2 路")
            assert toolbar.bounding_box()["height"] == initial_height
            settings.locator("summary").click()
            assert card.bounding_box()["height"] >= 800
            card.scroll_into_view_if_needed()
            bounds = card.bounding_box()
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 1002
            assert page.locator(".binding-workspace").evaluate("el=>el.scrollWidth===el.clientWidth")
            assert card.evaluate(
                "el=>el.scrollHeight>el.clientHeight && getComputedStyle(el).overscrollBehaviorY==='contain'"
            )
            assert page.locator(".binding-detail-scroll").evaluate("el=>getComputedStyle(el).overflowY==='visible'")
            page.evaluate("""() => {window.scrollMutations=0;
                new MutationObserver(changes=>window.scrollMutations+=changes.length)
                .observe(document.querySelector('.binding-workspace'),{subtree:true,childList:true,characterData:true});} """)
            outer = page.evaluate("document.scrollingElement.scrollTop")
            header = card.locator("header").bounding_box()
            page.mouse.move(header["x"] + 100, header["y"] + 30)
            page.mouse.wheel(0, 220)
            expect(card).to_have_js_property("scrollTop", 220)
            assert page.evaluate("document.scrollingElement.scrollTop") == outer
            page.mouse.move(bounds["x"] + 100, bounds["y"] + bounds["height"] - 60)
            page.mouse.wheel(0, 220)
            expect(card).to_have_js_property("scrollTop", 440)
            card.evaluate("el=>el.scrollTop=el.scrollHeight")
            bottom = card.evaluate("el=>el.scrollTop")
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(200)
            assert abs(card.evaluate("el=>el.scrollTop") - bottom) <= 1
            assert page.evaluate("document.scrollingElement.scrollTop") == outer
            assert page.evaluate("window.scrollMutations") == 0
            assert card.bounding_box() == bounds
            page.get_by_role("button", name="查看关联详情：模块01", exact=True).click()
            assert card.bounding_box() == bounds
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
            assert page.locator(".binding-workspace").evaluate("el=>el.scrollHeight<=el.clientHeight+1")
        finally:
            browser.close()
