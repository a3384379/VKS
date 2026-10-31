"""Product closure on isolated data and a separate Chromium context; no model call."""

from test_frontend_browser import site as site  # pytest fixture registration

from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import chromium_path
from test_product_navigation_design_alignment import sign_in
from vks_api.journeys import JourneyService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.modules import ModuleService


def create_material(service, actor, pid, aid, action, kind):
    return JourneyService(service).create(
        actor,
        pid,
        {
            "analysis_set_id": aid,
            "dsl": {
                "schema_version": "2.0",
                "key": "product-" + kind,
                "title": "Product " + kind,
                "entry": "action",
                "semantics": {"kind": kind, "basis": "declared", "purpose": "核对固定范围", "outcome": "阅读准确来源"},
                "nodes": [
                    {
                        "key": "action",
                        "kind": "action",
                        "title": "Read fixed action",
                        "x": 0,
                        "y": 0,
                        "binding": {"entity_id": action["entity_id"], "revision_id": action["entity_revision_id"]},
                    },
                    {"key": "done", "kind": "end", "title": "Done", "x": 300, "y": 0},
                ],
                "edges": [{"key": "next", "source": "action", "target": "done", "label": ""}],
            },
        },
        "product-" + kind,
    )


def test_cockpit_semantics_and_adopted_module_open_exact_members_at_four_widths(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid, action = w["selected_analysis_set"]["id"], w["actions"][0]
    materials = [
        create_material(service, actor, pid, aid, action, kind)
        for kind in (
            "business_process",
            "operation_sequence",
            "evidence_navigation",
            "observation_comparison",
            "unclassified",
        )
    ]
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        actor, pid, TaskInput(analysis_set_id=aid, task_kind="module_analysis").model_dump(), "product-modules"
    )
    service.process_job(job["id"])
    task = tasks.get(actor, job["id"])
    candidate = next(row for row in task["report"]["module_candidates"] if action["entity_id"] in row["entity_ids"])
    module = tasks.accept_module(actor, job["id"], candidate["key"], "product-adopt")
    modules = ModuleService(service)
    module = next(row for row in modules.tree(actor, pid, aid) if row["id"] == module["module_id"])
    w = service.workspace(actor, pid)
    page_entity = next(page for page in w["pages"] if page["stable_key"] == action["metadata"]["page_key"])
    document = w["documents"][0]
    chosen = [page_entity["id"], action["id"], materials[0]["journey_id"], materials[2]["journey_id"], document["id"]]
    modules.set_members(
        actor,
        pid,
        module["id"],
        {"analysis_set_id": aid, "entity_ids": chosen, "source_snapshot_ids": [], "connector_ids": []},
        module["row_version"],
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            sign_in(page, url, pid, "overview")
            expect(page.get_by_role("heading", name="业务场景 · 1", exact=True)).to_be_visible()
            for title in ("操作序列 · 1", "证据导航 · 1", "观察对比 · 1", "待分类材料 · 1"):
                expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
            page.locator(".business-modules").get_by_role("button", name=candidate["title"], exact=False).click()
            scope = page.locator(f"section[aria-label='{candidate['title']}模块范围']")
            expect(scope.locator("[data-module-entity]")).to_have_count(len(chosen))
            assert parse_qs(urlsplit(page.url).fragment)["module"] == [module["id"]]
            for width in (1440, 1024, 768, 375):
                page.set_viewport_size({"width": width, "height": 1100})
                expect(scope.locator(f"[data-module-entity='{action['id']}'] button")).to_be_visible()
                assert page.locator(".business-modules").evaluate(
                    "el => el.getBoundingClientRect().right <= innerWidth + 1"
                )
            page.set_viewport_size({"width": 1440, "height": 1100})
            scope.locator(f"[data-module-entity='{action['id']}'] button").click()
            expect(page.locator(".knowledge-detail")).to_be_visible()
            assert parse_qs(urlsplit(page.url).fragment)["entity"] == [action["id"]]
            assert parse_qs(urlsplit(page.url).fragment)["module"] == [module["id"]]
            page.reload()
            expect(page.locator(f"[data-module-entity='{materials[2]['journey_id']}'] button")).to_be_visible()
            page.locator(f"[data-module-entity='{materials[2]['journey_id']}'] button").click()
            expect(page.locator(".journey-reading")).to_be_visible()
            query = parse_qs(urlsplit(page.url).fragment)
            assert query["journey"] == [materials[2]["journey_id"]]
            assert query["journey_revision"] == [materials[2]["revision_id"]]
            assert query["module"] == [module["id"]]
        finally:
            browser.close()


def test_no_model_task_results_show_coverage_gaps_and_open_exact_generated_material(site):
    url, pid, _, _ = site
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    tasks = KnowledgeTaskService(service)
    jobs = []
    for kind in ("project_overview", "evidence_gaps", "business_flow"):
        payload = TaskInput(
            analysis_set_id=aid,
            task_kind=kind,
            action_ids=[row["id"] for row in w["actions"][:2]] if kind == "business_flow" else [],
        ).model_dump()
        job = tasks.create(actor, pid, payload, "product-result-" + kind)
        service.process_job(job["id"])
        result = tasks.get(actor, job["id"])
        assert result["state"] == "succeeded", result
        assert result["report"]["model_calls"] == 0
        jobs.append(result)
    # A provider-free document result exercises the real job serializer and UI,
    # while keeping generation quality outside this product-navigation fixture.
    from vks_api.models import Job

    document = w["documents"][0]
    with service.db() as session:
        row = session.get(Job, jobs[0]["id"])
        row.result_json = dict(
            row.result_json,
            result_refs=[
                *row.result_json["result_refs"],
                {"kind": "document_revision", "id": document["entity_revision_id"]},
            ],
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            sign_in(page, url, pid, "agent")
            for result in jobs[:2]:
                page.locator(".v2-task-list").get_by_role("button", name=result["id"][:8], exact=False).click()
                panel = page.locator("[aria-label='任务可读结果']")
                expect(panel.get_by_text("动作知识覆盖", exact=True)).to_be_visible()
                expect(panel.get_by_text("运行证据未核对动作", exact=True)).to_be_visible()
                if result["task_kind"] == "project_overview":
                    panel.get_by_role("button", name=document["name"], exact=True).click()
                    expect(page.locator(".document-reader")).to_be_visible()
                    query = parse_qs(urlsplit(page.url).fragment)
                    assert query["entity"] == [document["entity_id"]]
                    assert query["baseline"] == ["draft:" + aid]
                    page.goto(url + f"/#view=agent&project={pid}")
                if result["task_kind"] == "evidence_gaps" and result["report"]["uncovered_actions"]:
                    action = result["report"]["uncovered_actions"][0]
                    panel.get_by_role("button", name=action["name"], exact=True).click()
                    expect(page.locator(".knowledge-detail")).to_be_visible()
                    assert parse_qs(urlsplit(page.url).fragment)["entity"] == [action["entity_id"]]
                    page.goto(url + f"/#view=agent&project={pid}")
            result = jobs[2]
            ref = next(ref for ref in result["result_refs"] if ref["kind"] == "journey_revision")
            target = next(
                row for row in service.workspace(actor, pid)["entities"] if row["entity_revision_id"] == ref["id"]
            )
            page.locator(".v2-task-list").get_by_role("button", name=result["id"][:8], exact=False).click()
            page.locator("[aria-label='任务可读结果']").get_by_role("button", name=target["name"], exact=True).click()
            expect(page.locator(".journey-reading")).to_be_visible()
            query = parse_qs(urlsplit(page.url).fragment)
            assert query["journey"] == [target["entity_id"]]
            assert query["journey_revision"] == [ref["id"]]
        finally:
            browser.close()


def test_fixed_change_report_opens_affected_action_section_and_journey(site):
    from vks_api.documents import DocumentService
    from vks_api.knowledge import KnowledgeService

    pass

    url = site[0]
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(
        actor, {"key": "product-impact", "name": "Product impact fixture"}, "product-impact"
    )
    pid = project["id"]
    pass
    job = service.demo_import(actor, pid, "product-impact-import")
    service.process_job(job["id"])
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    action = w["actions"][0]
    material = create_material(service, actor, pid, aid, action, "business_process")
    candidate = service.save_snapshot(actor, pid, {"analysis_set_id": aid}, "product-impact-candidate")
    pass
    ticket = None
    release = candidate
    clone = KnowledgeService(service).clone(actor, pid, aid, "Product impact target", "product-impact-clone")
    document = w["documents"][0]
    docs = DocumentService(service)
    old = docs.read_revision(actor, document["id"], document["entity_revision_id"])
    # A fixed source edit gives real changes and graph propagation, without a provider.
    docs.save_draft(
        actor,
        document["id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": document["entity_revision_id"],
            "markdown": old["markdown"].replace("## 查询缴费记录", "## 查询与筛选")
            + "\n\nImpact fixture clarification.\n",
            "locked_section_ids": [],
        },
        1,
    )
    report = service.diff(actor, pid, release["id"], clone["id"])
    assert report["impact_paths"], report
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            sign_in(page, url, pid, "versions")
            expect(page.locator(".version-summary")).to_be_visible()
            page.locator(".version-compare select").nth(1).select_option(clone["id"])
            page.get_by_role("button", name="比较版本", exact=True).click()
            impact = page.locator("[aria-label='业务影响路径']")
            expect(impact).to_be_visible()
            expect(impact.get_by_text("直接材料变化", exact=True)).to_be_visible()
            # Choose actual returned visible paths, not guessed business relations.
            target_w = service.workspace(actor, pid, clone["id"])
            targets = {e["id"]: e for e in target_w["entities"]}
            action_path = next(
                path
                for path in report["impact_paths"][:30]
                if targets.get(path["impacted_entity_id"], {}).get("type") == "action"
            )
            affected_action = targets[action_path["impacted_entity_id"]]
            # A path can contain different entities with the same display name.
            impact.locator(f"[data-impact-target='{affected_action['id']}']").first.locator(
                f"[data-impact-entity='{affected_action['id']}']"
            ).get_by_role("button", name=affected_action["name"], exact=True).click()
            expect(page.locator(".knowledge-detail")).to_be_visible()
            query = parse_qs(urlsplit(page.url).fragment)
            assert query["entity"] == [affected_action["id"]]
            assert query["baseline"] == ["draft:" + clone["id"]]
            page.goto(url + f"/#view=versions&project={pid}&baseline=draft:{clone['id']}")
            page.get_by_role("button", name="比较版本", exact=True).click()
            expect(impact).to_be_visible()
            section_path = next(
                path
                for path in report["impact_paths"][:30]
                if any(targets.get(eid, {}).get("type") == "section" for eid in path["entity_ids"])
            )
            section = next(
                targets[eid] for eid in section_path["entity_ids"] if targets.get(eid, {}).get("type") == "section"
            )
            impact.locator(f"[data-impact-target='{section_path['impacted_entity_id']}']").first.locator(
                f"[data-impact-entity='{section['id']}']"
            ).get_by_role("button", name=section["name"], exact=True).click()
            expect(page.locator(".document-reader")).to_be_visible()
            assert parse_qs(urlsplit(page.url).fragment)["entity"] == [section["id"]]
            assert parse_qs(urlsplit(page.url).fragment)["baseline"] == ["draft:" + clone["id"]]
            page.goto(url + f"/#view=versions&project={pid}&baseline=draft:{clone['id']}")
            page.get_by_role("button", name="比较版本", exact=True).click()
            expect(impact).to_be_visible()
            expect(impact.get_by_role("button", name="Product business_process", exact=True).first).to_be_visible()
            impact.get_by_role("button", name="Product business_process", exact=True).first.click()
            expect(page.locator(".journey-reading")).to_be_visible()
            assert parse_qs(urlsplit(page.url).fragment)["journey_revision"] == [material["revision_id"]]
        finally:
            browser.close()
