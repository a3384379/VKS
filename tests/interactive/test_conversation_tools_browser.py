"""Real Studio flow for guided association, conversation edits and export receipts."""

import json

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.models import uid
from vks_api.service import DomainError

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_prompt_reassociate_chat_edit_followup_and_export_download(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True,
                    allow_external=True,
                    model_id="synthetic",
                    endpoint="https://api.openai.com/v1",
                    max_output_tokens=8000,
                ).model_dump(),
            )
        )
    calls = []
    guided_attempts = []
    lecture_calls = []

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        if "writing_instruction" in data:
            guided_attempts.append(data)
            if len(guided_attempts) == 1:
                raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "模拟模型响应超时", 503, retryable=True)
        if "associated_knowledge" in data:
            lecture_calls.append(data)
        if "document_sections" in data:
            row = data["document_sections"][0]
            answer = {
                "patches": [
                    {
                        "section_revision_id": row["section_revision_id"],
                        "markdown": row["markdown"] + "\n\n浏览器会话补充的说明。",
                    }
                ],
                "reason": "按用户明确要求补充",
            }
        else:
            answer = {
                "title": "更新后的关联",
                "explanation": f"独立功能讲解 {len(lecture_calls)}"
                if "associated_knowledge" in data
                else "该操作关联办理前提与结果章节。",
                "section_revision_ids": [data["chapters"][0]["revision_id"]],
                "confidence": 0.97,
                "unknowns": [],
            }
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 100},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
        }

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("article", name="关联详情").get_by_role("button", name="编辑", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            panel.get_by_role("button", name="取消", exact=True).click()
            expect(panel).to_have_count(0)
            page.get_by_role("button", name="原型", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            source.get_by_role("button", name="新增收费项", exact=True).click(button="right")
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            panel.get_by_role("button", name="AI 重新关联", exact=True).click()
            review = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            review.get_by_label("关联方向", exact=True).fill("重点说明办理前提和结果")
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(panel.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
            expect(review.get_by_role("alert")).to_contain_text("模拟模型响应超时")
            expect(review.get_by_label("关联方向", exact=True)).to_have_value("重点说明办理前提和结果")
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(review.get_by_text("该操作关联办理前提与结果章节。", exact=True)).to_be_visible()
            review.get_by_role("button", name="确认保存关联", exact=True).click()
            expect(review).to_have_count(0)
            expect(panel.get_by_role("region", name="关联说明")).to_contain_text("该操作关联办理前提与结果章节。")
            expect(panel.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
            assert len(guided_attempts) == 2
            assert all(row["writing_instruction"] == "重点说明办理前提和结果" for row in guided_attempts)
            assert calls[-1]["writing_instruction"] == "重点说明办理前提和结果"
            # Lectures have their own endpoint/result, and need no keywords.
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
            lecture = panel.get_by_role("region", name="AI 功能讲解")
            expect(lecture).to_contain_text("独立功能讲解 1")
            expect(panel.get_by_role("button", name="AI 重新关联", exact=True)).to_have_count(0)
            expect(panel.get_by_role("button", name="重新讲解", exact=True)).to_be_visible()
            expect(review).to_have_count(0)
            panel.get_by_role("button", name="重新讲解", exact=True).click()
            expect(lecture).to_contain_text("独立功能讲解 2")
            assert all("writing_instruction" not in data for data in lecture_calls)
            panel.get_by_role("tab", name="文档", exact=True).click()
            expect(panel.get_by_role("region", name="关联说明")).to_contain_text("该操作关联办理前提与结果章节。")
            expect(panel.get_by_text("独立功能讲解 2", exact=True)).to_have_count(0)
            panel.get_by_role("button", name="AI 重新关联", exact=True).click()
            review.get_by_label("关联方向", exact=True).fill("调整关联章节，保持讲解独立")
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            review.get_by_role("tab", name="Markdown 预览", exact=True).click()
            expect(review.get_by_text("该操作关联办理前提与结果章节。", exact=True)).to_be_visible()
            review.get_by_role("button", name="确认保存关联", exact=True).click()
            expect(review).to_have_count(0)
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
            expect(lecture).to_contain_text("独立功能讲解 2")
            expect(lecture).to_contain_text("关联已更新")
            assert len(lecture_calls) == 2
            panel.get_by_role("tab", name="回答", exact=True).click()
            field = panel.get_by_label("向知识助手提问", exact=True)
            field.fill("请修改关联文案，保留前提并说明结果")
            panel.get_by_role("button", name="发送问题", exact=True).click()
            expect(panel.locator(".chat-operation")).to_contain_text("已保存重新关联的文案")
            panel.get_by_role("button", name="收起助手面板", exact=True).click()
            page.get_by_role("button", name="文档", exact=True).click()
            expect(page.locator(".document-reader")).to_contain_text("新增收费项")
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            document_panel = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            field = document_panel.locator("#fh-ai-question")
            field.fill("请修改当前文档，补充：浏览器会话补充的说明。")
            document_panel.locator("#fh-ai-send").click()
            expect(document_panel.locator(".fh-ai-message.assistant")).to_contain_text("历史修订已保留")
            expect(page.locator(".document-reader")).to_contain_text("浏览器会话补充的说明。")
            expect(document_panel.get_by_role("button", name="查看修改后的文档", exact=True)).to_be_visible()
            field.fill("补充后的文档说明是什么？")
            document_panel.locator("#fh-ai-send").click()
            expect(document_panel.locator(".fh-ai-message.assistant")).to_have_count(2)
            assert "浏览器会话补充的说明" in json.dumps(calls[-1], ensure_ascii=False)
            field.fill("请导出当前文档为 Markdown")
            document_panel.locator("#fh-ai-send").click()
            expect(document_panel.get_by_role("button", name="查看导出结果", exact=True)).to_be_visible()
            with service.db() as s:
                from sqlalchemy import select
                from vks_api.models import Job

                job = s.scalar(select(Job).where(Job.project_id == pid, Job.kind == "export"))
                jid = job.id
            service.process_job(jid)
            with page.expect_download() as downloaded:
                document_panel.get_by_role("button", name="查看导出结果", exact=True).click()
            path = downloaded.value.path()
            import zipfile

            with zipfile.ZipFile(path) as archive:
                docs = [
                    archive.read(n).decode()
                    for n in archive.namelist()
                    if n.startswith("knowledge/") and n.endswith(".md")
                ]
            assert len(docs) == 1 and "浏览器会话补充的说明" in docs[0]
            assert not errors
        finally:
            browser.close()


def test_new_target_reassociation_saves_and_is_visible_after_reload(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    test_materials.install(service, owner, manager, pid, aid)
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True,
                    allow_external=True,
                    model_id="synthetic",
                    endpoint="https://api.openai.com/v1",
                ).model_dump(),
            )
        )
    calls = []

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        answer = {
            "title": "新保存的关联",
            "explanation": "重新关联的说明已经写入数据库。",
            "section_revision_ids": [data["chapters"][0]["revision_id"]],
            "confidence": 0.98,
            "unknowns": [],
        }
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 100},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
        }

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            source.get_by_role("button", name="新增收费项", exact=True).click(button="right")
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            panel.get_by_role("button", name="AI 关联", exact=True).click()
            review = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            review.get_by_label("关联方向", exact=True).fill("关联办理前提")
            with page.expect_response(lambda r: r.url.endswith("/interactive/reassociate")) as previewed:
                review.get_by_role("button", name="生成关联预览", exact=True).click()
            assert previewed.value.status == 200 and not previewed.value.json()["data"]["saved"], previewed.value.text()
            with page.expect_response(lambda r: r.url.endswith("/interactive/reassociate/confirm")) as saved:
                review.get_by_role("button", name="确认保存关联", exact=True).click()
            assert saved.value.status == 200 and saved.value.json()["data"]["saved"], saved.value.text()
            expect(review).to_have_count(0)
            expect(panel.get_by_role("region", name="关联说明")).to_contain_text("重新关联的说明已经写入数据库。")
            expect(panel.get_by_role("alert")).to_have_count(0)
            assert len(calls) == 1
            page.reload()
            page.get_by_role("button", name="关联管理", exact=True).click()
            expect(page.get_by_role("article", name="关联详情")).to_contain_text("重新关联的说明已经写入数据库。")
        finally:
            browser.close()
