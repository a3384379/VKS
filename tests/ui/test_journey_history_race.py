"""Independent final audit boundary regressions, using isolated fixtures only."""

from playwright.sync_api import expect, sync_playwright
from test_frontend_browser import site, chromium_path
from test_journey_navigation import navigation_site as navigation_site, login  # pytest fixture registration


def test_late_journey_list_does_not_restore_step_from_before_second_back(navigation_site):
    s = navigation_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1100))
        try:
            login(page, s)
            reader = page.get_by_label("当前导览", exact=True)
            expect(reader.get_by_role("button", name="核对动作", exact=True)).to_have_attribute("aria-pressed", "true")
            entry_url = page.url
            reader.get_by_role("button", name="阅读资料声明", exact=True).click()
            declaration_url = page.url
            context = reader.get_by_label("当前步骤的声明依据", exact=True)
            details = context.locator("details").filter(has=page.locator("summary").get_by_text(s["document"]["name"], exact=True))
            details.locator("summary").click()
            details.get_by_role("button", name="打开准确来源", exact=True).click()
            expect(page.locator(".document-reader")).to_contain_text("附件导航手册")
            cached = page.request.get(s["url"] + f"/api/v2/projects/{s['pid']}/journeys?analysis_set_id={s['aid']}")
            assert cached.ok
            held = []
            def hold(route):
                held.append(route)
                page.evaluate("window.__auditJourneyHoldCount = (window.__auditJourneyHoldCount || 0) + 1")
            page.route(f"**/api/v2/projects/{s['pid']}/journeys?*", hold)
            with page.expect_request(lambda request: f"/projects/{s['pid']}/journeys?" in request.url):
                page.go_back()
            page.wait_for_function("window.__auditJourneyHoldCount > 0")
            assert page.url == declaration_url and held
            page.go_back()
            assert page.url == entry_url
            for route in held:
                route.fulfill(status=200, json=cached.json())
            expect(reader.get_by_role("button", name="核对动作", exact=True)).to_have_attribute("aria-pressed", "true")
            assert page.url == entry_url
        finally:
            browser.close()
