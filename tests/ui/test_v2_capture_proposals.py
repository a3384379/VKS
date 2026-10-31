"""Finite saved capture -> draft Journey -> exact passive snapshot, in Chromium."""

from test_audit_navigation import audit_site as audit_site  # pytest fixture registration

from pathlib import Path
import pytest
from playwright.sync_api import expect
from test_audit_navigation import signed_page
from vks_api.journeys import JourneyService
from vks_api.scenarios import ScenarioService

ROOT = Path(__file__).resolve().parents[2]


def exercise_saved_snapshot_viewer(page, trigger, screenshot):
    trigger.click()
    viewer = page.get_by_role("dialog", name="快照大图", exact=True)
    expect(viewer).to_be_visible()
    close = viewer.get_by_role("button", name="关闭快照", exact=True)
    expect(close).to_be_focused()
    canvas = viewer.get_by_role("region", name="快照大图画布", exact=True)
    image = canvas.get_by_role("img")
    page.wait_for_function("image => image.naturalWidth > 0", arg=image.element_handle())
    width = image.evaluate("image => image.naturalWidth")
    expect(canvas).to_have_class("snapshot-viewer-stage fit")
    bounds, frame = image.bounding_box(), canvas.bounding_box()
    assert bounds["width"] <= frame["width"] + 1 and bounds["height"] <= frame["height"] + 1
    # The keyboard remains in the topmost viewer, including its scrollable canvas.
    page.keyboard.press("Shift+Tab")
    expect(canvas).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    viewer.get_by_role("button", name="原始尺寸", exact=True).click()
    expect(image).to_have_css("width", f"{width}px")
    expect(viewer.locator("[aria-live=polite]")).to_have_text("100%")
    viewer.get_by_role("button", name="放大快照", exact=True).click()
    expect(image).to_have_css("width", f"{width * 1.25:g}px")
    expect(viewer.locator("[aria-live=polite]")).to_have_text("125%")
    viewer.get_by_role("button", name="缩小快照", exact=True).click()
    expect(image).to_have_css("width", f"{width}px")
    viewer.get_by_role("button", name="适应窗口", exact=True).click()
    expect(canvas).to_have_class("snapshot-viewer-stage fit")
    output = ROOT / "output/playwright"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / screenshot))
    page.keyboard.press("Escape")
    expect(viewer).to_have_count(0)
    expect(trigger).to_be_focused()
    trigger.press("Enter")
    viewer.get_by_role("button", name="关闭快照", exact=True).click()
    expect(viewer).to_have_count(0)
    expect(trigger).to_be_focused()


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
        page.get_by_role("button", name="维护导览与结构", exact=True).click()
        expect(page.get_by_label("流程名称", exact=True)).to_have_value("Audit navigation · 操作顺序候选")
        page.locator(".journey-node.reference").first.click()
        page.get_by_role("button", name="查看录制快照", exact=True).click()
        modal = page.get_by_role("dialog", name="采集结果", exact=True)
        expect(modal.get_by_role("heading", name="采集结果", exact=True)).to_be_visible()
        expect(modal.get_by_label("采集快照", exact=True)).to_have_value("0")
        expect(modal.get_by_role("img", name="Synthetic observed · 采集截图", exact=True)).to_be_visible()
        exercise_saved_snapshot_viewer(
            page,
            modal.get_by_role("button", name="放大快照：Synthetic observed · 采集截图", exact=True),
            "v2-capture-modal-snapshot-enlarged.png",
        )
        expect(modal).to_be_visible()
        output = ROOT / "output/playwright"
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "v2-capture-journey-replay.png"), full_page=True)
        modal.get_by_role("button", name="关闭对话框", exact=True).click()
        page.get_by_role("button", name="返回业务导览", exact=True).click()
        reader = page.get_by_label("当前导览", exact=True)
        expect(reader.get_by_label("当前观察状态快照", exact=True)).to_be_visible()
        exercise_saved_snapshot_viewer(
            page,
            reader.get_by_role("button", name="放大快照：Synthetic observed · 当前观察状态", exact=True),
            "v2-journey-snapshot-enlarged.png",
        )
        page.set_viewport_size(dict(width=375, height=900))
        exercise_saved_snapshot_viewer(
            page,
            reader.get_by_role("button", name="放大快照：Synthetic observed · 当前观察状态", exact=True),
            "v2-journey-snapshot-enlarged-mobile.png",
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not [
            url
            for method, url in requests
            if method in ("POST", "PUT", "PATCH", "DELETE") and "/journey-proposals" not in url
        ]
        assert all(url.startswith(s["url"]) or url.startswith("data:") for _, url in requests)
    assert scenarios.list(s["owner"], s["pid"], s["a"]) == before
    assert scenarios.captures(s["owner"], s["pid"]) == captures
    assert len(JourneyService(s["service"]).list(s["owner"], s["pid"], s["a"])) == 1


@pytest.fixture
def capture_guide(audit_site):
    from vks_api.capture_proposals import CaptureJourneyService

    s = audit_site
    journeys = JourneyService(s["service"]).list(s["owner"], s["pid"], s["a"])
    if journeys:
        return journeys[0]
    scene = ScenarioService(s["service"]).list(s["owner"], s["pid"], s["a"])[0]
    capture = ScenarioService(s["service"]).captures(s["owner"], s["pid"])[0]
    version = s["service"].workspace(s["owner"], s["pid"], s["a"])["selected_analysis_set"]["selection_version"]
    return CaptureJourneyService(s["service"]).create(
        s["owner"],
        s["pid"],
        dict(
            analysis_set_id=s["a"],
            scenario_id=scene["scenario_id"],
            scenario_revision_id=scene["entity_revision_id"],
            capture_ids=[capture["id"]],
        ),
        version,
        "snapshot-viewer-guide",
    )


@pytest.mark.parametrize("change", ["authority", "visibility", "baseline"])
def test_capture_modal_and_enlarged_cached_image_disappear_when_read_scope_changes(audit_site, capture_guide, change):
    from vks_api.models import Member, Selection

    s = audit_site
    selection_revision = None
    try:
        with signed_page(
            s, f"/#view=journeys&project={s['pid']}&baseline=draft:{s['a']}&entity={capture_guide['journey_id']}"
        ) as page:
            reader = page.get_by_label("当前导览", exact=True)
            reader.get_by_role("button", name="打开完整来源", exact=True).click()
            modal = page.get_by_role("dialog", name="采集结果", exact=True)
            modal.get_by_role("button", name="放大快照：Synthetic observed · 采集截图", exact=True).click()
            viewer = page.get_by_role("dialog", name="快照大图", exact=True)
            expect(viewer.get_by_role("img")).to_be_visible()
            writes = []
            page.on(
                "request",
                lambda request: (
                    writes.append(request.url) if request.method in ("POST", "PUT", "PATCH", "DELETE") else None
                ),
            )
            if change == "authority":
                with s["service"].db() as db:
                    db.get(Member, (s["pid"], s["owner"].id)).role = "editor"
            elif change == "visibility":
                with s["service"].db() as db:
                    selection = db.get(Selection, (s["a"], capture_guide["journey_id"]))
                    selection_revision = selection.entity_revision_id
                    db.delete(selection)
            else:
                page.evaluate(
                    "baseline => { const query = new URLSearchParams(location.hash.slice(1)); query.set('baseline', baseline); location.hash = query.toString(); }",
                    "draft:" + s["b"],
                )
            expect(viewer).to_have_count(0, timeout=10000)
            expect(modal).to_have_count(0)
            expect(page.locator(".snapshot-overlay img")).to_have_count(0)
            assert page.evaluate("document.body.style.overflow") != "hidden"
            assert not writes
    finally:
        with s["service"].db() as db:
            db.get(Member, (s["pid"], s["owner"].id)).role = "admin"
            if selection_revision:
                db.add(
                    Selection(
                        analysis_set_id=s["a"],
                        entity_id=capture_guide["journey_id"],
                        entity_revision_id=selection_revision,
                    )
                )


def test_late_capture_response_cannot_reopen_modal_after_guide_visibility_changes(audit_site, capture_guide):
    from vks_api.models import Selection

    s = audit_site
    reference = next(node["capture_ref"] for node in capture_guide["dsl"]["nodes"] if "capture_ref" in node)
    capture_id = reference["capture_id"]
    selection_revision = None
    with signed_page(
        s, f"/#view=journeys&project={s['pid']}&baseline=draft:{s['a']}&entity={capture_guide['journey_id']}"
    ) as page:
        try:
            reader = page.get_by_label("当前导览", exact=True)
            expect(reader.get_by_role("img", name="Synthetic observed · 当前观察状态", exact=True)).to_be_visible()
            # Obtain an authorized response before the scope changes, then hold
            # its actual UI request until the old guide is no longer visible.
            cached = page.request.get(
                s["url"] + f"/api/v1/projects/{s['pid']}/captures/{capture_id}?analysis_set_id={s['a']}"
            )
            assert cached.ok
            payload = cached.json()
            held = []
            page.route(f"**/api/v1/projects/{s['pid']}/captures/{capture_id}?*", lambda route: held.append(route))
            with page.expect_request(lambda request: f"/captures/{capture_id}?" in request.url):
                reader.get_by_role("button", name="打开完整来源", exact=True).click()
            expect(reader).to_be_visible()
            assert held
            with s["service"].db() as db:
                selection = db.get(Selection, (s["a"], capture_guide["journey_id"]))
                selection_revision = selection.entity_revision_id
                db.delete(selection)
            expect(reader).to_have_count(0, timeout=10000)
            for route in held:
                route.fulfill(status=200, json=payload)
            expect(page.get_by_role("dialog", name="采集结果", exact=True)).to_have_count(0)
            expect(page.get_by_role("dialog", name="快照大图", exact=True)).to_have_count(0)
        finally:
            if selection_revision:
                with s["service"].db() as db:
                    db.add(
                        Selection(
                            analysis_set_id=s["a"],
                            entity_id=capture_guide["journey_id"],
                            entity_revision_id=selection_revision,
                        )
                    )
