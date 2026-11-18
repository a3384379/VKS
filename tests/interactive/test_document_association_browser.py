"""Chapter association chooses a module and previews before one exact save."""

import json

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway

from tests.interactive import test_assistant, test_knowledge
from tests.interactive.test_native_documents_browser import native_studio
from tests.interactive.test_reassociation_preview import bindings
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_document_chapter_selects_module_preview_cancel_and_confirm(assistant, tmp_path, monkeypatch):
    (web, _service, _owner, pid, aid, grant, package, section, _), _body, calls = assistant
    base = test_assistant.root(pid)
    modules = web.get(
        base + "/reassociate/targets", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "module-list")
    ).json()["data"]["items"]
    assert modules
    target = modules[0]["request"]
    binding = {
        **test_knowledge.form(aid, package, section),
        "package_id": target["package_id"],
        "target": target["target"],
    }
    binding["point"]["summary"] = "原模块规则应当保留。"
    saved = web.post(
        base + "/bindings",
        json=binding,
        headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "module-baseline"),
    )
    assert saved.status_code == 200, saved.text
    old = bindings(web, pid, aid, grant)
    submitted = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        submitted.append(data)
        assert data["control"] == target["target"]
        assert data["context"]["scope"] == "module"
        assert data["existing_binding"]["summary"] == "原模块规则应当保留。"
        assert data["additional_section_revision_ids"]
        response = test_assistant.response(data["additional_section_revision_ids"][0])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["title"] = "目标模块补全后的说明"
        answer["explanation"] = "原模块规则应当保留。\n\n## 新增章节补充\n\n新的适用规则。[1]"
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.add_init_script("if(top===window)localStorage.setItem('vks.studio.auto-associate','off')")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            source.get_by_role("button", name="Add", exact=True).click()
            # Keep the old prototype target in this same React session.
            page.get_by_role("button", name="文档", exact=True).click()
            catalog = test_knowledge.current(web, pid, aid, grant)
            document = (
                next(row for row in catalog["documents"] if row["revision_id"] == section["document_revision_id"])
                if "document_revision_id" in section
                else catalog["documents"][0]
            )
            page.evaluate(
                "value => window.location.hash = value",
                f"project={pid}&analysis={aid}&view=documents&doc={document['revision_id']}&reader=standard",
            )
            article = page.locator(".document-reader")
            action = article.get_by_role("button", name="关联操作", exact=True).first
            expect(action).to_be_visible(timeout=20000)
            action.click()
            picker = page.get_by_role("dialog", name="选择关联模块", exact=True)
            expect(picker).to_be_visible()
            expect(picker.get_by_label("搜索目标模块", exact=True)).to_be_focused()
            expect(picker.get_by_role("button", name="下一步：关联预览", exact=True)).to_be_disabled()
            assert not calls and not submitted and bindings(web, pid, aid, grant) == old
            picker.get_by_label("搜索目标模块", exact=True).fill("no-such-module-value")
            expect(picker).to_contain_text("没有可关联的模块")
            picker.get_by_label("搜索目标模块", exact=True).fill(modules[0]["module_label"])
            expect(picker.get_by_role("radio").first).to_be_visible()
            picker.get_by_role("radio").first.check()
            assert picker.locator(".document-association-modules label").first.bounding_box()["height"] < 100
            picker.get_by_role("button", name="下一步：关联预览", exact=True).click()
            review = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(picker).to_have_count(0)
            expect(review).to_contain_text("新增关联章节")
            expect(review).to_contain_text(modules[0]["module_label"])
            expect(review.get_by_label("关联方向", exact=True)).to_have_value("")
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(review.get_by_role("tab", name="新旧对比", exact=True)).to_be_visible(timeout=15000)
            expect(review).to_contain_text("原模块规则应当保留")
            assert len(submitted) == 1 and bindings(web, pid, aid, grant) == old
            review.get_by_role("button", name="取消", exact=True).click()
            assert bindings(web, pid, aid, grant) == old
            action.click()
            picker.get_by_role("radio").first.check()
            picker.get_by_role("button", name="下一步：关联预览", exact=True).click()
            review.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(review.get_by_role("button", name="确认保存关联", exact=True)).to_be_enabled(timeout=15000)
            assert len(submitted) == 2 and bindings(web, pid, aid, grant) == old
            review.get_by_role("button", name="确认保存关联", exact=True).click()
            expect(review).to_have_count(0, timeout=15000)
            current = bindings(web, pid, aid, grant)[0]
            assert current["target"] == target["target"] and current["summary"].endswith("新的适用规则。[1]")
            assert submitted[-1]["additional_section_revision_ids"][0] in {
                row["revision_id"] for row in current["sections"]
            }
            assert len(submitted) == 2 and not calls and not errors
        finally:
            browser.close()
