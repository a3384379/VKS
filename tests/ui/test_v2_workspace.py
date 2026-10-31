from pathlib import Path
from playwright.sync_api import expect, sync_playwright
import pytest

from test_frontend_browser import site as site, chromium_path  # pytest fixture registration

ROOT = Path(__file__).resolve().parents[2]


def test_v2_journey_save_history_and_passive_exploration(site):
    url, pid, _, _ = site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        requests = []
        page.on("request", lambda request: requests.append((request.method, request.url)))
        try:
            page.goto(url + f"/#view=journeys&project={pid}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            expect(page.get_by_role("navigation", name="主导航").get_by_role("button")).to_have_count(5)
            page.get_by_role("button", name="维护导览与结构", exact=True).click()
            page.get_by_role("button", name="新建流程", exact=True).click()
            page.get_by_label("流程名称", exact=True).fill("Synthetic invoice journey")
            page.get_by_role("button", name="保存流程", exact=True).click()
            expect(page.get_by_text("流程已保存为不可变修订", exact=True)).to_be_visible()
            title = page.get_by_label("流程名称", exact=True)
            title.fill("Changed title")
            page.get_by_role("button", name="撤销", exact=True).click()
            expect(title).to_have_value("Synthetic invoice journey")
            page.get_by_role("button", name="重做", exact=True).click()
            expect(title).to_have_value("Changed title")
            page.get_by_role("button", name="还原", exact=True).click()
            page.reload()
            page.get_by_role("button", name="维护导览与结构", exact=True).click()
            expect(title).to_have_value("Synthetic invoice journey")
            workspace = page.request.get(url + f"/api/v1/projects/{pid}/workspace").json()["data"]
            aid = workspace["selected_analysis_set"]["id"]
            version = workspace["selected_analysis_set"]["selection_version"]
            csrf = page.request.get(url + "/api/v1/auth/session").json()["data"]["csrf_token"]
            rows = page.request.get(url + f"/api/v2/projects/{pid}/journeys?analysis_set_id={aid}").json()["data"]
            row = next(r for r in rows if r["dsl"]["title"] == "Synthetic invoice journey")
            queued = page.request.post(
                url + f"/api/v2/projects/{pid}/knowledge-tasks",
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "poll-for-concurrency"},
                data={"analysis_set_id": aid, "task_kind": "project_overview"},
            )
            assert queued.ok, queued.text()
            page.reload()
            page.get_by_role("button", name="维护导览与结构", exact=True).click()
            expect(title).to_have_value("Synthetic invoice journey")
            title.fill("Unsaved local journey")
            changed = dict(row["dsl"], title="Another editor saved this")
            with page.expect_response(
                lambda r: (
                    "/workspace?" in r.url
                    and r.status == 200
                    and r.json()["data"]["selected_analysis_set"]["selection_version"] > version
                )
            ):
                updated = page.request.post(
                    url + f"/api/v2/journeys/{row['journey_id']}/revisions",
                    headers={
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "concurrent-editor",
                        "If-Match": row["revision_id"],
                    },
                    data={"analysis_set_id": aid, "dsl": changed},
                )
                assert updated.ok, updated.text()
            page.get_by_role("button", name="保存流程", exact=True).click()
            expect(title).to_have_value("Unsaved local journey")
            expect(page.get_by_text("当前内容已更新，请重新加载后合并", exact=False)).to_be_visible()
            page.get_by_role("button", name="还原", exact=True).click()
            # Arrow keys edit coordinates through the same undo history.
            node = page.locator(".journey-node.action")
            node.focus()
            node.press("ArrowRight")
            page.get_by_role("button", name="还原", exact=True).click()
            page.get_by_role("button", name="查看同版知识", exact=False).click()
            expect(page.locator(".knowledge-detail")).to_be_visible()
            page.locator(".knowledge-detail").get_by_role("button", name="接口", exact=True).click()
            assert all("/api/" in target or target.startswith(url) for _, target in requests)
            assert not [r for r in requests if r[0] in ("POST", "PUT", "DELETE") and not r[1].startswith(url + "/api/")]
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1600, 1440, 1024, 768, 375])
def test_v2_cockpit_responsive_old_hash_and_onboarding(site, width):
    url, pid, _, _ = site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        try:
            page.goto(url + f"/#view=overview&project={pid}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            expect(page.locator(".v2-hero")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"), page.evaluate(
                "Array.from(document.querySelectorAll('body *')).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(e=>e.right>innerWidth+2).slice(0,16)"
            )
            if width == 1440:
                output = ROOT / "output/playwright"
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / "v2-project-space.png"), full_page=True)
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="知识构建", exact=True).click()
            expect(page.get_by_role("heading", name="智能接入向导", exact=True)).to_be_visible()
            page.locator(".v2-steps button").nth(2).click()
            page.get_by_role("button", name="检查接入范围", exact=True).click()
            expect(page.get_by_role("heading", name="检查问题并上传原始材料")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"), page.evaluate(
                "Array.from(document.querySelectorAll('body *')).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(e=>e.right>innerWidth+2).slice(0,16)"
            )
        finally:
            browser.close()
