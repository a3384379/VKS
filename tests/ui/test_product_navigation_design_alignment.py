"""Actual Chromium reproductions for fixed-target product navigation."""

from test_frontend_browser import site as site  # pytest fixture registration

from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import chromium_path
from vks_api.scenarios import ScenarioService


def sign_in(page, url, pid, view):
    page.goto(url + f"/#view={view}&project={pid}")
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()


pass


def test_trajectory_choice_change_discards_in_flight_previous_pair(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    workspace = service.workspace(actor, pid)
    aid = workspace["selected_analysis_set"]["id"]
    page_key = workspace["pages"][0]["stable_key"]
    for index in range(2):
        ScenarioService(service).create(
            actor,
            pid,
            {
                "analysis_set_id": aid,
                "dsl": {
                    "schema_version": "1.0",
                    "scenario_key": f"alignment-trajectory-{index}",
                    "name": f"Alignment trajectory {index}",
                    "environment_ref": "unused-mock",
                    "dataset_ref": "unused-synthetic",
                    "risk_level": "read_only",
                    "timeout_ms": 10000,
                    "steps": [
                        {"step_key": "read", "kind": "goto", "path": "/", "risk": "read_only", "timeout_ms": 3000}
                    ],
                    "explanation_graph": {
                        "projectKey": "charge-demo",
                        "baselineKey": workspace["selected_analysis_set"]["key"],
                        "states": [
                            {
                                "stateKey": f"alignment-state-{index}",
                                "label": f"Alignment state {index}",
                                "pageKey": page_key,
                                "evidenceOrigin": "declared",
                            }
                        ],
                        "transitions": [],
                    },
                },
            },
            f"alignment-trajectory-{index}",
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        held = []
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.route("**/api/v2/projects/*/trajectory-comparison?*", lambda route: held.append(route))
            sign_in(page, url, pid, "workbench")
            left = page.get_by_label("左侧轨迹", exact=True)
            right = page.get_by_label("右侧轨迹", exact=True)
            expect(right.locator("option").nth(1)).to_have_count(1)
            expect(left).not_to_have_value("")
            old_pair = (left.input_value(), right.input_value())
            assert old_pair[0] != old_pair[1]
            page.get_by_role("button", name="比较已保存轨迹", exact=True).click()
            expect(page.get_by_role("button", name="正在比较…", exact=True)).to_be_visible()
            assert held
            changed = right.is_enabled()
            if changed:
                right.select_option(old_pair[0])
            with page.expect_response(lambda response: "/trajectory-comparison?" in response.url):
                for route in held:
                    route.continue_()
                held.clear()
            expect(page.get_by_role("button", name="比较已保存轨迹", exact=True)).to_be_visible()
            if changed:
                expect(right).to_have_value(old_pair[0])
                expect(page.get_by_text("固定比较摘要：", exact=False)).not_to_be_visible()
            else:
                expect(right).to_have_value(old_pair[1])
        finally:
            browser.close()
