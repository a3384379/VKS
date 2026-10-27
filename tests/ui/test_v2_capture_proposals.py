"""Finite saved capture -> draft Journey -> exact passive snapshot, in Chromium."""

from pathlib import Path
from playwright.sync_api import expect
from test_audit_navigation import audit_site, signed_page
from vks_api.journeys import JourneyService
from vks_api.scenarios import ScenarioService

ROOT = Path(__file__).resolve().parents[2]


def test_capture_proposal_canvas_and_replay_never_repeat_recorded_actions(audit_site):
    s = audit_site
    scenarios = ScenarioService(s["service"])
    before = scenarios.list(s["owner"], s["pid"], s["a"])
    captures = scenarios.captures(s["owner"], s["pid"])
    with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}") as page:
        page.get_by_text("从已保存状态构建流程候选", exact=True).click()
        page.get_by_role("checkbox", name="选择录制轨迹 1", exact=True).check()
        requests = []
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.get_by_role("button", name="从选中录制生成流程", exact=True).click()
        expect(page.get_by_label("流程名称", exact=True)).to_have_value("Audit navigation · 流程候选")
        page.locator(".journey-node.reference").first.click()
        page.get_by_role("button", name="查看录制快照", exact=True).click()
        modal = page.get_by_role("dialog")
        expect(modal.get_by_role("heading", name="采集结果", exact=True)).to_be_visible()
        expect(modal.get_by_label("采集快照", exact=True)).to_have_value("0")
        expect(modal.get_by_role("img", name="Synthetic observed · 已脱敏采集截图", exact=True)).to_be_visible()
        output = ROOT / "output/playwright"
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "v2-capture-journey-replay.png"), full_page=True)
        assert not [
            url for method, url in requests if method in ("POST", "PUT", "DELETE") and "/journey-proposals" not in url
        ]
        assert all(url.startswith(s["url"]) or url.startswith("data:") for _, url in requests)
    assert scenarios.list(s["owner"], s["pid"], s["a"]) == before
    assert scenarios.captures(s["owner"], s["pid"]) == captures
    assert len(JourneyService(s["service"]).list(s["owner"], s["pid"], s["a"])) == 1
