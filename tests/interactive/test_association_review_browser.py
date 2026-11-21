"""Review candidates with bounded rendering; only explicit confirmation writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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


def test_migration_grays_association_and_restores_it_after_stop(materials, tmp_path, monkeypatch):
    web, pid, aid = setup(materials)
    status = {"migrating": True}
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        page.route("**/interactive/reassociate/status?*", lambda route: route.fulfill(json={"data": status}))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            original = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            add = original.get_by_role("button", name="新增收费项", exact=True)
            add.click(button="right", position={"x": 2, "y": 2}, timeout=30000)
            expect(page.get_by_role("menuitem", name="AI 讲解此功能", exact=True)).to_be_disabled()
            expect(page.get_by_role("menuitem", name="重新检索与讲解", exact=True)).to_be_disabled()
            page.get_by_role("menuitem", name="查看关联文档", exact=True).click()
            assistant = page.get_by_role("complementary", name="知识助手")
            expect(assistant.get_by_text("必须保留的人工说明", exact=True)).to_be_visible()
            explain = assistant.get_by_role("tab", name="AI 讲解", exact=True)
            reassociate = assistant.get_by_role("button", name="AI 重新关联", exact=True)
            expect(explain).to_be_disabled()
            expect(reassociate).to_be_disabled()
            status["migrating"] = False
            expect(explain).to_be_enabled(timeout=8000)
            expect(reassociate).to_be_enabled()
            status["migrating"] = True
            page.get_by_role("button", name="关联管理", exact=True).click()
            regenerate = page.get_by_role("button", name="重新关联全部", exact=True)
            missing = page.get_by_role("button", name="补齐未关联", exact=True)
            expect(regenerate).to_be_disabled()
            expect(missing).to_be_disabled()
            migration = page.get_by_role("status", name="资料引用迁移")
            expect(migration).to_have_text("当前草稿正在迁移资料引用，可在资料管理查看进度。")
            expect(migration).to_be_visible()
            status["migrating"] = False
            expect(regenerate).to_be_enabled(timeout=8000)
            expect(missing).to_be_enabled()
            regenerate.click()
            dialog = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            generate = dialog.get_by_role("button", name="生成关联预览", exact=True)
            expect(generate).to_be_enabled()
            status["migrating"] = True
            expect(generate).to_be_disabled(timeout=8000)
        finally:
            browser.close()


def batch_results(route, rows, job_id, state="succeeded", processed=None, timing=None):
    query = route.request.post_data_json
    offset, limit = query["offset"], query["limit"]
    status = query.get("status", "all")
    needle = query.get("q", "").strip().casefold()
    remaining = [r for r in rows if r["item_id"] not in query.get("excluded_item_ids", [])]
    remaining = [
        {**r, "status": "insufficient", "preview_id": None, "can_confirm": False}
        if r["item_id"] in query.get("insufficient_item_ids", [])
        else r
        for r in remaining
    ]
    matching = [
        r
        for r in remaining
        if (status == "all" or status == r["status"] or status == "needs_attention" and not r["can_confirm"])
        and (
            not needle
            or needle in " ".join([r["label"], r.get("module_label", ""), r["request"]["target"]["route"]]).casefold()
        )
    ]
    route.fulfill(
        json={
            "data": {
                "job_id": job_id,
                "state": state,
                "processed": len(rows) if processed is None else processed,
                "total": len(rows),
                "timing": timing or {"started_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:00:15Z"},
                "matched_total": len(matching),
                "unresolved_total": sum(not r["can_confirm"] for r in remaining),
                "items": matching[offset : offset + limit],
                "next_offset": offset + limit if offset + limit < len(matching) else None,
            }
        }
    )


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
            return {"item_id": f"item-{index}", "label": f"模块{index:04}", "module_label": "业务目录", "status": "generated", "preview_id": "regenerated-0" if index == 0 and calls["regenerate"] else f"preview-{index}", "can_confirm": True, "request": target}

        def generate(route):
            calls["generate"].append(route.request.post_data_json)
            route.fulfill(json={"data": {"job_id": "review-job", "total": total}})

        run_started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        batch_state = {
            "state": "running", "processed": 0, "timing": {"started_at": run_started_at.isoformat()},
        }

        def completed(count, seconds):
            batch_state.update(processed=count, timing={
                "started_at": run_started_at.isoformat(),
                "updated_at": (run_started_at + timedelta(seconds=seconds)).isoformat(),
            })

        def listing(route):
            batch_results(route, [row(i) for i in range(total)], "review-job", **batch_state)

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
        page.route(base + "/timing", lambda r: r.fulfill(json={"data": {"single_seconds": 27, "batch_seconds_per_item": 2}}))
        page.route(base + "/batch-preview", generate)
        page.route(base + "/batch-preview/review-job/results", listing)
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
            expect(dialog.locator(".task-timing")).to_contain_text("参考历史")
            completed(5, 4)
            expect(dialog.get_by_role("status").filter(has_text=f"已处理 5 / {total} 项")).to_be_visible()
            expect(dialog.locator(".task-timing")).to_contain_text("参考历史")
            completed(9, 24)
            expect(dialog.get_by_role("status").filter(has_text=f"已处理 9 / {total} 项")).to_be_visible()
            expect(dialog.locator(".task-timing")).not_to_contain_text("参考历史")
            expect(dialog.locator(".task-timing")).to_contain_text("预计剩余 约")
            batch_state.update(state="succeeded", processed=total, timing=None)
            expect(dialog.locator(".task-timing")).to_have_text("已运行 15 秒 · 预计剩余 0 秒")
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
            detail.get_by_label("本项关联方向", exact=True).fill("第二个明确方向")
            detail.get_by_role("button", name="按本项方向重新生成", exact=True).click()
            expect(detail.get_by_text("本次重新生成已完成，模型返回了与上一版相同的标题和正文。")).to_be_visible()
            assert calls["regenerate"][1]["instruction"] == "第二个明确方向"
            assert calls["regenerate"][1]["base_preview_id"] == "regenerated-0"
            detail.get_by_role("button", name="编辑预览", exact=True).click()
            detail.get_by_label("关联标题", exact=True).fill("人工核对标题")
            detail.get_by_label("Markdown 正文", exact=True).fill("人工核对后的规则。[1]")
            expect(dialog.get_by_role("button", name="整批保存", exact=True)).to_be_disabled()
            detail.get_by_role("button", name="应用到对比", exact=True).click()
            expect(detail.locator(".association-diff-inserted code", has_text="人工核对后的规则")).to_be_visible()
            nav = dialog.get_by_role("navigation", name="预览列表分页", exact=True)
            nav.get_by_role("button", name="下一页", exact=True).click()
            expect(dialog.get_by_role("button", name="查看关联预览：模块0026", exact=True)).to_be_visible()
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
            assert calls["confirm"][0]["edits"] == [{
                "preview_id": "regenerated-0", "point": {"title": "人工核对标题", "summary": "人工核对后的规则。[1]"},
            }]
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
    held = []
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
            held.append(route)

        page.route("**/interactive/bindings?*", bindings)
        page.route(base + "/targets?*", targets)
        def batch_preview(route):
            batches.append(route.request.post_data_json)
            route.fulfill(json={"data": {"job_id": "selected-batch", "total": 2}})

        def batch_page(route):
            batch_results(route, [{"item_id": str(index), "label": f"已选{index}", "status": "generated", "preview_id": f"selected-{index}", "can_confirm": True, "request": item} for index, item in enumerate(batches[0]["items"])], "selected-batch")

        page.route(base, record)
        page.route(base + "/timing", lambda r: r.fulfill(json={"data": {"single_seconds": 27, "batch_seconds_per_item": None}}))
        page.route(base + "/batch-preview", batch_preview)
        page.route(base + "/batch-preview/selected-batch/results", batch_page)
        page.route(base + "/previews/*", lambda route: route.fulfill(json={"data": {"title": "预览", "explanation": "保留原关联", "sections": [], "unknowns": [], "confidence": .9, "preview_id": route.request.url.rsplit("/", 1)[-1], "can_confirm": True, "saved": False, "selection_version": 10}}))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            manager = page.get_by_role("region", name="关联管理", exact=True)
            expect(manager.locator(".binding-list-caption")).to_contain_text("26 条关联")
            report = page.get_by_role("complementary", name="项目 Token 用量", exact=True)
            expect(report).to_have_attribute("aria-busy", "false")
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
            expect(dialog.locator(".task-timing")).to_contain_text("参考历史")
            held[0].fulfill(json={"data": {"title": "精确预览", "explanation": "**未保存的建议**", "sections": [], "unknowns": [], "confidence": .95, "preview_id": "exact-preview", "can_confirm": True, "saved": False, "selection_version": 10}})
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


def test_batch_filters_find_later_failures_and_retries_can_overlap(materials, tmp_path, monkeypatch):
    web, pid, aid = setup(materials)
    in_flight, confirms = {}, []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        base = f"**/api/v3/projects/{pid}/interactive/reassociate"
        job_id = "retry-batch"
        rows = []
        for index in range(65):
            status = "failed" if index in (31, 59) else "insufficient" if index == 64 else "generated"
            rows.append({
                "item_id": str(index), "label": f"模块{index:02d}", "module_label": "智能计费",
                "status": status, "can_confirm": status == "generated",
                "preview_id": f"preview-{index}" if status == "generated" else None,
                "request": {"analysis_set_id": aid, "package_id": "fixture",
                            "target": {"route": f"/billing/{index}", "selector": "body", "label": f"模块{index:02d}", "frame_path": []},
                            "context": {"scope": "module", "page_title": "智能计费"}},
            })

        def preview(identifier, confirmable=True):
            return {"title": f"生成 {identifier}", "explanation": "预览正文", "sections": [], "unknowns": [],
                    "confidence": .95 if confirmable else 0, "preview_id": identifier if confirmable else None,
                    "can_confirm": confirmable, "saved": False, "selection_version": 10}

        def hold_retry(route):
            request = route.request.post_data_json
            assert request["batch_id"] == job_id
            in_flight[request["item_id"]] = (route, request)

        def save(route):
            confirms.append(route.request.post_data_json)
            route.fulfill(json={"data": {"saved": True, "count": len(confirms[-1]["preview_ids"]), "binding_ids": []}})

        page.route(base + "/targets?*", empty_targets)
        page.route(base + "/batch-preview", lambda r: r.fulfill(json={"data": {"job_id": job_id, "total": len(rows)}}))
        page.route(base + f"/batch-preview/{job_id}/results", lambda r: batch_results(r, rows, job_id))
        page.route(base + "/previews/*", lambda r: r.fulfill(json={"data": preview(r.request.url.rsplit("/", 1)[-1])}))
        page.route(base + "/batch-confirm", save)
        page.route(base, hold_retry)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="重新关联全部", exact=True).click()
            dialog = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            dialog.get_by_role("button", name="生成关联预览", exact=True).click()
            status_filter = dialog.get_by_label("预览状态", exact=True)
            search = dialog.get_by_label("搜索关联预览", exact=True)
            save_button = dialog.get_by_role("button", name="整批保存", exact=True)
            expect(dialog.locator(".association-review-list li")).to_have_count(25)
            expect(save_button).to_be_disabled()
            status_filter.select_option("failed")
            expect(dialog.locator(".association-review-list li")).to_have_count(2)
            search.fill("/billing/59")
            expect(dialog.locator(".association-review-list li")).to_have_count(1)
            expect(dialog.get_by_role("button", name="查看关联预览：模块59", exact=True)).to_be_visible()
            dialog.get_by_role("button", name="查看 3 项需处理", exact=True).click()
            expect(search).to_have_value("")
            expect(dialog.locator(".association-review-list li")).to_have_count(3)

            detail = dialog.get_by_role("article", name="本项完整关联预览")
            direction = detail.get_by_role("textbox", name="本项关联方向", exact=True)
            for index in (31, 59):
                dialog.get_by_role("button", name=f"查看关联预览：模块{index}", exact=True).click()
                direction.fill(f"方向-{index}")
                retry = detail.get_by_role("button", name="重试本项", exact=True)
                expect(retry).to_be_enabled()
                retry.click()
                expect(detail.get_by_role("button", name="正在重新生成…", exact=True)).to_be_disabled()
            assert [in_flight[str(i)][1]["instruction"] for i in (31, 59)] == ["方向-31", "方向-59"]
            rows[59].update(status="generated", preview_id="retry-59", can_confirm=True)
            in_flight["59"][0].fulfill(json={"data": preview("retry-59")})
            expect(dialog.get_by_role("button", name="查看关联预览：模块59", exact=True)).to_have_count(0)
            expect(save_button).to_be_disabled()
            dialog.get_by_role("button", name="查看关联预览：模块31", exact=True).click()
            expect(detail.get_by_role("button", name="正在重新生成…", exact=True)).to_be_disabled()
            in_flight["31"][0].fulfill(json={"data": preview("retry-31", confirmable=False)})
            status_filter.select_option("insufficient")
            expect(dialog.locator(".association-review-list li")).to_have_count(2)
            del in_flight["31"]
            dialog.get_by_label("统一重试方向", exact=True).fill("按公式与原文重试")
            dialog.get_by_role("button", name="批量重试未完成项", exact=True).click()
            page.wait_for_function("() => document.body.innerText.includes('已重试 0 / 2 项')")
            for _ in range(10):
                if {"31", "64"} <= in_flight.keys():
                    break
                page.wait_for_timeout(40)
            assert in_flight["31"][1]["instruction"] == in_flight["64"][1]["instruction"] == "按公式与原文重试"
            rows[31].update(status="generated", preview_id="retry-31-final", can_confirm=True)
            in_flight["31"][0].fulfill(json={"data": preview("retry-31-final")})
            in_flight["64"][0].fulfill(json={"data": preview("retry-64", confirmable=False)})
            expect(dialog.get_by_text("已重试 2 / 2 项 · 1 项可保存", exact=True)).to_be_visible()
            dialog.get_by_role("button", name="查看关联预览：模块64", exact=True).click()
            detail.get_by_role("button", name="从本次移除", exact=True).click()
            expect(dialog.locator(".association-review-list li")).to_have_count(0)
            expect(save_button).to_be_enabled()
            save_button.click()
            expect(dialog).to_have_count(0)
            assert confirms == [{"job_id": job_id, "preview_ids": [rows[i]["preview_id"] for i in range(64)]}]
        finally:
            browser.close()


def test_manager_filters_controls_and_submits_all_matching_pages(materials, tmp_path, monkeypatch):
    web, pid, aid = setup(materials)
    batches = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        base = f"**/api/v3/projects/{pid}/interactive/reassociate"

        def catalog(route):
            response = route.fetch()
            payload = response.json()
            for package in payload["data"]["packages"]:
                if package["kind"] == "prototype":
                    package["format"] = "charge-prototype"
            route.fulfill(response=response, json=payload)

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            items = []
            for index in range(1, 16):
                row = deepcopy(original)
                row.update(id=f"binding-{index}", title=f"账务功能{index:02d}", scope="module")
                row["target"].update(route=f"/actual/账务/billing-{index}.html", selector="body", label=row["title"])
                items.append(row)
            for identifier, scope, selector, subpath, excluded in (
                ("menu", "control", "#businessMenu > button:nth-of-type(1)", "portal.html", False),
                ("other", "module", "body", "other.html", False),
                ("component", "module", "body", "components/refund.html", False),
                ("control", "control", "#save", "billing-1.html", False),
                ("excluded", "module", "body", "removed.html", True),
            ):
                row = deepcopy(original)
                row.update(
                    id=identifier,
                    title=("其他业务" if identifier == "other" else f"账务-{identifier}"),
                    scope=scope,
                    excluded=excluded,
                )
                row["target"].update(route=f"/actual/账务/{subpath}", selector=selector, label=row["title"])
                items.append(row)
            payload["data"] = items
            route.fulfill(response=response, json=payload)

        def targets(route):
            params = parse_qs(urlsplit(route.request.url).query)
            offset = int(params["offset"][0])
            identifier = "page" if offset == 0 else "control"
            scope = "module" if offset == 0 else "control"
            request = {
                "package_id": "pending-package",
                "target": {
                    "route": "/actual/账务/pending.html",
                    "selector": "body" if offset == 0 else "#save",
                    "label": f"账务待关联-{identifier}",
                    "frame_path": ["#runtime"],
                    "state": "ready",
                },
                "context": {"scope": scope, "page_title": "账务待关联"},
            }
            route.fulfill(
                json={
                    "data": {
                        "items": [{"item_id": identifier, "label": request["target"]["label"], "request": request}],
                        "total": 2,
                        "next_offset": 1 if offset == 0 else None,
                    }
                }
            )

        def batch(route):
            request = route.request.post_data_json
            batches.append(request)
            route.fulfill(json={"data": {"job_id": "matched-batch", "total": len(request["items"])}})

        page.route("**/interactive/catalog?*", catalog)
        page.route("**/interactive/bindings?*", bindings)
        page.route(base + "/targets?*", targets)
        page.route(base + "/batch-preview", batch)
        page.route(
            base + "/batch-preview/matched-batch/results",
            lambda route: route.fulfill(
                json={
                    "data": {
                        "job_id": "matched-batch",
                        "state": "cancelled",
                        "items": [],
                        "total": len(batches[0]["items"]),
                        "processed": 0,
                        "matched_total": 0,
                        "unresolved_total": 0,
                        "next_offset": None,
                    }
                }
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            manager = page.get_by_role("region", name="关联管理", exact=True)
            category = manager.get_by_label("控件类别", exact=True)
            keyword = manager.get_by_label("搜索关联", exact=True)
            selection = manager.get_by_role("status").filter(has_text="已选")
            caption = manager.locator(".binding-list-caption")
            counts = manager.get_by_role("group", name="关联状态筛选").locator("button > span")
            expect(caption).to_contain_text("21 条关联")
            expect(counts).to_have_text(["21", "2", "19", "1"])
            category.select_option("component")
            expect(caption).to_contain_text("1 条关联")
            expect(counts).to_have_text(["1", "0", "1", "0"])
            expect(manager.locator("tbody")).to_contain_text("组件页面")
            category.select_option("control")
            expect(caption).to_contain_text("2 条关联")
            expect(manager.locator("tbody")).to_contain_text("页面控件")
            category.select_option("menu")
            expect(caption).to_contain_text("18 条关联")
            expect(counts).to_have_text(["18", "1", "17", "1"])
            keyword.fill("账务-")
            expect(caption).to_contain_text("1 条关联")
            expect(counts).to_have_text(["1", "0", "1", "1"])
            expect(manager.locator("tbody")).to_contain_text("账务-menu")
            keyword.fill("")
            current = manager.get_by_role("checkbox", name="选择当前页", exact=True)
            all_matches = manager.get_by_role("checkbox", name="选择全部筛选结果", exact=True)
            current.check()
            expect(selection).to_contain_text("已选 10 项")
            expect(all_matches).to_have_js_property("indeterminate", True)
            nav = manager.get_by_role("navigation", name="关联列表分页", exact=True)
            nav.get_by_role("button", name="下一页", exact=True).click()
            expect(current).not_to_be_checked()
            current.check()
            expect(selection).to_contain_text("已选 18 项")
            current.uncheck()
            expect(selection).to_contain_text("已选 10 项")
            all_matches.check()
            expect(selection).to_contain_text("已选 18 项")
            keyword.fill("账务功能")
            expect(selection).to_contain_text("已选 0 项")
            expect(caption).to_contain_text("15 条关联")
            expect(counts).to_have_text(["15", "0", "15", "0"])
            all_matches.check()
            expect(selection).to_contain_text("已选 15 项")
            category.select_option("control")
            expect(selection).to_contain_text("已选 0 项")
            expect(caption).to_contain_text("1 条关联")
            keyword.fill("没有匹配的控件")
            expect(all_matches).to_be_disabled()
            expect(counts).to_have_text(["0", "0", "0", "0"])
            category.select_option("menu")
            keyword.fill("")
            all_matches.check()
            nav.get_by_role("button", name="下一页", exact=True).click()
            manager.get_by_role("checkbox", name="选择关联：其他业务", exact=True).uncheck()
            expect(selection).to_contain_text("已选 17 项")
            manager.get_by_role("button", name="批量 AI 关联", exact=True).click()
            dialog = page.get_by_role("dialog", name="AI 关联预览", exact=True)
            expect(dialog).to_contain_text("已选择 17 个模块或操作")
            assert not batches
            with page.expect_response("**/interactive/reassociate/batch-preview"):
                dialog.get_by_role("button", name="生成关联预览", exact=True).click()
            items = batches[0]["items"]
            assert {x["binding_id"] for x in items if x.get("binding_id")} == {
                "menu",
                *(f"binding-{i}" for i in range(1, 16)),
            }
            assert next(x for x in items if x.get("binding_id") == "menu")["context"]["scope"] == "control"
            pending = next(x for x in items if not x.get("binding_id"))
            assert pending["target"]["state"] == "ready"
            assert pending["target"]["frame_path"] == ["#runtime"]
            assert pending["context"]["scope"] == "module"
        finally:
            browser.close()
