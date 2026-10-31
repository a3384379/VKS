"""A09–A11 and explanation navigation use a fully isolated synthetic site."""

from contextlib import contextmanager
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
import os
import socket
import subprocess
import time
import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from test_frontend_browser import chromium_path, live_api
from packages.analyzers.vks_ingest import ParsedBundle, _entity
from vks_api.app import create_app
from vks_api.documents import DocumentService
from vks_api.hotspots import HotspotService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Evidence, Relation, Revision, uid
from vks_api.relationships import RelationshipService
from vks_api.scenarios import ScenarioService
from vks_api.versions import VersionService

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def audit_site(tmp_path_factory):
    directory = tmp_path_factory.mktemp("audit-navigation")
    app = create_app(directory / "store", enable_worker=False)
    s = app.state.service
    _, owner = s.login(s.bootstrap_token)
    project = s.create_project(owner, {"key": "charge-demo", "name": "Audit main"}, "main")
    pid = project["id"]
    job = s.demo_import(owner, pid, "demo")
    s.process_job(job["id"])
    w = s.workspace(owner, pid)
    aid = w["selected_analysis_set"]["id"]
    extra = ParsedBundle(
        manifest={"baseline_key": "extra"},
        entities=[
            _entity(
                "ui.page.audit-b",
                "page",
                "Audit B page",
                '<main data-vks-page="ui.page.audit-b"><button data-vks-action="ui.action.audit-b">Audit B action</button></main>',
                "",
                {},
                {"route": "/audit-b", "stateKey": "b"},
            ),
            _entity("ui.page.audit-empty", "page", "Audit empty page", "<main>Empty synthetic page</main>", "", {}, {}),
            _entity(
                "ui.action.audit-b",
                "action",
                "Audit B action",
                "B action explanation",
                "",
                {},
                {"page_key": "ui.page.audit-b", "event": "click"},
            ),
            _entity(
                "api.audit-b",
                "api",
                "Audit B endpoint",
                "B endpoint explanation",
                "",
                {},
                {"method": "GET", "normalized_route": "/audit-b"},
            ),
        ],
    )
    with s.db() as db:
        s._commit_bundle(db, pid, owner.id, extra, aid)
    w = s.workspace(owner, pid, aid)
    action_b = next((e for e in w["actions"] if e["stable_key"] == "ui.action.audit-b"))
    action_a = next((e for e in w["actions"] if e["metadata"].get("page_key") != "ui.page.audit-b"))
    page_a = next((p for p in w["pages"] if p["stable_key"] == action_a["metadata"]["page_key"]))
    api_b = next((e for e in w["entities"] if e["stable_key"] == "api.audit-b"))
    with s.db() as db:
        row = db.get(Revision, page_a["entity_revision_id"])
        row.metadata_json = dict(row.metadata_json, stateKey="ready")
        row = db.get(Revision, action_a["entity_revision_id"])
        row.metadata_json = dict(
            row.metadata_json, target_state_key="b", rect={"x": 0.2, "y": 0.2, "width": 0.15, "height": 0.1}
        )
        ev = db.scalar(select(Evidence).where(Evidence.analysis_set_id == aid))
        db.add(
            Relation(
                id=uid(),
                project_id=pid,
                analysis_set_id=aid,
                from_revision_id=action_b["entity_revision_id"],
                to_revision_id=api_b["entity_revision_id"],
                relation_type="may_call",
                evidence_ids=[ev.id],
                reason="Synthetic",
                excluded=False,
                validity_status="valid",
                causal_basis="not_established",
            )
        )
    scenario = ScenarioService(s).create(
        owner,
        pid,
        {
            "analysis_set_id": aid,
            "dsl": {
                "schema_version": "1.0",
                "scenario_key": "audit-navigation",
                "name": "Audit navigation",
                "environment_ref": "nav-mock",
                "dataset_ref": "nav-data",
                "risk_level": "read_only",
                "timeout_ms": 10000,
                "steps": [
                    {"step_key": "open", "kind": "goto", "path": "/", "risk": "read_only", "timeout_ms": 3000},
                    {
                        "step_key": "snapshot",
                        "kind": "snapshot",
                        "name": "Synthetic observed",
                        "risk": "read_only",
                        "timeout_ms": 3000,
                    },
                ],
                "explanation_graph": {
                    "projectKey": "charge-demo",
                    "baselineKey": w["selected_analysis_set"]["key"],
                    "states": [
                        {"stateKey": "ready", "label": "Ready state", "evidenceOrigin": "declared"},
                        {"stateKey": "b", "label": "B simulated state", "evidenceOrigin": "simulated"},
                    ],
                    "transitions": [
                        {
                            "transitionKey": "to-b",
                            "fromState": "ready",
                            "toState": "b",
                            "actionKey": action_a["stable_key"],
                            "conditionLabel": "Choose B branch",
                            "evidenceOrigin": "simulated",
                            "scenarioRef": "audit-navigation",
                        }
                    ],
                },
            },
        },
        "scenario",
    )
    for rel in s.workspace(owner, pid, aid)["relations"]:
        s.set_relation_state(
            owner,
            rel["id"],
            {
                "excluded": dict(decision="confirm", reason="Synthetic baseline", evidence_ids=rel["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            rel["row_version"],
        )
    candidate = s.save_snapshot(owner, pid, {"analysis_set_id": aid}, "release")
    ticket = None
    release = candidate
    a = KnowledgeService(s).clone(owner, pid, aid, "A working", "clone-a")
    b = KnowledgeService(s).clone(owner, pid, aid, "B target", "clone-b")

    class CapturePage(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<main><h1>Synthetic captured UI</h1></main>")

    capture_server = ThreadingHTTPServer(("127.0.0.1", 0), CapturePage)
    capture_thread = Thread(target=capture_server.serve_forever, daemon=True)
    capture_thread.start()
    try:
        with sync_playwright() as playwright:
            executable = chromium_path(playwright)
        with patch.dict(
            os.environ,
            {"VKS_RUNNER_ENABLED": "1", "VKS_RUNNER_ALLOW_LOOPBACK": "1", "VKS_CHROMIUM_EXECUTABLE": executable},
        ):
            svc = ScenarioService(s)
            base = f"http://127.0.0.1:{capture_server.server_port}"
            svc.create_environment(
                owner,
                pid,
                dict(
                    key="nav-mock",
                    name="Synthetic capture",
                    kind="mock",
                    base_origin=base,
                    allowed_origins=[base],
                    allowed_cidrs=["127.0.0.1/32"],
                    allow_loopback=True,
                ),
            )
            svc.create_dataset(owner, pid, dict(key="nav-data", name="Synthetic", synthetic=True, data={}))
            current = svc.list(owner, pid, a["id"])[0]
            approval = dict(
                analysis_set_id=a["id"],
                scenario_revision_id=current["scenario_revision_id"],
                reason="Synthetic UI capture",
            )
            accepted = svc.request_capture(
                owner,
                current["scenario_id"],
                dict(
                    scenario_revision_id=current["scenario_revision_id"],
                    environment_ref="nav-mock",
                    analysis_set_id=approval["analysis_set_id"],
                ),
                "capture",
            )
            s.process_job(accepted["job"]["id"])
            assert s.get_job(owner, accepted["job"]["id"])["state"] == "succeeded"
    finally:
        capture_server.shutdown()
        capture_server.server_close()
        capture_thread.join(timeout=2)
    with s.db() as db:
        s._new_job(db, owner, pid, "index", {"analysis_set_id": a["id"]})
    later = []
    for index in range(1, 26):
        later.append(s.create_project(owner, {"key": f"audit-{index}", "name": f"Project {index:02}"}, str(index)))
    with live_api(app) as api:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        program = "import {createServer} from 'vite';\n        const server=await createServer({root:process.argv[1]+'/apps/web',configFile:process.argv[1]+'/apps/web/vite.config.ts',\n        cacheDir:process.argv[2],server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,\n        proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}});await server.listen();"
        log = (directory / "vite.log").open("wb")
        process = subprocess.Popen(
            ["node", "--input-type=module", "-e", program, str(ROOT), str(directory / "cache"), str(port), api],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(timeout=1) as client:
                for _ in range(200):
                    assert process.poll() is None, (directory / "vite.log").read_text()
                    try:
                        if client.get(url).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("Isolated Vite did not start")
            yield dict(
                url=url,
                pid=pid,
                a=a["id"],
                b=b["id"],
                release=release["id"],
                action_b=action_b["entity_id"],
                action_a=action_a["entity_id"],
                action_a_name=action_a["name"],
                scenario=scenario["scenario_id"],
                page_a=page_a["name"],
                late=later[-1],
                service=s,
                owner=owner,
            )
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()
    s.engine.dispose()


@contextmanager
def signed_page(site, path, *, prepare=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1586, "height": 992})
        if prepare:
            prepare(page)
        page.goto(site["url"] + path)
        page.get_by_label("登录名", exact=True).fill("owner")
        page.get_by_label("密码", exact=True).fill("owner")
        page.get_by_role("button", name="打开工作空间").click()
        try:
            yield page
        finally:
            browser.close()


def test_a09_search_b_action_overrides_previously_selected_a_page(audit_site):
    s = audit_site
    with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}") as page:
        page.locator(".scenario-tree").get_by_role("button", name=s["page_a"], exact=True).click()
        page.locator(".global-search").click()
        dialog = page.get_by_role("dialog")
        dialog.locator("input").first.fill("Audit B action")
        dialog.get_by_text("Audit B action", exact=True).click()
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit B page")
        expect(page.locator(".selected-hotspot")).to_contain_text("Audit B action")
        assert "entity=" + s["action_b"] in page.url
        page.locator(".knowledge-detail").get_by_role("button", name="接口", exact=True).click()
        expect(page.locator(".knowledge-detail .api-card")).to_contain_text("/audit-b")


def test_a10_comparison_keeps_requested_target_through_refresh(audit_site):
    s = audit_site
    with signed_page(s, f"/#view=versions&project={s['pid']}&baseline=draft:{s['a']}") as page:
        selects = page.locator(".version-compare select")
        selects.nth(0).select_option(s["release"])
        selects.nth(1).select_option(s["b"])
        page.get_by_role("button", name="比较版本", exact=True).click()
        expect(selects.nth(1)).to_have_value(s["b"])
        expect(page.get_by_label("本次版本对比")).to_contain_text("B target")
        with page.expect_response(lambda r: "/workspace?" in r.url):
            pass
        expect(selects.nth(1)).to_have_value(s["b"])
        expect(page.get_by_label("本次版本对比")).to_contain_text("B target")
        selects.nth(1).select_option(s["a"])
        expect(page.get_by_label("本次版本对比")).to_have_count(0)


def test_a11_later_project_deep_link_and_project_pagination(audit_site):
    s = audit_site
    with signed_page(s, f"/#view=overview&project={s['late']['id']}") as page:
        expect(page.get_by_label("当前项目", exact=True)).to_have_value(s["late"]["id"])
        expect(page.locator(".breadcrumb")).to_contain_text("Project 25")
        page.reload()
        expect(page.get_by_label("当前项目", exact=True)).to_have_value(s["late"]["id"])
        page.get_by_role("button", name="加载更多项目", exact=True).click()
        expect(page.get_by_label("当前项目", exact=True).locator("option")).to_have_count(26)
        page.get_by_label("搜索项目", exact=True).fill("Project 24")
        expect(page.get_by_label("当前项目", exact=True).locator("option")).to_contain_text(
            ["Project 25", "Project 24"]
        )


@pytest.mark.parametrize("status", [403, 404])
def test_a11_inaccessible_pinned_project_retains_accessible_list(audit_site, status):
    site = audit_site
    missing = "00000000-0000-0000-0000-000000000000"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            if status == 403:
                page.route(
                    f"**/api/v1/projects/{missing}**",
                    lambda route: route.fulfill(
                        status=403,
                        content_type="application/json",
                        body='{"error":{"code":"VKS_FORBIDDEN","message":"Synthetic access revoked"}}',
                    ),
                )
            page.goto(site["url"] + f"/#view=overview&project={missing}&baseline=draft:{missing}&entity=missing")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间", exact=True).click()
            selector = page.get_by_label("当前项目", exact=True)
            expect(selector).not_to_have_value(missing)
            expect(selector.locator("option")).not_to_have_count(0)
            expect(page.locator(".breadcrumb")).to_contain_text("Audit main")
            assert "project=" + missing not in page.url
            assert "baseline=draft%3A" + missing not in page.url and "entity=missing" not in page.url
            other = selector.locator("option").nth(1)
            other_id, other_name = (other.get_attribute("value"), other.inner_text())
            selector.select_option(other_id)
            expect(selector).to_have_value(other_id)
            expect(page.locator(".breadcrumb")).to_contain_text(other_name)
        finally:
            browser.close()


def test_g01_hotspot_branch_page_tree_and_url_use_one_passive_context(audit_site):
    s = audit_site

    def prepare(page):
        page.add_init_script(
            "(() => {\n          window.__navigationEvidence = [];\n          const record = event => {\n            window.__navigationEvidence.push({event, hash:location.hash,\n              selected:document.querySelector('.scenario-states button.selected')?.textContent || ''});\n            window.__navigationEvidence = window.__navigationEvidence.slice(-30);\n          };\n          for (const name of ['pushState','replaceState']) {\n            const original = history[name].bind(history);\n            history[name] = (...args) => { const result = original(...args); record(name); return result; };\n          }\n          for (const event of ['popstate','hashchange']) addEventListener(event, () => record(event));\n        })()"
        )

    with signed_page(
        s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}&entity={s['action_a']}", prepare=prepare
    ) as page:
        external = []
        page.on(
            "request",
            lambda request: (
                external.append(request.url)
                if request.url.startswith("http") and (not request.url.startswith(s["url"]))
                else None
            ),
        )
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Ready state")
        page.locator(".authored-hotspot").click()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("B simulated state")
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit B page")
        expect(page.locator(".selected-hotspot")).to_contain_text("Audit B action")
        assert "state=b" in page.url and "scenario=" + s["scenario"] in page.url
        page.go_back()
        try:
            expect(page.locator(".scenario-states button.selected")).to_contain_text("Ready state")
        except AssertionError as error:
            evidence = page.evaluate("window.__navigationEvidence")
            raise AssertionError(f"{error}\nURL: {page.url}\nNavigation events: {evidence}") from error
        page.go_forward()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("B simulated state")
        page.reload()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("B simulated state")
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit B page")
        page.locator(".scenario-tree").get_by_role("button", name=s["page_a"], exact=True).click()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Ready state")
        page.get_by_role("button", name="Choose B branch", exact=False).click()
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit B page")
        page.locator(".scenario-tree").get_by_role("button", name="Audit empty page", exact=True).click()
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit empty page")
        expect(page.locator(".action-tree-item")).to_have_count(0)
        assert not external


def test_g01_native_back_at_first_branch_dom_commit_restores_same_ready_context(audit_site):
    s = audit_site
    with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}&entity={s['action_a']}") as page:
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Ready state")
        page.evaluate(
            "() => {\n          const observer = new MutationObserver(() => {\n            if (!document.querySelector('.scenario-states button.selected')?.textContent.includes('B simulated state')) return;\n            observer.disconnect();\n            window.__branchCommitHash = location.hash;\n            history.back();\n          });\n          observer.observe(document.body, {subtree:true, childList:true, attributes:true});\n        }"
        )
        page.locator(".authored-hotspot").click()
        page.wait_for_function("window.__branchCommitHash !== undefined", timeout=5000)
        assert "state=b" in page.evaluate("window.__branchCommitHash")
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Ready state")
        expect(page.locator(".selected-hotspot")).to_contain_text(s["action_a_name"])
        assert "state=ready" in page.url


def test_g02_view_capture_bind_action_and_restore_observed_state(audit_site):
    s = audit_site
    with signed_page(s, f"/#view=workbench&project={s['pid']}&baseline=draft:{s['a']}") as page:
        external = []
        page.on(
            "request",
            lambda request: (
                external.append(request.url)
                if request.url.startswith("http") and (not request.url.startswith(s["url"]))
                else None
            ),
        )
        page.get_by_role("button", name="查看采集结果", exact=True).click()
        viewer = page.get_by_role("dialog", name="采集结果", exact=True)
        expect(viewer.locator("img")).to_be_visible()
        viewer.get_by_text("查看安全 DOM 快照", exact=True).click()
        expect(viewer).to_contain_text("Synthetic captured UI")
        viewer.get_by_role("button", name="关联已有动作", exact=True).click()
        binding = page.get_by_role("dialog", name="绑定采集到讲解状态", exact=True)
        binding.get_by_label("讲解状态键", exact=True).fill("captured-ui")
        binding.get_by_label("状态名称", exact=True).fill("Captured UI state")
        binding.get_by_label("关联动作（人工声明）", exact=True).select_option(s["action_b"])
        binding.get_by_role("button", name="保存采集绑定", exact=True).click()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Captured UI state")
        expect(page.locator(".preview-toolbar")).to_contain_text("Audit B page")
        expect(page.locator(".selected-hotspot")).to_contain_text("Audit B action")
        expect(page.get_by_label("当前观察状态快照").locator("img")).to_be_visible()
        page.wait_for_function("document.querySelector('[aria-label=\"当前观察状态快照\"] img')?.naturalWidth > 0")
        expect(page.locator(".scenario-states")).to_contain_text("Ready state")
        expect(page.locator(".scenario-states")).to_contain_text("B simulated state")
        assert "state=captured-ui" in page.url
        page.reload()
        expect(page.locator(".scenario-states button.selected")).to_contain_text("Captured UI state")
        expect(page.get_by_label("当前观察状态快照").locator("img")).to_be_visible()
        assert not external


def test_a05_all_knowledge_tabs_follow_retarget_hotspot_and_version_migration(audit_site):
    site = audit_site
    service, owner, pid = (site["service"], site["owner"], site["pid"])
    released = service.workspace(owner, pid, release_id=site["release"])
    source = released["selected_analysis_set"]["id"]
    clone = KnowledgeService(service).clone(owner, pid, source, "Policy lifecycle", "policy-lifecycle")
    aid = clone["id"]
    workspace = service.workspace(owner, pid, aid)
    action = next((e for e in workspace["actions"] if e["entity_id"] == site["action_b"]))
    sections = [e for e in workspace["entities"] if e["type"] == "section"]
    symbol = next((e for e in workspace["entities"] if e["type"] in ("symbol", "code_symbol", "sql")))
    api = next((e for e in workspace["entities"] if e["stable_key"] == "api.audit-b"))
    with service.db() as db:
        for relation in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            relation.excluded = True
        evidence = db.scalar(select(Evidence).where(Evidence.analysis_set_id == aid))
        for target, kind in ((api, "may_call"), (sections[0], "documents"), (symbol, "handled_by")):
            existing = db.scalar(
                select(Relation).where(
                    Relation.analysis_set_id == aid,
                    Relation.from_revision_id == action["entity_revision_id"],
                    Relation.to_revision_id == target["entity_revision_id"],
                    Relation.relation_type == kind,
                )
            )
            if existing:
                existing.excluded = False
            else:
                db.add(
                    Relation(
                        id=uid(),
                        project_id=pid,
                        analysis_set_id=aid,
                        from_revision_id=action["entity_revision_id"],
                        to_revision_id=target["entity_revision_id"],
                        relation_type=kind,
                        evidence_ids=[evidence.id],
                        reason="Synthetic isolated edge",
                        excluded=False,
                        validity_status="valid",
                        causal_basis="not_established",
                    )
                )
    with signed_page(site, f"/#view=workbench&project={pid}&baseline=draft:{aid}&entity={action['entity_id']}") as page:
        panel = page.locator(".knowledge-detail")

        def assert_tabs(current_aid, action_id, policy="valid"):
            expected = service.action_context(owner, pid, action_id, current_aid, relation_policy=policy)
            types = [e["type"] for e in expected["entities"] if e["entity_id"] != action_id]
            for name, selector, count in (
                ("接口", ".api-card", sum((t in ("api", "api_endpoint") for t in types))),
                ("实现", ".code-evidence", sum((t in ("symbol", "code_symbol", "sql") for t in types))),
                ("文档", ".section-preview", sum((t in ("section", "document_section", "document") for t in types))),
                ("证据", ".evidence-item", len(expected["evidence"])),
            ):
                panel.locator(".detail-tabs").get_by_role("button", name=name, exact=True).click()
                expect(panel.locator(selector)).to_have_count(count)
            return expected

        assert_tabs(aid, action["entity_id"])
        workspace = service.workspace(owner, pid, aid)
        edge = next(
            (
                r
                for r in workspace["relations"]
                if r["from_revision_id"] == action["entity_revision_id"] and r["relation_type"] == "documents"
            )
        )
        result = RelationshipService(service).correct(
            owner,
            edge["id"],
            dict(
                analysis_set_id=aid,
                operation="retarget",
                reason="Synthetic new target",
                targets=[dict(to_revision_id=sections[1]["entity_revision_id"], evidence_ids=edge["evidence_ids"])],
            ),
            edge["row_version"],
        )
        assert result["original_relations"][0]["excluded"] is True
        page.reload()
        assert any((e["type"] == "section" for e in assert_tabs(aid, action["entity_id"])["entities"]))
        panel.get_by_role("button", name="全部关联", exact=True).click()
        assert any((e["type"] == "section" for e in assert_tabs(aid, action["entity_id"], "all")["entities"]))
        new = result["new_relations"][0]
        service.set_relation_state(
            owner,
            new["id"],
            {
                "excluded": dict(decision="confirm", reason="Synthetic review", evidence_ids=new["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            new["row_version"],
        )
        page.reload()
        assert_tabs(aid, action["entity_id"])
        workspace = service.workspace(owner, pid, aid)
        hotspot_action = next((e for e in workspace["actions"] if e["entity_id"] == site["action_a"]))
        for edge in workspace["relations"]:
            if edge["from_revision_id"] == hotspot_action["entity_revision_id"]:
                service.set_relation_state(
                    owner,
                    edge["id"],
                    {
                        "excluded": dict(
                            decision="confirm", reason="Synthetic hotspot baseline", evidence_ids=edge["evidence_ids"]
                        )["decision"]
                        == "reject"
                    },
                    edge["row_version"],
                )
        page.goto(
            site["url"] + f"/#view=workbench&project={pid}&baseline=draft:{aid}&entity={hotspot_action['entity_id']}"
        )
        assert assert_tabs(aid, hotspot_action["entity_id"])["relations"]
        current_page = next(
            (p for p in workspace["pages"] if p["stable_key"] == hotspot_action["metadata"]["page_key"])
        )
        HotspotService(service).save(
            owner,
            pid,
            current_page["entity_id"],
            dict(
                analysis_set_id=aid,
                base_page_revision_id=current_page["entity_revision_id"],
                action_id=hotspot_action["entity_id"],
                label="Revised audit hotspot",
                rect=dict(x=0.1, y=0.1, width=0.1, height=0.1),
            ),
            current_page["row_version"],
        )
        page.reload()
        expect(page.locator(".selected-hotspot")).to_contain_text("Revised audit hotspot")
        assert not assert_tabs(aid, hotspot_action["entity_id"])["relations"]
        migrated = KnowledgeService(service).clone(owner, pid, source, "Policy migration", "policy-migration")
        workspace = service.workspace(owner, pid, migrated["id"])
        document = workspace["documents"][0]
        documents = DocumentService(service)
        revision = documents.read_revision(owner, document["entity_id"], document["entity_revision_id"])
        documents.save_draft(
            owner,
            document["entity_id"],
            dict(
                analysis_set_id=migrated["id"],
                base_revision_id=revision["entity_revision_id"],
                markdown=revision["markdown"].replace("## 查询缴费记录", "## 查询与筛选"),
                locked_section_ids=[],
            ),
            revision["row_version"],
        )
        with service.db() as db:
            VersionService(service).detect_drift(db, pid, site["release"], migrated["id"], owner.id)
        page.goto(
            site["url"] + f"/#view=workbench&project={pid}&baseline=draft:{migrated['id']}&entity={site['action_a']}"
        )
        assert_tabs(migrated["id"], site["action_a"])
        panel.get_by_role("button", name="失效关联", exact=True).click()
        pending = assert_tabs(migrated["id"], site["action_a"], "outdated")
        assert pending["relations"] and all((r["validity_status"] != "valid" for r in pending["relations"]))
