"""Round-two product regressions beyond the first eighteen closed checks."""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import site as site, chromium_path  # pytest fixture registration
from test_product_navigation_design_alignment import sign_in
from vks_api.journeys import JourneyService
from vks_api.knowledge import KnowledgeService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Member
from vks_api.modules import ModuleService


ARTIFACTS = Path(__file__).resolve().parents[2] / ".vks/design-audit-round2-20261030"


def failure_artifacts(page, name):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / (name + ".json")).write_text(json.dumps({
        "url": page.url, "body": page.locator("body").inner_text(),
        "selects": page.locator("select").evaluate_all("rows => rows.map(row => ({label: row.labels?.[0]?.textContent, options: [...row.options].map(option => ({value: option.value, text: option.textContent}))}))"),
    }, ensure_ascii=False, indent=2))
    page.screenshot(path=str(ARTIFACTS / (name + ".png")), full_page=True)


def module(service, actor, pid, aid, key, title, entities):
    modules = ModuleService(service)
    row = modules.create(actor, pid, {"key": key, "name": title})
    modules.set_members(actor, pid, row["id"], {
        "analysis_set_id": aid, "entity_ids": [entity["entity_id"] for entity in entities],
        "source_snapshot_ids": [], "connector_ids": [],
    }, row["row_version"])
    return row


