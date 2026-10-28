from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import site, chromium_path


def login(page, url, pid, view="versions"):
    page.goto(url + f"/#view={view}&project={pid}")
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()


def test_real_browser_submits_documents_then_atomically_reviews_mixed_types(site):
    url, pid, _, _ = site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login(page, url, pid)
            inbox = page.locator(".review-v2-inbox")
            expect(inbox).to_be_visible()
            page.get_by_label("审核对象类型").select_option("document")
            expect(inbox.locator("article[data-review-key^='document:']")).not_to_have_count(0)
            page.get_by_label("收件箱审核原因").fill("Synthetic browser document submission")
            page.get_by_label("选择本页可审核对象").check()
            with page.expect_response(lambda r: "/review-batches" in r.url and r.request.method == "POST") as saved:
                inbox.get_by_role("button", name="提交文档审核", exact=True).click()
            assert saved.value.status == 200, saved.value.text()
            expect(page.get_by_label("审核对象类型")).to_have_value("all")
            workspace = page.request.get(url + f"/api/v1/projects/{pid}/workspace").json()["data"]
            aid = workspace["selected_analysis_set"]["id"]
            action = workspace["actions"][0]
            csrf = page.request.get(url + "/api/v1/auth/session").json()["data"]["csrf_token"]
            response = page.request.post(
                url + f"/api/v2/projects/{pid}/journeys",
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "browser-mixed-flow"},
                data=dict(
                    analysis_set_id=aid,
                    dsl=dict(
                        schema_version="2.0",
                        key="browser-review-flow",
                        title="Browser review flow",
                        entry="a",
                        nodes=[
                            dict(
                                key="a",
                                kind="action",
                                title="Action",
                                x=40,
                                y=40,
                                binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
                            ),
                            dict(key="end", kind="end", title="End", x=300, y=40),
                        ],
                        edges=[dict(key="next", source="a", target="end", label="")],
                    ),
                ),
            )
            assert response.status == 201, response.text()
            flow = response.json()["data"]
            page.reload()
            inbox = page.locator(".review-v2-inbox")
            expect(inbox.locator(f"article[data-review-key='journey:{flow['journey_id']}']")).to_be_visible()
            for checkbox in inbox.locator("article[data-review-key^='document:'] input[type=checkbox]").all():
                checkbox.check()
            inbox.locator(f"article[data-review-key='journey:{flow['journey_id']}'] input[type=checkbox]").check()
            page.get_by_label("收件箱审核原因").fill("Exact documents and flow checked together")
            with page.expect_response(lambda r: "/review-batches" in r.url and r.request.method == "POST") as saved:
                inbox.get_by_role("button", name="批准 / 接受", exact=True).click()
            assert saved.value.status == 200, saved.value.text()
            assert saved.value.json()["data"]["count"] >= 2
            listing = page.request.get(
                url + f"/api/v2/projects/{pid}/review-items?analysis_set_id={aid}&status=approved&limit=100"
            ).json()["data"]
            assert any(
                row["subject_id"] == flow["journey_id"] and row["status"] == "approved" for row in listing["items"]
            )
            assert any(row["kind"] == "document" and row["status"] == "approved" for row in listing["items"])
        finally:
            browser.close()


def test_wizard_creates_ordered_candidate_without_binding_json_or_model(site):
    url, pid, _, _ = site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login(page, url, pid, "imports")
            page.locator(".v2-steps button").nth(4).click()
            flow = page.get_by_role("region", name="向导流程草稿")
            expect(flow).to_be_visible()
            controls = flow.locator(".v2-action-options input[type=checkbox]")
            controls.nth(0).check()
            controls.nth(1).check()
            flow.locator(".v2-ordered-actions li").nth(1).get_by_role(
                "button", name="向导动作上移", exact=False
            ).click()
            with page.expect_response(lambda r: "/knowledge-tasks" in r.url and r.request.method == "POST") as created:
                flow.get_by_role("button", name="生成向导流程草稿", exact=True).click()
            assert created.value.status == 202, created.value.text()
            request = created.value.request.post_data_json
            assert request["max_model_calls"] == 0 and request["selection_version"]
            assert len(request["action_ids"]) == 2
            jid = created.value.json()["data"]["id"]
            site.service.process_job(jid)
            expect(flow.get_by_text("已生成独立待审流程草稿，原始材料与已有流程保持不变。", exact=True)).to_be_visible(
                timeout=10000
            )
            with site.service.db() as s:
                from vks_api.models import Job

                job = s.get(Job, jid)
                assert job.state == "succeeded" and not job.input_json.get("_model_reserved")
                assert job.input_json["action_ids"] == request["action_ids"]
            result = page.request.get(url + f"/api/v2/knowledge-tasks/{jid}").json()["data"]
            assert result["report"]["candidate_only"]
            dsl = result["report"]["journey_dsl"]
            assert [node["binding"]["entity_id"] for node in dsl["nodes"] if node["kind"] == "action"] == request[
                "action_ids"
            ]
            page.set_viewport_size({"width": 375, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            browser.close()
