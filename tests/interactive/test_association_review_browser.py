"""Review candidates with bounded rendering; only explicit confirmation writes."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def setup(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    return web, pid, aid


def empty_targets(route):
    route.fulfill(json={"data": {"items": [], "total": 0, "next_offset": None}})


def bounds(locator):
    box = locator.bounding_box()
    return tuple(round(box[key]) for key in ("x", "y", "width", "height"))


def test_batch_review_paged_markdown_regenerate_remove_and_atomic_save_retry(materials, tmp_path, monkeypatch):
    web, pid, aid = setup(materials)
    calls = {"generate": [], "regenerate": [], "confirm": [], "writes": []}
    total = 1001
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        base = f"**/api/v3/projects/{pid}/interactive/reassociate"
        target = {"analysis_set_id": aid, "package_id": "fixture-package", "target": {"route": "/", "frame_path": [], "selector": "body", "label": "模块"}, "context": {"scope": "module", "page_title": "业务模块"}}

        def row(index):
            return {"item_id": f"item-{index}", "label": f"模块{index:04}", "module_label": "业务目录", "status": "generated", "preview_id": f"preview-{index}", "can_confirm": True, "request": target}

        def generate(route):
            calls["generate"].append(route.request.post_data_json)
            route.fulfill(json={"data": {"job_id": "review-job", "total": total}})

        def listing(route):
            query = parse_qs(urlsplit(route.request.url).query)
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            route.fulfill(json={"data": {"job_id": "review-job", "state": "succeeded", "processed": total, "total": total, "items": [row(i) for i in range(offset, min(total, offset + limit))], "next_offset": offset + limit if offset + limit < total else None}})

        def preview_value(identifier):
            long = identifier == "preview-0"
            return {"title": f"生成标题 {identifier}", "explanation": "**完整预览正文** [1]\n\n|规则|结果|\n|---|---|\n|条件|保留|\n\n```json\n{\"full\":true}\n```\n" + ("\n\n长说明" * 150 if long else "短说明"), "sections": [{"revision_id": "section-exact", "document_revision_id": "document-exact", "document_name": "准确文档", "name": "准确章节", "content": "# 准确原文\n\n**保留原文内容**"}], "unknowns": [], "confidence": .95, "baseline": {"title": "已保存标题", "explanation": "原有规则\n\n旧公式：月单价 × 月数\n", "binding_version": 3, "point_revision_id": "old-point", "sections": []}, "preview_id": identifier, "can_confirm": True, "saved": False, "selection_version": 10}

        def preview(route):
            route.fulfill(json={"data": preview_value(route.request.url.rsplit("/", 1)[-1])})

        def regenerate(route):
            calls["regenerate"].append(route.request.post_data_json)
            route.fulfill(json={"data": preview_value("regenerated-0")})

        def confirm(route):
            calls["confirm"].append(route.request.post_data_json)
            if len(calls["confirm"]) == 1:
                route.fulfill(status=412, json={"error": {"message": "资料已变化，整批尚未保存", "code": "VKS_VERSION_CONFLICT"}})
            else:
                route.fulfill(json={"data": {"saved": True, "count": total - 1, "selection_version": 11, "binding_ids": []}})

        def forbid_write(route):
            if route.request.method != "GET":
                calls["writes"].append(route.request.url)
                route.fulfill(status=500, json={"error": {"message": "unexpected binding mutation"}})
            else:
                route.continue_()

        page.route("**/interactive/bindings**", forbid_write)
        page.route(base + "/targets?*", empty_targets)
        page.route(base + "/batch-preview", generate)
        page.route(base + "/batch-preview/review-job?*", listing)
        page.route(base + "/previews/*", preview)
        page.route(base, regenerate)
        page.route(base + "/batch-confirm", confirm)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="重新关联全部", exact=True).click()
            dialog = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(dialog).to_be_visible()
            initial = bounds(dialog)
            assert not calls["generate"] and not calls["confirm"]
            dialog.get_by_label("统一关联方向", exact=True).fill("保留完整规则和准确引用")
            dialog.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(dialog.get_by_role("button", name="查看关联预览：模块0000", exact=True)).to_be_visible()
            expect(dialog.locator(".association-review-list li")).to_have_count(25)
            detail = dialog.get_by_role("article", name="本项完整关联预览")
            expect(detail.get_by_text("生成标题 preview-0", exact=True)).to_be_visible()
            comparison = detail.get_by_role("region", name="关联文档新旧对比", exact=True)
            expect(comparison.get_by_role("tab", name="新旧对比", exact=True)).to_have_attribute("aria-selected", "true")
            expect(comparison.locator(".association-diff-deleted code", has_text="旧公式")).to_be_visible()
            expect(comparison.locator(".association-diff-inserted code", has_text="完整预览正文")).to_be_visible()
            assert not calls["confirm"] and not calls["writes"]
            comparison.get_by_role("tab", name="Markdown 预览", exact=True).click()
            expect(detail.locator("strong", has_text="完整预览正文")).to_be_visible()
            expect(detail.locator("table")).to_have_count(1)
            expect(detail.locator("pre code")).to_have_text('{"full":true}')
            assert bounds(dialog) == initial
            frame = bounds(dialog.locator(".association-review-workspace"))
            detail.get_by_role("button", name="查看预览引用原文 1", exact=True).click()
            source = page.get_by_role("dialog", name="关联预览引用原文", exact=True)
            expect(source.locator("strong", has_text="保留原文内容")).to_be_visible()
            source.get_by_role("button", name="关闭预览引用原文", exact=True).click()
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="查看关联预览：模块0001", exact=True).click()
            expect(detail.get_by_text("生成标题 preview-1", exact=True)).to_be_visible()
            assert bounds(dialog.locator(".association-review-workspace")) == frame
            detail.get_by_role("button", name="从本次移除", exact=True).click()
            expect(dialog.get_by_role("button", name="查看关联预览：模块0001", exact=True)).to_have_count(0)
            dialog.get_by_role("button", name="查看关联预览：模块0000", exact=True).click()
            detail.get_by_label("本项关联方向", exact=True).fill("本项新方向")
            detail.get_by_role("button", name="按本项方向重新生成", exact=True).click()
            expect(detail.get_by_text("生成标题 regenerated-0", exact=True)).to_be_visible()
            assert calls["regenerate"][0]["batch_id"] == "review-job"
            assert calls["regenerate"][0]["item_id"] == "item-0"
            assert calls["regenerate"][0]["base_preview_id"] == "preview-0"
            nav = dialog.get_by_role("navigation", name="预览列表分页", exact=True)
            nav.get_by_role("button", name="下一页", exact=True).click()
            expect(dialog.get_by_role("button", name="查看关联预览：模块0025", exact=True)).to_be_visible()
            assert bounds(dialog.locator(".association-review-workspace")) == frame
            page.set_viewport_size({"width": 390, "height": 720})
            assert dialog.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            expect(dialog.get_by_role("button", name="整批保存", exact=True)).to_be_visible()
            dialog.get_by_role("button", name="整批保存", exact=True).click()
            expect(dialog.get_by_role("alert")).to_have_text("资料已变化，整批尚未保存")
            expect(dialog).to_be_visible()
            assert len(calls["confirm"]) == 1
            ids = calls["confirm"][0]["preview_ids"]
            assert len(ids) == total - 1 and "regenerated-0" in ids and "preview-1" not in ids and "preview-2" in ids
            dialog.get_by_role("button", name="整批保存", exact=True).click()
            expect(dialog).to_have_count(0)
            assert len(calls["confirm"]) == 2
            assert calls["confirm"][1] == calls["confirm"][0]
            assert len(calls["generate"]) == 1 and not calls["writes"] and not errors
        finally:
            browser.close()


def test_manager_cross_page_selection_pending_exact_target_and_cancel(materials, tmp_path, monkeypatch):
    web, pid, aid = setup(materials)
    requests = []
    batches = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        base = f"**/api/v3/projects/{pid}/interactive/reassociate"

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            items = []
            for index in range(1, 26):
                row = deepcopy(original)
                row.update(id=f"binding-{index}", title=f"示例关联{index:02}", summary=("长说明\n\n" * 300 if index == 2 else f"说明{index}"))
                items.append(row)
            payload["data"] = items
            route.fulfill(response=response, json=payload)

        exact = {"package_id": "exact-package", "target": {"route": "/exact-module", "frame_path": ["#runtime"], "selector": "body", "label": "准确未关联模块", "state": "active", "open_steps": []}, "context": {"scope": "module", "page_title": "准确业务标题"}}

        def targets(route):
            route.fulfill(json={"data": {"items": [{"item_id": "exact-target", "label": "准确未关联模块", "module_label": "准确业务标题", "request": exact}], "total": 1, "next_offset": None}})

        def record(route):
            requests.append(route.request.post_data_json)
            route.fulfill(json={"data": {"title": "精确预览", "explanation": "**未保存的建议**", "sections": [], "unknowns": [], "confidence": .95, "preview_id": "exact-preview", "can_confirm": True, "saved": False, "selection_version": 10}})

        page.route("**/interactive/bindings?*", bindings)
        page.route(base + "/targets?*", targets)
        def batch_preview(route):
            batches.append(route.request.post_data_json)
            route.fulfill(json={"data": {"job_id": "selected-batch", "total": 2}})

        def batch_page(route):
            route.fulfill(json={"data": {"job_id": "selected-batch", "state": "succeeded", "processed": 2, "total": 2, "items": [{"item_id": str(index), "label": f"已选{index}", "status": "generated", "preview_id": f"selected-{index}", "can_confirm": True, "request": item} for index, item in enumerate(batches[0]["items"])], "next_offset": None}})

        page.route(base, record)
        page.route(base + "/batch-preview", batch_preview)
        page.route(base + "/batch-preview/selected-batch?*", batch_page)
        page.route(base + "/previews/*", lambda route: route.fulfill(json={"data": {"title": "预览", "explanation": "保留原关联", "sections": [], "unknowns": [], "confidence": .9, "preview_id": route.request.url.rsplit("/", 1)[-1], "can_confirm": True, "saved": False, "selection_version": 10}}))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            manager = page.get_by_role("region", name="关联管理", exact=True)
            expect(manager.locator(".binding-list-caption")).to_contain_text("26 条关联")
            stable_frame = lambda: manager.locator(".binding-workspace").evaluate("el=>[el.offsetLeft,el.offsetTop,el.clientWidth,el.clientHeight]")
            initial = stable_frame()
            manager.get_by_role("button", name="查看关联详情：示例关联02", exact=True).click()
            expect(manager.get_by_role("article", name="关联详情")).to_contain_text("长说明")
            assert stable_frame() == initial
            manager.get_by_label("选择关联：示例关联01", exact=True).check()
            nav = manager.get_by_role("navigation", name="关联列表分页", exact=True)
            nav.get_by_role("button", name="下一页", exact=True).click()
            manager.get_by_label("选择关联：示例关联11", exact=True).check()
            expect(manager.get_by_role("status").filter(has_text="已选")).to_contain_text("已选 2 项")
            assert stable_frame() == initial
            manager.get_by_role("button", name="批量 AI 关联", exact=True).click()
            dialog = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(dialog).to_contain_text("已选择 2 个模块或操作")
            assert not requests
            dialog.get_by_role("button", name="取消", exact=True).click()
            expect(dialog).to_have_count(0)
            manager.get_by_role("button", name="批量 AI 关联", exact=True).click()
            dialog.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(dialog.get_by_role("button", name="查看关联预览：已选0", exact=True)).to_be_visible()
            assert batches[0]["mode"] == "all"
            assert [item["binding_id"] for item in batches[0]["items"]] == ["binding-1", "binding-11"]
            dialog.get_by_role("button", name="取消", exact=True).click()
            expect(dialog).to_have_count(0)
            manager.get_by_label("搜索关联", exact=True).fill("准确未关联模块")
            pending = manager.get_by_role("button", name="AI关联：准确未关联模块", exact=True)
            expect(pending).to_be_visible()
            pending.click()
            expect(dialog.get_by_label("关联方向", exact=True)).to_be_focused()
            dialog.get_by_role("button", name="生成关联预览", exact=True).click()
            expect(dialog.get_by_text("未保存的建议", exact=True)).to_be_visible()
            assert requests[0]["target"] == exact["target"]
            assert requests[0]["context"] == exact["context"]
            dialog.get_by_role("button", name="取消", exact=True).click()
            expect(pending).to_be_visible()
            expect(manager.get_by_role("article", name="关联详情")).to_contain_text("尚未关联知识")
            assert len(requests) == 1
            page.set_viewport_size({"width": 390, "height": 720})
            assert manager.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
        finally:
            browser.close()
