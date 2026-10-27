from pathlib import Path
from playwright.sync_api import expect
from test_audit_navigation import audit_site, signed_page
from vks_api.scenarios import ScenarioService
from vks_api.knowledge import KnowledgeService
from packages.collector.snapshots import Snapshot, bundle_bytes

ROOT = Path(__file__).resolve().parents[2]


def test_graph_uses_bounded_server_data_and_scrolls_all_100_nodes_instead_of_radial_13(audit_site):
    s = audit_site
    service = s["service"]
    actor = s["owner"]
    clone = KnowledgeService(service).clone(actor, s["pid"], s["a"], "Map fixture", "map-fixture-clone")
    aid = clone["id"]
    content = (
        '<main data-vks-page="ui.page.map-many"><h1>Map fixture</h1>'
        + "".join(f'<button data-vks-action="ui.action.map-{i}">Map action {i}</button>' for i in range(150))
        + "</main>"
    ).encode()
    raw = bundle_bytes(
        Snapshot("local", "map", {"map.html": content}),
        "charge-demo",
        "map-fixture",
        source_key="map-ui",
        role="prototype",
    )
    with service.db() as db:
        service._commit_bundle(db, s["pid"], actor.id, service._parse(raw, "charge-demo"), aid)
    entity = next(
        row for row in service.workspace(actor, s["pid"], aid)["pages"] if row["stable_key"] == "ui.page.map-many"
    )
    with signed_page(s, f"/#view=graph&project={s['pid']}&baseline=draft:{aid}&entity={entity['entity_id']}") as page:
        expect(page.locator(".graph-node")).to_have_count(100)
        expect(page.get_by_text("已达到 100 节点上限", exact=False)).to_be_visible()
        assert page.locator(".v2-graph-scroll").evaluate("(el)=>el.scrollHeight>el.clientHeight")
        output = ROOT / "output/playwright"
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "v2-bounded-knowledge-map.png"), full_page=True)
        page.get_by_label("仅已确认关系", exact=True).check()
        expect(page.locator(".graph-node")).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")


def test_saved_trajectory_comparison_shows_exact_observation_without_business_requests(audit_site):
    s = audit_site
    record = ScenarioService(s["service"]).captures(s["owner"], s["pid"])[0]
    with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}") as page:
        expect(page.get_by_label("右侧轨迹", exact=True).locator("option")).not_to_have_count(0)
        page.get_by_label("右侧轨迹", exact=True).select_option("capture:" + record["id"])
        requests = []
        page.on("request", lambda r: requests.append((r.method, r.url)))
        page.get_by_role("button", name="比较已保存轨迹", exact=True).click()
        expect(page.get_by_label("右侧轨迹观察快照", exact=True)).to_be_visible()
        expect(page.get_by_text("固定比较摘要：", exact=False)).to_be_visible()
        assert not [row for row in requests if row[0] in ("POST", "PUT", "DELETE")]
        assert all(url.startswith(s["url"]) or url.startswith("data:") for _, url in requests)


def test_revoked_sensitive_grants_clear_previously_loaded_trajectory_on_workspace_refresh(audit_site):
    from vks_api.models import Member
    from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput

    s = audit_site
    record = ScenarioService(s["service"]).captures(s["owner"], s["pid"])[0]
    job = KnowledgeTaskService(s["service"]).create(
        s["owner"],
        s["pid"],
        TaskInput(analysis_set_id=s["a"], task_kind="project_overview").model_dump(),
        "ui-role-refresh",
    )
    try:
        with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}") as page:
            page.get_by_label("右侧轨迹", exact=True).select_option("capture:" + record["id"])
            page.get_by_role("button", name="比较已保存轨迹", exact=True).click()
            expect(page.get_by_label("右侧轨迹观察快照", exact=True)).to_be_visible()
            with s["service"].db() as db:
                db.get(Member, (s["pid"], s["owner"].id)).role = "editor"
            # An unrelated queued task causes the normal workspace/session poll.
            # Membership changes do not advance the knowledge selection version.
            expect(page.get_by_label("右侧轨迹观察快照", exact=True)).not_to_be_visible(timeout=10000)
            expect(
                page.get_by_label("右侧轨迹", exact=True).locator(f'option[value="capture:{record["id"]}"]')
            ).to_have_count(0)
    finally:
        with s["service"].db() as db:
            db.get(Member, (s["pid"], s["owner"].id)).role = "admin"
        s["service"].cancel_job(s["owner"], job["id"])