def test_search_module_options_drop_currently_invisible_modules_after_permission_refresh(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    symbol = next(entity for entity in w["entities"] if entity["type"] == "symbol")
    private_module = module(service, actor, pid, aid, "round2-private-search", "Round2 private source module", [symbol])
    marker = KnowledgeTaskService(service).create(actor, pid, TaskInput(
        analysis_set_id=aid, task_kind="project_overview",
    ).model_dump(), "round2-search-poll")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            try:
                sign_in(page, url, pid, "overview")
                page.locator(".global-search").click()
                dialog = page.get_by_role("dialog", name="搜索项目知识", exact=True)
                option = dialog.locator(".search-filters select").first.locator(f"option[value='{private_module['id']}']")
                expect(option).to_have_count(1)
                with page.expect_response(lambda response: "/workspace?" in response.url and response.status == 200
                                          and not any(entity["entity_id"] == symbol["entity_id"]
                                                      for entity in response.json()["data"]["entities"]), timeout=10000):
                    with service.db() as db:
                        db.get(Member, (pid, actor.id)).role = "editor"
                assert not any(row["id"] == private_module["id"] for row in ModuleService(service).tree(actor, pid, aid))
                expect(option).to_have_count(0)
            except Exception:
                failure_artifacts(page, "search-private-module-before-fix")
                raise
            finally:
                browser.close()
    finally:
        with service.db() as db:
            db.get(Member, (pid, actor.id)).role = "admin"
        service.cancel_job(actor, marker["id"])


def test_source_connector_response_cannot_restore_manage_only_metadata_after_revocation(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    connector = KnowledgeService(service).add_connector(actor, pid, {
        "key": "round2-private-connector", "name": "Round2 administrator source", "provider": "git",
        "remote_locator": "https://example.invalid/round2-administrator-repository", "secret_ref": None,
    })
    marker = KnowledgeTaskService(service).create(actor, pid, TaskInput(
        analysis_set_id=aid, task_kind="project_overview",
    ).model_dump(), "round2-source-poll")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            held = []
            try:
                sign_in(page, url, pid, "settings")
                def capture_authorized_response(route):
                    response = route.fetch()
                    held.append((route, response))
                    page.evaluate("window.__round2SourceResponseReady = true")
                page.route(f"**/api/v1/projects/{pid}/source-connectors", capture_authorized_response)
                page.get_by_role("button", name="来源连接", exact=True).click()
                page.wait_for_function("window.__round2SourceResponseReady === true")
                assert held and any(row["id"] == connector["id"] for row in held[0][1].json()["data"])
                with page.expect_response(lambda response: "/workspace?" in response.url and response.status == 200
                                          and not any(entity["type"] == "symbol" for entity in response.json()["data"]["entities"]), timeout=10000):
                    with service.db() as db:
                        db.get(Member, (pid, actor.id)).role = "editor"
                expect(page.get_by_role("button", name="登记来源", exact=True)).to_have_count(0)
                for route, response in held:
                    route.fulfill(response=response)
                held.clear()
                expect(page.locator(".source-connectors").get_by_text(connector["name"], exact=True)).not_to_be_visible()
            except Exception:
                failure_artifacts(page, "source-connector-late-before-fix")
                raise
            finally:
                browser.close()
    finally:
        with service.db() as db:
            db.get(Member, (pid, actor.id)).role = "admin"
        service.cancel_job(actor, marker["id"])


def test_background_knowledge_completion_preserves_unsaved_module_metadata(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid, version = w["selected_analysis_set"]["id"], w["selected_analysis_set"]["selection_version"]
    target = module(service, actor, pid, aid, "round2-unsaved-module", "Round2 editable module", [w["actions"][0]])
    marker = KnowledgeTaskService(service).create(actor, pid, TaskInput(
        analysis_set_id=aid, task_kind="business_flow", action_ids=[w["actions"][0]["entity_id"]],
    ).model_dump(), "round2-unsaved-poll")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            sign_in(page, url, pid, "settings")
            page.get_by_role("button", name="项目模块", exact=True).click()
            page.locator(".module-tree").get_by_role("button", name=target["name"], exact=False).click()
            page.locator(".module-detail").get_by_role("button", name="编辑", exact=True).click()
            modal = page.get_by_role("dialog", name="编辑模块", exact=True)
            modal.get_by_label("模块名称", exact=True).fill("Round2 unsaved reviewer meaning")
            with page.expect_response(lambda response: "/workspace?" in response.url and response.status == 200
                                      and response.json()["data"]["selected_analysis_set"]["selection_version"] > version, timeout=10000):
                service.process_job(marker["id"])
            assert service.get_job(actor, marker["id"])["state"] == "succeeded"
            expect(modal).to_be_visible()
            expect(modal.get_by_label("模块名称", exact=True)).to_have_value("Round2 unsaved reviewer meaning")
        except Exception:
            failure_artifacts(page, "module-unsaved-background-before-fix")
            raise
        finally:
            browser.close()


def test_adopting_a_selected_module_candidate_removes_it_from_later_batch_rejection(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    tasks = KnowledgeTaskService(service)
    job = tasks.create(actor, pid, TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"], task_kind="module_analysis",
    ).model_dump(), "round2-module-adopt-selection")
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    candidates = result["report"]["module_candidates"]
    assert len(candidates) >= 2
    adopted, rejected = candidates[:2]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            sign_in(page, url, pid, "agent")
            page.locator(".v2-task-list").get_by_role("button", name=job["id"][:8], exact=False).click()
            adopted_card = page.locator(".v2-domain").filter(has=page.get_by_role("heading", name=adopted["title"], exact=True))
            adopted_card.locator("input[type=checkbox]").check()
            with page.expect_response(lambda response: f"/module-proposals/{adopted['key']}/accept" in response.url) as accepted:
                adopted_card.get_by_role("button", name="采用为模块分组", exact=True).click()
            assert accepted.value.ok, accepted.value.text()
            expect(adopted_card.get_by_text("已采用", exact=True)).to_be_visible()
            rejected_card = page.locator(".v2-domain").filter(has=page.get_by_role("heading", name=rejected["title"], exact=True))
            rejected_card.locator("input[type=checkbox]").check()
            page.get_by_label("批量拒绝原因", exact=True).fill("Reject only the remaining unresolved module candidate")
            with page.expect_response(lambda response: "/module-proposals/reject" in response.url) as response:
                page.get_by_role("button", name="拒绝所选", exact=False).click()
            assert response.value.request.post_data_json["candidate_keys"] == [rejected["key"]], response.value.text()
            assert response.value.ok, response.value.text()
        except Exception:
            failure_artifacts(page, "module-adopt-hidden-selection-before-fix")
            raise
        finally:
            browser.close()


def test_global_search_opens_journey_as_business_guide_with_exact_revision(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    action = w["actions"][0]
    guide = JourneyService(service).create(actor, pid, {
        "analysis_set_id": w["selected_analysis_set"]["id"],
        "dsl": {"schema_version": "2.0", "key": "round2-searchable-guide", "title": "Round2 searchable guide",
                "semantics": {"kind": "evidence_navigation", "basis": "declared", "purpose": "Read exact source"},
                "entry": "action", "nodes": [
                    {"key": "action", "kind": "action", "title": "Read source", "x": 0, "y": 0,
                     "binding": {"entity_id": action["entity_id"], "revision_id": action["entity_revision_id"]}},
                    {"key": "end", "kind": "end", "title": "Done", "x": 300, "y": 0},
                ], "edges": [{"key": "next", "source": "action", "target": "end", "label": ""}]}}, "round2-search-guide")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            sign_in(page, url, pid, "overview")
            page.locator(".global-search").click()
            dialog = page.get_by_role("dialog", name="搜索项目知识", exact=True)
            dialog.locator(".search-input input").fill("Round2 searchable guide")
            dialog.locator(".search-results").get_by_role("button", name="Round2 searchable guide", exact=False).click()
            expect(page.locator(".journey-reading")).to_be_visible()
            assert "journey_revision=" + guide["revision_id"] in page.url
        except Exception:
            failure_artifacts(page, "search-journey-route-before-fix")
            raise
        finally:
            browser.close()
