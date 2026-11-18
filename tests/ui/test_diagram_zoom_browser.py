"""Real Mermaid pixels remain clear across zoom, resets and fresh rendering."""

from playwright.sync_api import expect, sync_playwright

from tests.ui.diagram_assertions import assert_clear_mermaid_zoom
from tests.ui.test_frontend_browser import chromium_path, site


def test_mermaid_zoom_has_no_offset_shadow_after_resets_and_rerender(site):
    url, pid, _, document = site
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1586, "height": 992})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: print("MEDIA_CONSOLE", message.type, message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: print("MEDIA_REQUEST_FAILED", request.url, request.failure))
        try:
            target = url + f"/#view=documents&project={pid}&entity={document}"
            page.goto(target)
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            first_url = None
            for _ in range(2):
                diagram = page.locator(".document-reader .diagram-block").first
                diagram.scroll_into_view_if_needed()
                assert_clear_mermaid_zoom(diagram)
                current_url = diagram.locator("img").get_attribute("src")
                if first_url is not None:
                    assert current_url != first_url
                first_url = current_url
                expect(page.locator(".diagram-render-frame")).to_have_count(0)
                page.reload()
            assert not errors
        finally:
            browser.close()
