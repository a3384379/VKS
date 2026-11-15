"""Source inventory identities must resolve on actual prototype gestures."""

import json

from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.bulk_association import BulkAssociationService

from tests.interactive import test_bulk_association
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_bulk_association.materials


def test_preassociated_ids_labels_and_table_rows_match_runtime_clicks(materials, tmp_path, monkeypatch):
    source = test_bulk_association.SOURCE.replace(
        '<button id="add">新增收费项</button>', '<button id="1a:b"><span>新增</span><span>收费项</span></button>'
    )
    source = source.replace(
        '<label for="memo">备注</label><textarea id="memo">DO_NOT_SEND_TEXTAREA_VALUE</textarea>',
        '<label>备注<textarea id="memo">DO_NOT_SEND_TEXTAREA_VALUE</textarea></label>',
    )
    service, owner, _, pid, aid, _, _, calls, model = test_bulk_association.setup(materials, monkeypatch, source)

    def labeled(config, body):
        answer = model(config, body)
        data = calls[-1]
        content = answer["output"][0]["content"][0]
        text = json.loads(content["text"])
        text["title"] = "说明：" + data["control"]["label"]
        content["text"] = json.dumps(text)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", labeled)
    manager = BulkAssociationService(service)
    job = test_bulk_association.enqueue(manager, owner, pid, aid)
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["result"]["saved"] == 7
    call_count = len(calls)
    web = materials[0]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            frame = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(frame.get_by_role("button", name="新增收费项", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            for selector, label in [
                ('[id="1a:b"]', "新增收费项"),
                ("#name", "名称"),
                ("#kind", "类别"),
                ("#memo", "备注"),
                ("#edit1", "编辑收费项"),
                ("#edit2", "编辑收费项"),
            ]:
                frame.locator(selector).click()
                if selector == "#kind":
                    page.keyboard.press("Escape")
                expect(panel.locator(".knowledge-context")).to_contain_text(label)
                expect(panel.get_by_role("heading", name="说明：" + label, exact=True)).to_be_visible()
            frame.locator("#name").fill("PERSONAL_TYPED_VALUE")
            assert len(calls) == call_count
        finally:
            browser.close()
