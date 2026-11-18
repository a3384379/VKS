"""Background page associations must not reset a conversation's immutable scope."""

import json
from threading import Event

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.document_presentation import DocumentPresentation
from vks_api.models import Job
from vks_api.scan_pipeline import reconcile

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.interactive.test_runtime_inventory_browser import setup, start_legacy_pipeline
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_pipeline_catalog_refresh_retains_draft_history_citation_and_inflight_answer(materials, tmp_path, monkeypatch):
    # Three page batches are needed to exercise the three independent catalog
    # refreshes below now that one job can contain a whole page's controls.
    from tests.interactive import test_runtime_inventory_browser as inventory

    zipper = inventory.zip_docs
    monkeypatch.setattr(
        inventory, "zip_docs", lambda pages: zipper([*pages, ("c.html", inventory.dynamic_page("账单 C"))])
    )
    web, service, owner, pid, aid, _calls = setup(materials, monkeypatch)
    with service.db() as db:
        db.add(DocumentPresentation(analysis_set_id=aid, settings={"mode": "handbook"}))
    entered, release = Event(), Event()
    questions = []

    def model(_config, body):
        payload = json.loads(body["input"][1]["content"])
        if "question" in payload:
            questions.append(payload)
            if payload["question"] == "新增收费项客户名称有哪些要求":
                entered.set()
                assert release.wait(20), "The browser must preserve the in-flight question during a binding refresh"
        if "targets" in payload:
            from tests.interactive.test_bulk_association import synthetic_response

            return synthetic_response(body, [])
        answer = test_assistant.response(payload["chapters"][0]["revision_id"])
        if "question" in payload:
            content = answer["output"][0]["content"][0]
            value = json.loads(content["text"])
            value["explanation"] = "Answer: " + payload["question"] + " [1]"
            content["text"] = json.dumps(value)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            start_legacy_pipeline(page, service, owner, pid, aid)
            expect(page.get_by_label("已保存的预扫描进度", exact=True)).to_contain_text(
                "已采集 3 / 3 页", timeout=20000
            )
            with service.db() as db:
                jobs = list(
                    db.scalars(
                        select(Job.id)
                        .where(Job.project_id == pid, Job.kind == "associate_all")
                        .order_by(Job.created_at, Job.id)
                    )
                )
            assert len(jobs) >= 1
            with service.db() as db:
                inputs = [db.get(Job, jid).input_json for jid in jobs]
            targets = [item["id"] for payload in inputs for item in payload["items"]]
            assert len(targets) == len(set(targets)) == 12
            page.get_by_role("button", name="文档", exact=True).click()
            expect(page.get_by_label("文档阅读方式", exact=True)).to_have_count(0)
            book = page.get_by_role("region", name="知识手册阅读模板")
            book.get_by_role("button", name="总览", exact=True).click()
            book.get_by_role("button", name="向手册 AI 提问", exact=True).click()
            panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            field = panel.locator("#fh-ai-question")

            def finish_background(job_id):
                with page.expect_response(
                    lambda response: "/interactive/catalog?" in response.url and response.status == 200, timeout=12000
                ):
                    service.process_job(job_id)
                    outcome = service.get_job(owner, job_id)
                    assert outcome["state"] == "succeeded", outcome
                    assert outcome["result"]["saved"] > 0, json.dumps(outcome, ensure_ascii=False)
                    reconcile(service)

            field.fill("Draft survives background association")
            finish_background(jobs[0])
            expect(field).to_have_value("Draft survives background association")
            field.fill("新增收费项需要客户名称吗")
            panel.locator("#fh-ai-send").click()
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("Answer: 新增收费项需要客户名称吗")
            # Opening a citation changes route.doc but retains the original
            # project-wide question scope and its continuous history.
            panel.locator(".fh-ai-cite").first.click()
            panel.get_by_role("button", name="打开当前手册原文 ↗", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(1)
            finish_background(jobs[1])
            expect(panel.locator(".fh-ai-message.assistant")).to_contain_text("Answer: 新增收费项需要客户名称吗")
            field.fill("新增收费项客户名称有哪些要求")
            panel.locator("#fh-ai-send").click()
            assert entered.wait(5)
            finish_background(jobs[2])
            expect(panel.locator("#fh-ai-stop")).to_be_visible()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            release.set()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            expect(panel.locator(".fh-ai-message.assistant").last).to_contain_text(
                "Answer: 新增收费项客户名称有哪些要求"
            )
            assert len(questions) == 2 and len(questions[1]["history"]) == 2

            # A genuine document replacement invalidates the old persisted
            # answer scope when reopening the project.
            _, _, _, manager, _ = materials
            from vks_api.interactive import InteractiveKnowledgeService

            catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
            document_package = catalog["documents"][0]["package_id"]
            updated = test_materials.stage(
                service,
                owner,
                manager,
                pid,
                aid,
                "documents",
                "# New guide\n\nChanged source corpus.",
                package_id=document_package,
            )
            test_materials.activate(service, owner, manager, pid, aid, [updated])
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(panel.locator(".fh-ai-message.assistant")).to_have_count(0)
            expect(field).to_have_value("")
        finally:
            release.set()
            browser.close()
