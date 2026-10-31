"""Independent product closure checks for scope changes and permission renewal."""

from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import site as site, chromium_path  # pytest fixture registration
from test_product_navigation_design_alignment import sign_in
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Member
from vks_api.modules import ModuleService


def module_with_entities(service, actor, pid, aid, key, title, entities):
    modules = ModuleService(service)
    row = modules.create(actor, pid, {"key": key, "name": title})
    modules.set_members(actor, pid, row["id"], {
        "analysis_set_id": aid, "entity_ids": [entity["entity_id"] for entity in entities],
        "source_snapshot_ids": [], "connector_ids": [],
    }, row["row_version"])
    return row


def test_module_settings_clear_previously_visible_source_member_after_grant_revocation(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    workspace = service.workspace(actor, pid)
    aid = workspace["selected_analysis_set"]["id"]
    symbol = next(row for row in workspace["entities"] if row["type"] == "symbol")
    module = module_with_entities(service, actor, pid, aid, "max-revoked", "Alignment revoked module",
                                  [workspace["actions"][0], symbol])
    marker = KnowledgeTaskService(service).create(
        actor, pid, TaskInput(analysis_set_id=aid, task_kind="project_overview").model_dump(), "module-revocation-poll"
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                sign_in(page, url, pid, "settings")
                page.get_by_role("button", name="项目模块", exact=True).click()
                page.locator(".module-tree").get_by_role("button", name=module["name"], exact=False).click()
                old_member = page.locator(".module-entities").get_by_text(symbol["stable_key"], exact=True)
                expect(old_member).to_be_visible()
                with page.expect_response(lambda response: "/workspace?" in response.url
                                          and response.status == 200
                                          and not any(entity["entity_id"] == symbol["entity_id"]
                                                      for entity in response.json()["data"]["entities"]), timeout=10000):
                    with service.db() as db:
                        db.get(Member, (pid, actor.id)).role = "editor"
                expect(old_member).not_to_be_visible()
            finally:
                browser.close()
    finally:
        with service.db() as db:
            db.get(Member, (pid, actor.id)).role = "admin"
        service.cancel_job(actor, marker["id"])


def test_selecting_a_different_module_cannot_keep_an_out_of_scope_current_action(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    workspace = service.workspace(actor, pid)
    aid = workspace["selected_analysis_set"]["id"]
    first, second = workspace["actions"][:2]
    a = module_with_entities(service, actor, pid, aid, "max-module-a", "Alignment module A", [first])
    b = module_with_entities(service, actor, pid, aid, "max-module-b", "Alignment module B", [second])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            sign_in(page, url, pid, "overview")
            page.locator(".business-modules").get_by_role("button", name=a["name"], exact=False).click()
            page.locator(f"[data-module-entity='{first['entity_id']}'] button").click()
            expect(page.locator(".selected-hotspot")).to_contain_text(first["name"])
            page.locator(".business-modules").get_by_role("button", name=b["name"], exact=False).click()
            scope = page.get_by_role("region", name=b["name"] + "模块范围", exact=True)
            expect(scope.locator(f"[data-module-entity='{second['entity_id']}']")).to_be_visible()
            assert scope.locator(f"[data-module-entity='{first['entity_id']}']").count() == 0
            expect(page.locator(".selected-hotspot")).not_to_contain_text(first["name"])
        finally:
            browser.close()


def test_late_entity_link_validation_does_not_override_newer_user_navigation(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    workspace = service.workspace(actor, pid)
    aid = workspace["selected_analysis_set"]["id"]
    action = workspace["actions"][0]
    module = module_with_entities(service, actor, pid, aid, "max-late-link", "Alignment late link", [action])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        held = []
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            sign_in(page, url, pid, "settings")
            page.get_by_role("button", name="项目模块", exact=True).click()
            page.locator(".module-tree").get_by_role("button", name=module["name"], exact=False).click()
            target = page.locator(".module-entities").get_by_role("button", name=action["name"], exact=True)
            expect(target).to_be_visible()
            page.route(f"**/api/v1/projects/{pid}/workspace?*", lambda route: held.append(route))
            with page.expect_request(lambda request: f"/projects/{pid}/workspace?" in request.url):
                target.click()
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="项目空间", exact=True).click()
            expect(page.locator(".v2-hero")).to_be_visible()
            assert held
            with page.expect_response(lambda response: f"/projects/{pid}/workspace?" in response.url):
                for route in held:
                    route.continue_()
                held.clear()
            expect(page.locator(".v2-hero")).to_be_visible()
            assert parse_qs(urlsplit(page.url).fragment)["view"] == ["overview"]
        finally:
            browser.close()
