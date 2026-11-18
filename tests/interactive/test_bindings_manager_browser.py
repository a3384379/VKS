"""Compact association list preserves filtering, editing, exclusion and prototype navigation."""

import re
from copy import deepcopy

from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_association_list_detail_search_remove_restore_and_reveal(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _docs = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            row = page.get_by_role("button", name="查看关联详情：新增收费项说明", exact=True)
            expect(row).to_be_visible()
            detail = page.get_by_role("article", name="关联详情")
            expect(detail).to_contain_text("必须保留的人工说明")
            expect(page.get_by_role("button", name="打开知识助手", exact=True)).to_have_count(0)
            summary = page.get_by_role("status", name="关联任务摘要", exact=True)
            expect(summary).to_contain_text("当前无运行任务")
            tasks = page.locator(".batch-task-details")
            expect(tasks).not_to_have_attribute("open", "")
            tasks.locator(":scope > summary").click()
            expect(tasks).to_have_attribute("open", "")
            expect(tasks).to_contain_text("已发现 2")
            expect(tasks).to_contain_text("人工维护 1")
            tasks.locator(":scope > summary").click()
            expect(tasks).not_to_have_attribute("open", "")
            expect(page.locator(".binding-list-panel .markdown")).to_have_count(0)
            page.get_by_label("搜索关联", exact=True).fill("missing-test-value")
            expect(row).to_have_count(0)
            expect(page.get_by_text("没有符合条件的关联，可调整筛选或搜索内容。", exact=True)).to_be_visible()
            page.get_by_label("搜索关联", exact=True).fill("人工说明")
            expect(row).to_be_visible()
            page.get_by_label("搜索关联", exact=True).fill("")
            detail.get_by_role("button", name="编辑", exact=True).click()
            assistant = page.get_by_role("complementary", name="知识助手")
            expect(assistant.get_by_label("知识标题", exact=True)).to_have_value("新增收费项说明")
            # All three dismissal paths close the editor without saving.
            assistant.get_by_label("知识标题", exact=True).fill("Unsaved title")
            assistant.get_by_role("button", name="关闭关联编辑", exact=True).click()
            expect(assistant).to_have_count(0)
            detail.get_by_role("button", name="编辑", exact=True).click()
            expect(assistant.get_by_label("知识标题", exact=True)).to_have_value("新增收费项说明")
            assistant.get_by_role("button", name="取消", exact=True).click()
            expect(assistant).to_have_count(0)
            detail.get_by_role("button", name="编辑", exact=True).click()
            assistant.get_by_label("知识标题", exact=True).press("Escape")
            expect(assistant).to_have_count(0)
            expect(detail).to_contain_text("必须保留的人工说明")
            detail.get_by_role("button", name="移除关联", exact=True).click()
            expect(row).to_have_count(0)
            page.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^已移除")).click()
            expect(row).to_be_visible()
            detail.get_by_role("button", name="恢复关联", exact=True).click()
            page.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^全部当前")).click()
            expect(row).to_be_visible()
            detail.get_by_role("button", name="打开原型", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.get_by_role("button", name="新增收费项", exact=True)).to_be_visible(timeout=15000)
        finally:
            browser.close()


def test_association_pagination_filters_and_background_refresh_preserve_current_page(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    phase = {"finished": False}
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            items = []
            for index in range(1, 27 if phase["finished"] else 26):
                row = deepcopy(original)
                row.update(id=f"fixture-{index}", title=f"示例关联{index:02}", summary=f"完整说明{index:02}")
                if index % 10 == 0:
                    row["validity_status"] = "stale"
                items.append(row)
            payload["data"] = items
            route.fulfill(response=response, json=payload)

        def batch(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["jobs"] = [
                {
                    "id": "fixture-job",
                    "state": "succeeded" if phase["finished"] else "running",
                    "stage": "ASSOCIATE",
                    "progress": 100 if phase["finished"] else 0,
                    "current_scope": True,
                    "concurrency": 3,
                    "mode": "missing",
                    "result": {
                        "total": 25,
                        "processed": 25 if phase["finished"] else 0,
                        "saved": 1 if phase["finished"] else 0,
                    },
                }
            ]
            route.fulfill(response=response, json=payload)

        page.route("**/interactive/reassociate/targets?*", lambda route: route.fulfill(json={"data": {"items": [], "total": 0, "next_offset": None}}))
        page.route("**/api/v3/projects/*/interactive/bindings?*", bindings)
        page.route("**/associations/batch?*", batch)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            panel = page.get_by_role("region", name="关联管理", exact=True)
            nav = panel.get_by_role("navigation", name="关联列表分页")
            rows = panel.locator(".binding-table tbody tr")
            expect(rows).to_have_count(10)
            expect(nav).to_contain_text("第 1 / 3 页")
            nav.get_by_role("button", name="下一页", exact=True).click()
            expect(panel.get_by_role("article", name="关联详情")).to_contain_text("完整说明11")
            phase["finished"] = True
            expect(panel.locator(".binding-list-caption")).to_contain_text("26 条关联", timeout=15000)
            expect(nav).to_contain_text("第 2 / 3 页")
            expect(rows).to_have_count(10)
            nav.get_by_role("button", name="末页", exact=True).click()
            expect(rows).to_have_count(6)
            expect(nav.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
            nav.get_by_label("跳转关联页", exact=True).fill("2")
            nav.get_by_role("button", name="跳转", exact=True).click()
            expect(nav).to_contain_text("第 2 / 3 页")
            expect(rows).to_have_count(10)
            panel.get_by_label("搜索关联", exact=True).fill("示例关联02")
            expect(rows).to_have_count(1)
            expect(nav).to_contain_text("第 1 / 1 页")
            panel.get_by_label("搜索关联", exact=True).fill("")
            nav.get_by_role("button", name="下一页", exact=True).click()
            panel.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^待处理")).click()
            expect(rows).to_have_count(2)
            expect(nav).to_contain_text("第 1 / 1 页")
            panel.get_by_role("group", name="关联状态筛选").get_by_role("button", name=re.compile("^全部当前")).click()
            panel.get_by_label("每页关联条数", exact=True).select_option("20")
            expect(rows).to_have_count(20)
            expect(nav).to_contain_text("第 1 / 2 页")
            assert panel.locator(".binding-table-wrap").evaluate("el=>el.clientHeight>100&&getComputedStyle(el).overflowY==='auto'")
            page.set_viewport_size({"width": 390, "height": 720})
            assert panel.locator(".binding-table-wrap").evaluate("el=>el.clientHeight>100&&getComputedStyle(el).overflowY==='auto'")
            assert not errors
        finally:
            browser.close()


def test_result_totals_jump_and_global_search_with_status_filter(materials, tmp_path, monkeypatch):
    web, _, _, _, projects = materials
    pid, aid = projects[0]
    outcomes = [
        {
            "id": f"result-{index}",
            "label": f"控件{index:03}",
            "status": "insufficient" if index % 3 == 0 else "linked",
            "message": "文档依据不足" if index % 3 == 0 else "关联已保存",
        }
        for index in range(1, 236)
    ]
    requests = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def batch(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["jobs"] = [
                {
                    "id": "results-fixture",
                    "state": "succeeded",
                    "stage": "ASSOCIATE",
                    "current_scope": True,
                    "result": {
                        "total": 235,
                        "processed": 235,
                        "saved": 157,
                        "details_truncated": True,
                        "completed": {item["id"]: item for item in outcomes[-200:]},
                    },
                }
            ]
            route.fulfill(response=response, json=payload)

        def items(route):
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(route.request.url).query)
            needle = query.get("q", [""])[0]
            status = query.get("status", ["all"])[0]
            offset = int(query["offset"][0])
            selected = [
                item
                for item in outcomes
                if (status == "all" or item["status"] == status) and needle in (item["label"] + item["message"])
            ]
            requests.append(query)
            route.fulfill(
                json={
                    "data": {
                        "items": selected[offset : offset + 10],
                        "total": len(selected),
                        "has_more": offset + 10 < len(selected),
                    }
                }
            )

        page.route("**/associations/batch?*", batch)
        page.route("**/associations/batch/items?*", items)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.locator(".batch-task-details > summary").click()
            page.get_by_text("查看逐项结果", exact=True).click()
            nav = page.get_by_role("navigation", name="逐项结果分页", exact=True)
            rows = page.locator(".batch-result-list li")
            expect(nav).to_contain_text("共 235 条 · 第 1 / 24 页")
            expect(rows).to_have_count(10)
            nav.get_by_label("跳转结果页", exact=True).fill("24")
            nav.get_by_role("button", name="跳转", exact=True).click()
            expect(nav).to_contain_text("第 24 / 24 页")
            expect(rows).to_have_count(5)
            expect(nav.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
            page.get_by_label("搜索逐项结果", exact=True).fill("控件235")
            expect(nav).to_contain_text("共 1 条 · 第 1 / 1 页")
            expect(rows).to_have_count(1)
            expect(rows).to_contain_text("控件235")
            page.get_by_label("搜索逐项结果", exact=True).fill("")
            page.get_by_label("筛选处理结果", exact=True).select_option("insufficient")
            expect(nav).to_contain_text("共 78 条 · 第 1 / 8 页")
            expect(rows).to_have_count(10)
            assert page.locator(".batch-result-viewport").evaluate("el=>el.scrollHeight<=el.clientHeight+1")
            assert any(request.get("offset") == ["230"] for request in requests)
            assert any(request.get("q") == ["控件235"] for request in requests)
        finally:
            browser.close()


def test_module_tree_parent_feature_search_keyboard_and_mobile(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        fetched = []
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")

        def bindings(route):
            response = route.fetch()
            payload = response.json()
            original = payload["data"][0]
            rows = []
            for key, path, label, selector, frames in [
                ("fee", "/actual/物业缴费/items.html", "收费项目管理", "body", ["actualPage"]),
                ("add", "/actual/物业缴费/items.html", "新增", "#add", ["actualPage"]),
                ("embedded", "/actual/物业缴费/items.html", "确定", "#ok", ["actualPage", "dialog"]),
                ("reduce", "/actual/物业缴费/reduce.html", "减免类型", "body", ["actualPage"]),
                ("report", "/actual/物业报表/stat.html", "应收统计", "body", ["actualPage"]),
                ("download", "/actual/物业报表/stat.html", "下载", "#download", ["actualPage"]),
            ]:
                row = deepcopy(original)
                row.update(
                    id=key,
                    title=label,
                    validity_status="outdated" if key == "add" else "valid",
                    scope="module" if selector == "body" else "control",
                )
                row["target"].update(route=path, label=label, selector=selector, frame_path=frames)
                rows.append(row)
            payload["data"] = rows
            route.fulfill(response=response, json=payload)

        def inventory(route):
            fetched.append(route.request.url)
            route.fulfill(
                json={
                    "data": {
                        "packages": [
                            {
                                "package_id": prototype["artifact_id"],
                                "pages": [
                                    {
                                        "route": "/actual/物业缴费/items.html",
                                        "label": "收费项目管理",
                                        "frame_path": ["actualPage"],
                                    },
                                    {
                                        "route": "/actual/隐藏/private.html",
                                        "label": "不可展示的未关联页面",
                                        "frame_path": [],
                                    },
                                ],
                            }
                        ]
                    }
                }
            )

        page.route("**/interactive/reassociate/targets?*", lambda route: route.fulfill(json={"data": {"items": [], "total": 0, "next_offset": None}}))
        page.route("**/interactive/bindings?*", bindings)
        page.route("**/associations/inventory?*", inventory)
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            caption = page.locator(".binding-list-caption")
            expect(caption).to_contain_text("6 条关联")
            trigger = page.get_by_role("button", name="筛选关联模块", exact=True)
            trigger.click()
            tree = page.get_by_role("tree", name="关联模块与功能", exact=True)
            expect(tree.get_by_role("treeitem")).to_have_count(2)
            fees = tree.get_by_role("treeitem", name="物业缴费", exact=True)
            expect(fees).to_have_attribute("aria-expanded", "false")
            tree.get_by_role("button", name="展开物业缴费", exact=True).click()
            expect(fees.get_by_role("treeitem")).to_have_count(2)
            expect(tree.get_by_role("treeitem", name="新增", exact=True)).to_have_count(0)
            expect(tree.get_by_role("treeitem", name="不可展示的未关联页面", exact=True)).to_have_count(0)
            fees.focus()
            fees.press("Enter")
            expect(caption).to_contain_text("4 条关联")
            expect(trigger).to_contain_text("物业缴费")
            trigger.click()
            feature = tree.get_by_role("treeitem", name="收费项目管理", exact=True)
            feature.focus()
            feature.press("Enter")
            expect(caption).to_contain_text("3 条关联")
            expect(trigger).to_contain_text("物业缴费 / 收费项目管理")
            status_filters = page.get_by_role("group", name="关联状态筛选")
            counts = status_filters.locator("button > span")
            expect(counts).to_have_text(["3", "1", "2", "0"])
            status_filters.get_by_role("button", name=re.compile("^待处理")).click()
            expect(caption).to_contain_text("1 条关联")
            expect(counts).to_have_text(["3", "1", "2", "0"])
            status_filters.get_by_role("button", name=re.compile("^全部当前")).click()
            expect(caption).to_contain_text("3 条关联")
            keyword = page.get_by_label("搜索关联", exact=True)
            keyword.fill("确定")
            expect(caption).to_contain_text("1 条关联")
            expect(counts).to_have_text(["1", "0", "1", "0"])
            keyword.fill("")
            trigger.click()
            search = page.get_by_label("搜索模块或功能", exact=True)
            search.fill("统计")
            expect(tree.get_by_role("treeitem", name="物业报表", exact=True)).to_be_visible()
            stats = tree.get_by_role("treeitem", name="应收统计", exact=True)
            expect(stats).to_be_visible()
            stats.focus()
            stats.press("Enter")
            expect(caption).to_contain_text("2 条关联")
            expect(counts).to_have_text(["2", "0", "2", "0"])
            trigger.click()
            page.get_by_role("button", name="全部模块", exact=True).click()
            expect(caption).to_contain_text("6 条关联")
            expect(counts).to_have_text(["6", "1", "5", "0"])
            trigger.press("ArrowDown")
            search.press("ArrowDown")
            fees.focus()
            fees.press("ArrowRight")
            expect(fees).to_have_attribute("aria-expanded", "true")
            fees.press("ArrowDown")
            page.keyboard.press("ArrowLeft")
            expect(fees).to_be_focused()
            page.keyboard.press("Escape")
            expect(tree).to_have_count(0)
            expect(trigger).to_be_focused()
            page.set_viewport_size({"width": 390, "height": 720})
            trigger.click()
            popover = page.locator(".binding-module-popover")
            box = popover.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
            page.get_by_role("heading", name="关联管理", exact=True).click()
            expect(tree).to_have_count(0)
            assert len(fetched) == 1 and not errors
        finally:
            browser.close()


def test_detached_document_draft_can_switch_to_binding_edit_and_return(materials, tmp_path, monkeypatch):
    from vks_api import ai_gateway

    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    monkeypatch.setattr(
        ai_gateway, "request_model", lambda *args: (_ for _ in ()).throw(AssertionError("No inference expected"))
    )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda r: (
                writes.append(r.url)
                if r.method in ("POST", "PUT", "PATCH", "DELETE")
                and (r.url.endswith("/conversation") or "/interactive/bindings" in r.url)
                else None
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            document = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            field = document.locator("#fh-ai-question")
            expect(field).to_be_visible()
            field.fill("保持未发送的文档草稿")
            with page.expect_popup() as opened:
                document.get_by_role("button", name="弹出独立窗口", exact=True).click()
            popup = opened.value
            popup.on("pageerror", lambda error: errors.append(str(error)))
            detached_document = popup.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(detached_document.locator("#fh-ai-question")).to_have_value("保持未发送的文档草稿")
            page.get_by_role("button", name="关联管理", exact=True).click()
            expect(page.get_by_role("button", name="打开知识助手", exact=True)).to_have_count(0)
            detail = page.get_by_role("article", name="关联详情")
            detail.get_by_role("button", name="编辑", exact=True).click()
            editor = popup.get_by_role("complementary", name="知识助手", exact=True)
            expect(editor).to_be_visible()
            expect(editor.locator("..")).to_be_visible()
            expect(editor.get_by_label("知识标题", exact=True)).to_have_value("新增收费项说明")
            editor.get_by_label("知识标题", exact=True).fill("不应保存的编辑草稿")
            with popup.expect_event("close"):
                editor.get_by_role("button", name="取消", exact=True).click()
            assert popup.is_closed()
            expect(detail).to_contain_text("新增收费项说明")
            expect(detail).to_contain_text("必须保留的人工说明")
            page.get_by_role("button", name="文档", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            expect(field).to_have_value("保持未发送的文档草稿")
            assert not writes and not errors
        finally:
            browser.close()
