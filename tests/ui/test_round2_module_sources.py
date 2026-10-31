"""Real browser acceptance for precise module/source many-to-many bindings."""

from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import site as site, chromium_path  # pytest fixture registration
from test_product_navigation_design_alignment import sign_in
from test_round2_product_context_and_permissions import failure_artifacts, module
from vks_api.knowledge import KnowledgeService
from vks_api.modules import ModuleService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Member


def test_module_sources_can_bind_multiple_exact_snapshots_and_same_named_connectors(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    snapshots = w["sources"][:2]
    assert len(snapshots) == 2
    connections = [KnowledgeService(service).add_connector(actor, pid, {
        "key": f"round2-exact-source-{index}", "name": "Same repository title", "provider": "git",
        "remote_locator": f"https://example.invalid/repository-{index}", "secret_ref": None,
    }) for index in range(2)]
    first = module(service, actor, pid, aid, "round2-source-first", "Round2 source first module", [w["actions"][0]])
    second = module(service, actor, pid, aid, "round2-source-second", "Round2 source second module", [w["actions"][0]])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            sign_in(page, url, pid, "settings")
            page.get_by_role("button", name="项目模块", exact=True).click()

            def open_binding(target):
                page.locator(".module-tree").get_by_role("button", name=target["name"], exact=False).click()
                page.get_by_role("button", name="调整实体归属", exact=True).click()
                return page.get_by_role("dialog", name="调整模块的实体归属", exact=True)

            def save_binding(dialog):
                with page.expect_response(lambda response: f"/modules/" in response.url and response.request.method == "PUT") as saved:
                    dialog.get_by_role("button", name="保存归属", exact=True).click()
                assert saved.value.ok, saved.value.text()
                expect(dialog).to_have_count(0)
                return saved.value.request.post_data_json

            dialog = open_binding(first)
            snapshot_choices = dialog.get_by_role("group", name="来源快照", exact=True)
            connector_choices = dialog.get_by_role("group", name="来源连接", exact=True)
            expect(snapshot_choices).to_be_visible()
            for snapshot in snapshots:
                snapshot_choices.locator(f"input[value='{snapshot['source_snapshot_id']}']").check()
            for connection in connections:
                connector_choices.locator(f"input[value='{connection['id']}']").check()
            failure_artifacts(page, "product-fix-module-sources-binding")
            body = save_binding(dialog)
            assert set(body["source_snapshot_ids"]) == {row["source_snapshot_id"] for row in snapshots}
            assert set(body["connector_ids"]) == {row["id"] for row in connections}
            assert body["entity_ids"] == [w["actions"][0]["entity_id"]]

            dialog = open_binding(second)
            dialog.get_by_role("group", name="来源快照", exact=True).locator(f"input[value='{snapshots[0]['source_snapshot_id']}']").check()
            dialog.get_by_role("group", name="来源连接", exact=True).locator(f"input[value='{connections[0]['id']}']").check()
            save_binding(dialog)
            modules = ModuleService(service)
            first_members = modules.members(actor, pid, first["id"], aid)
            second_members = modules.members(actor, pid, second["id"], aid)
            assert set(first_members["source_snapshot_ids"]) == {row["source_snapshot_id"] for row in snapshots}
            assert set(first_members["connector_ids"]) == {row["id"] for row in connections}
            assert second_members["source_snapshot_ids"] == [snapshots[0]["source_snapshot_id"]]
            assert second_members["connector_ids"] == [connections[0]["id"]]

            # Exact IDs survive a reopen and allow removing one of two identical names.
            dialog = open_binding(first)
            dialog.get_by_role("group", name="来源连接", exact=True).locator(f"input[value='{connections[1]['id']}']").uncheck()
            save_binding(dialog)
            assert modules.members(actor, pid, first["id"], aid)["connector_ids"] == [connections[0]["id"]]
            failure_artifacts(page, "product-fix-module-sources-passed")
        except Exception:
            failure_artifacts(page, "product-fix-module-sources-failure")
            raise
        finally:
            browser.close()


def test_module_binding_revocation_hides_connection_choices_and_preserves_hidden_memberships(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    connection = KnowledgeService(service).add_connector(actor, pid, {
        "key": "round2-revoked-binding", "name": "Round2 administrator binding source", "provider": "git",
        "remote_locator": "https://example.invalid/private-binding", "secret_ref": None,
    })
    target = module(service, actor, pid, aid, "round2-revoked-binding", "Round2 retained public module", [w["actions"][0]])
    modules = ModuleService(service)
    current = modules.members(actor, pid, target["id"], aid)
    modules.set_members(actor, pid, target["id"], {
        "analysis_set_id": aid, "entity_ids": current["entity_ids"],
        "source_snapshot_ids": [], "connector_ids": [connection["id"]],
    }, current["row_version"])
    marker = KnowledgeTaskService(service).create(actor, pid, TaskInput(
        analysis_set_id=aid, task_kind="project_overview",
    ).model_dump(), "round2-binding-revoke-poll")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            try:
                sign_in(page, url, pid, "settings")
                page.get_by_role("button", name="项目模块", exact=True).click()
                page.locator(".module-tree").get_by_role("button", name=target["name"], exact=False).click()
                page.get_by_role("button", name="调整实体归属", exact=True).click()
                dialog = page.get_by_role("dialog", name="调整模块的实体归属", exact=True)
                expect(dialog.get_by_role("group", name="来源连接", exact=True).locator(f"input[value='{connection['id']}']")).to_be_checked()
                with page.expect_response(lambda response: "/workspace?" in response.url and response.status == 200
                                          and not any(entity["type"] == "symbol" for entity in response.json()["data"]["entities"]), timeout=10000):
                    with service.db() as db:
                        db.get(Member, (pid, actor.id)).role = "editor"
                expect(dialog).to_have_count(0)
                expect(page.locator("body")).not_to_contain_text(connection["name"])
                page.locator(".module-tree").get_by_role("button", name=target["name"], exact=False).click()
                page.get_by_role("button", name="调整实体归属", exact=True).click()
                expect(dialog.get_by_role("group", name="来源连接", exact=True)).to_have_count(0)
                with page.expect_response(lambda response: f"/modules/{target['id']}/members" in response.url and response.request.method == "PUT") as saved:
                    dialog.get_by_role("button", name="保存归属", exact=True).click()
                assert saved.value.ok, saved.value.text()
                assert saved.value.request.post_data_json["connector_ids"] == []
                with service.db() as db:
                    db.get(Member, (pid, actor.id)).role = "admin"
                assert modules.members(actor, pid, target["id"], aid)["connector_ids"] == [connection["id"]]
                failure_artifacts(page, "product-fix-module-binding-revocation-passed")
            except Exception:
                failure_artifacts(page, "product-fix-module-binding-revocation-failure")
                raise
            finally:
                browser.close()
    finally:
        with service.db() as db:
            db.get(Member, (pid, actor.id)).role = "admin"
        service.cancel_job(actor, marker["id"])


def test_module_metadata_background_refresh_keeps_original_cas_and_conflicting_input(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid, version = w["selected_analysis_set"]["id"], w["selected_analysis_set"]["selection_version"]
    target = module(service, actor, pid, aid, "round2-cas-draft", "Round2 original CAS module", [w["actions"][0]])
    modules = ModuleService(service)
    initial_version = modules.members(actor, pid, target["id"], aid)["row_version"]
    marker = KnowledgeTaskService(service).create(actor, pid, TaskInput(
        analysis_set_id=aid, task_kind="business_flow", action_ids=[w["actions"][0]["entity_id"]],
    ).model_dump(), "round2-module-cas-poll")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            sign_in(page, url, pid, "settings")
            page.get_by_role("button", name="项目模块", exact=True).click()
            page.locator(".module-tree").get_by_role("button", name=target["name"], exact=False).click()
            page.locator(".module-detail").get_by_role("button", name="编辑", exact=True).click()
            dialog = page.get_by_role("dialog", name="编辑模块", exact=True)
            draft = "Round2 preserved local conflicting name"
            dialog.get_by_label("模块名称", exact=True).fill(draft)
            modules.update(actor, pid, target["id"], {"name": "Round2 remote newer name"}, initial_version)
            with page.expect_response(lambda response: "/workspace?" in response.url and response.status == 200
                                      and response.json()["data"]["selected_analysis_set"]["selection_version"] > version, timeout=10000):
                service.process_job(marker["id"])
            expect(dialog.get_by_label("模块名称", exact=True)).to_have_value(draft)
            with page.expect_response(lambda response: f"/modules/{target['id']}" in response.url and response.request.method == "PATCH") as saved:
                dialog.get_by_role("button", name="保存模块", exact=True).click()
            assert saved.value.status == 412, saved.value.text()
            assert saved.value.request.headers["if-match"] == f'"{initial_version}"'
            expect(dialog.get_by_label("模块名称", exact=True)).to_have_value(draft)
            expect(dialog.get_by_text("模块已被其他人更新，当前输入保留。关闭前请复制变更，再重新载入并合并。", exact=True)).to_be_visible()
            failure_artifacts(page, "product-fix-module-original-cas-passed")
        except Exception:
            failure_artifacts(page, "product-fix-module-original-cas-failure")
            raise
        finally:
            browser.close()
