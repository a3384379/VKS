
from test_frontend_browser import site as site  # pytest fixture registration
from playwright.sync_api import expect, sync_playwright
from test_frontend_browser import chromium_path


def login(page, url, pid, view="versions"):
    page.goto(url + f"/#view={view}&project={pid}")
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()


def test_wizard_creates_parallel_evidence_navigation_without_order_claims_or_model(site):
    url, pid, _, _ = site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login(page, url, pid, "imports")
            page.locator(".v2-steps button").nth(4).click()
            flow = page.get_by_role("region", name="向导动作证据导航")
            expect(flow).to_be_visible()
            controls = flow.locator(".v2-action-options input[type=checkbox]")
            controls.nth(0).check()
            controls.nth(1).check()
            expect(flow.locator(".v2-evidence-actions li")).to_have_count(2)
            expect(flow.get_by_role("button", name="上移", exact=False)).to_have_count(0)
            expect(flow).to_contain_text("菜单排列和选择顺序不代表业务先后")
            with page.expect_response(lambda r: "/knowledge-tasks" in r.url and r.request.method == "POST") as created:
                flow.get_by_role("button", name="生成动作证据导航", exact=True).click()
            assert created.value.status == 202, created.value.text()
            request = created.value.request.post_data_json
            assert request["max_model_calls"] == 0 and request["selection_version"]
            assert len(request["action_ids"]) == 2
            jid = created.value.json()["data"]["id"]
            site.service.process_job(jid)
            expect(
                flow.get_by_text("已生成并列的动作证据导航，产物可直接查阅；条目间没有业务先后含义。", exact=True)
            ).to_be_visible(timeout=10000)
            with site.service.db() as s:
                from vks_api.models import Job

                job = s.get(Job, jid)
                assert job.state == "succeeded" and (not job.input_json.get("_model_reserved"))
                assert job.input_json["action_ids"] == request["action_ids"]
            result = page.request.get(url + f"/api/v2/knowledge-tasks/{jid}").json()["data"]
            assert result["report"]["saved_directly"]
            dsl = result["report"]["journey_dsl"]
            assert dsl["semantics"]["kind"] == "evidence_navigation"
            action_keys = {node["key"] for node in dsl["nodes"] if node["kind"] == "action"}
            assert not any((edge["source"] in action_keys and edge["target"] in action_keys for edge in dsl["edges"]))
            assert [node["binding"]["entity_id"] for node in dsl["nodes"] if node["kind"] == "action"] == request[
                "action_ids"
            ]
            page.set_viewport_size({"width": 375, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            browser.close()
