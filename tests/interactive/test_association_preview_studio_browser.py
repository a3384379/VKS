"""The prototype footer opens review without inference; only confirmation saves."""

import json
import re
from contextlib import suppress
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.materials import MaterialService

from tests.interactive import test_assistant
from tests.interactive.test_native_documents_browser import native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_footer_review_cancel_and_detached_confirmation_preserve_exact_preview(assistant, tmp_path, monkeypatch):
    (web, service, owner, pid, aid, grant, package, section, _raw), body, calls = assistant
    MaterialService(service).open(owner, pid, aid)
    binding = test_assistant.form(aid, package, section)
    binding["target"]["frame_path"] = []
    binding["point"]["summary"] = "关联正文与准确引用[1]。相邻引用[2][3]。\n\n`[1]` 保持代码原样。"
    chapters = InteractiveAssistant(service).prepare(owner, pid, AssistanceInput.model_validate(body))[1]
    assert len(chapters) >= 3
    binding["section_revision_ids"] = [row["revision_id"] for row in chapters[:3]]
    version = test_assistant.current(web, pid, aid, grant)["selection_version"]
    response = web.post(
        test_assistant.root(pid) + "/bindings", json=binding, headers=_headers(grant, version, "preview-ui-existing")
    )
    assert response.status_code == 200, response.text

    def links():
        return web.get(
            test_assistant.root(pid) + "/bindings",
            params={"analysis_set_id": aid},
            headers=_headers(grant, 0, "preview-ui-read"),
        ).json()["data"]

    original = links()

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        result = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(result["output"][0]["content"][0]["text"])
        if "writing_instruction" in data:
            answer["title"] = "准备替换的标题"
            answer["explanation"] = "**预览规则**\n\n| 检查 | 预览结果 |\n| --- | --- |\n| 原关联 | 确认后才替换 |\n\n依据[1]。"
        else:
            assert "当前页面的操作指引" in payload["input"][0]["content"]
            assert "associated_knowledge" not in data
            answer["explanation"] = "独立讲解保持原关联。[1]"
        result["output"][0]["content"][0]["text"] = json.dumps(answer)
        return result

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script("if(top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        page.add_init_script("""
            if (window === top) {
                const OriginalChannel = window.MessageChannel;
                window.runtimeEvents = [];
                window.MessageChannel = class extends OriginalChannel {
                    constructor() {
                        super();
                        this.port1.addEventListener("message", ({data}) => {
                            if (!data?.type?.startsWith("context.")) return;
                            runtimeEvents.push({
                                type: data.type, sequence: data.sequence, instance: data.runtime_instance_id,
                                view: new URLSearchParams(location.hash.slice(1)).get("view"),
                                route: data.payload?.target?.route || data.payload?.route
                            });
                        });
                    }
                };
            }
        """)
        page.context.tracing.start(screenshots=True, snapshots=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            add = source.get_by_role("button", name="Add", exact=True)
            expect(add).to_be_visible(timeout=30000)
            add.click(button="right", position={"x": 2, "y": 2})
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")

            def open_citation(number, section_id):
                before = page.url
                panel.get_by_role("button", name=f"原文引用 {number}", exact=True).click()
                preview = page.get_by_role("dialog", name="引用原文预览", exact=True)
                expect(preview).to_be_visible()
                assert page.url == before
                preview.get_by_role("button", name="打开文档原文", exact=True).click()
                expect(page).to_have_url(re.compile("view=documents"))
                assert "section=" + section_id in page.url

            expect(panel.locator(".knowledge-context")).to_contain_text("Add")
            expect(panel.locator(".reassociate-panel")).to_have_count(0)
            references = panel.locator(".source-disclosure")
            expect(references).to_have_count(1)
            expect(references).not_to_have_attribute("open", "")
            expect(references.locator(".citation")).to_have_count(0)
            references.locator("summary").first.click()
            expect(references.locator(".citation")).not_to_have_count(0)
            expect(references.locator(".inline-preview")).not_to_have_attribute("open", "")
            references.locator("summary").first.click()
            expect(references.locator(".citation")).to_have_count(0)
            expect(panel.get_by_role("button", name="原文引用 1", exact=True)).to_have_count(1)
            expect(panel.get_by_role("button", name="原文引用 2", exact=True)).to_have_count(1)
            expect(panel.get_by_role("button", name="原文引用 3", exact=True)).to_have_count(1)
            open_citation(3, original[0]["sections"][2]["revision_id"])
            page.get_by_role("button", name="原型", exact=True).click()
            add.click(button="right", position={"x": 2, "y": 2})
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            open_citation(1, original[0]["sections"][0]["revision_id"])
            page.get_by_role("button", name="原型", exact=True).click()
            add.click(button="right", position={"x": 2, "y": 2})
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            footer = panel.locator(".knowledge-footer")
            expect(footer.get_by_role("button", name="AI 讲解", exact=True)).to_have_count(0)
            footer.get_by_role("button", name="AI 重新关联", exact=True).click()
            review = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(review.get_by_label("关联方向", exact=True)).to_be_focused()
            assert not calls and links() == original
            review.get_by_role("button", name="取消", exact=True).click()
            expect(review).to_have_count(0)
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
            expect(panel.get_by_role("region", name="AI 功能讲解")).to_contain_text("独立讲解保持原关联")
            assert len(calls) == 1 and links() == original
            expect(footer.get_by_role("button", name="重新讲解", exact=True)).to_have_count(1)
            expect(footer.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
            expect(panel.locator(".assistant-answer-title button")).to_have_count(0)
            panel.get_by_role("tab", name="回答", exact=True).click()
            expect(footer.get_by_role("button", name="编辑知识", exact=True)).to_have_count(0)
            panel.get_by_role("tab", name="AI 讲解", exact=True).click()
            assert len(calls) == 1
            expect(panel.locator(".source-disclosure")).not_to_have_attribute("open", "")
            expect(panel.locator(".source-disclosure .citation")).to_have_count(0)
            open_citation(1, original[0]["sections"][0]["revision_id"])
            page.get_by_role("button", name="原型", exact=True).click()
            add.click(button="right", position={"x": 2, "y": 2})
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            footer.get_by_role("button", name="AI 重新关联", exact=True).click()
            expect(panel.get_by_role("tab", name="文档", exact=True)).to_have_attribute("aria-selected", "true")
            review.get_by_label("关联方向", exact=True).fill("只调整本次预览的方向")
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(review.get_by_role("heading", name="准备替换的标题", exact=True)).to_be_visible(timeout=15000)
            expect(review.locator(".association-diff-deleted code")).not_to_have_count(0)
            assert links() == original
            review.get_by_role("tab", name="Markdown 预览", exact=True).click()
            expect(review.locator("table th")).to_have_text(["检查", "预览结果"])
            assert len(calls) == 2 and links() == original
            review.get_by_role("button", name="取消", exact=True).click()
            assert links() == original
            with page.expect_popup() as opened:
                panel.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            popup.on("pageerror", lambda error: errors.append(str(error)))
            popup.set_viewport_size({"width": 1000, "height": 850})
            detached = popup.get_by_role("complementary", name="知识助手")
            detached.locator(".knowledge-footer").get_by_role("button", name="AI 重新关联", exact=True).click()
            preview = popup.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(page.get_by_role("dialog", name="AI 关联预览", exact=True)).to_have_count(0)
            preview.get_by_label("关联方向", exact=True).fill("确认保存同一份Markdown预览")
            preview.get_by_role("button", name="生成关联预览", exact=True).click()
            preview.get_by_role("tab", name="Markdown 预览", exact=True).click()
            expect(preview.locator("table th")).to_have_text(["检查", "预览结果"], timeout=15000)
            assert len(calls) == 3 and links() == original
            screenshot_dir = Path("output/playwright/assistant-visibility-20261116")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            popup.screenshot(path=str(screenshot_dir / "prototype-association-preview.png"))
            preview.get_by_role("button", name="确认保存关联", exact=True).click()
            expect(preview).to_have_count(0, timeout=15000)
            expect(detached.get_by_role("region", name="关联说明").locator("table th")).to_have_text(
                ["检查", "预览结果"]
            )
            assert len(calls) == 3
            current = links()[0]
            assert current["id"] == original[0]["id"]
            assert current["point_revision_id"] != original[0]["point_revision_id"]
            assert "确认后才替换" in current["summary"]
            assert not errors
        except Exception:
            with suppress(Exception):
                print("Runtime context events", page.evaluate("window.runtimeEvents"))
                page.screenshot(path=str(tmp_path / "association-preview-failure.png"), timeout=3000)
                page.context.tracing.stop(path=str(tmp_path / "association-preview-trace.zip"))
            raise
        else:
            page.context.tracing.stop()
        finally:
            browser.close()
