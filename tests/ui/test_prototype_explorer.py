from io import BytesIO
from urllib.parse import parse_qs

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright
from test_frontend_browser import chromium_path
from test_frontend_browser import site as site  # noqa: PLC0414 - pytest fixture
from vks_api import identity

from scripts.build_prototype_demo import HEIGHT, PAGE_KEY, WIDTH, build_bundle
from tests.prototype.test_raster_prototype import import_project, original


def test_raster_canvas_keeps_pixels_actions_and_exact_source_return(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-demo", "name": "Prototype"}, "prototype-browser")["id"]
    raw = build_bundle()
    aid = import_project(service, owner, pid, raw, "prototype-browser-import")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 2048, "height": 1440}, device_scale_factor=1)
        try:
            page.goto(f"{site[0]}/#view=workbench&project={pid}")
            page.get_by_label("登录名", exact=True).fill("owner")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开工作空间").click()
            canvas = page.get_by_role("region", name="原型画布")
            image = canvas.locator("img")
            page.get_by_role("button", name="原尺寸", exact=True).click()
            page.get_by_role("button", name="显示热区", exact=True).click()
            expect(image).to_be_visible()
            assert image.evaluate("(img) => img.decode().then(() => img.naturalWidth)") == WIDTH
            assert image.bounding_box()["width"] == WIDTH and image.bounding_box()["height"] == HEIGHT
            with Image.open(BytesIO(image.screenshot())) as painted, Image.open(BytesIO(original(raw))) as source:
                assert ImageChops.difference(painted.convert("RGB"), source.convert("RGB")).getbbox() is None

            page.get_by_role("button", name="显示热区", exact=True).click()
            canvas.get_by_role("button", name="新增收费项").click()
            expect(page.get_by_role("heading", name="收费项管理（合成） · 新增抽屉")).to_be_focused()
            expect(page.get_by_text("合成规则：新增项需要填写唯一编码。", exact=False)).to_be_visible()
            exact_route = parse_qs(page.url.split("#")[1])
            assert exact_route["baseline"] == [f"draft:{aid}"]
            assert exact_route["trigger"] == ["list"]
            assert exact_route["state"] == ["create-drawer"]
            page.get_by_role("button", name="查看原文 · 新增校验").click()
            expect(page.get_by_role("heading", name="新增校验", exact=True)).to_be_visible()
            page.get_by_role("button", name="返回业务动作 · 新增收费项").click()
            expect(page.get_by_role("heading", name="收费项管理（合成） · 新增抽屉")).to_be_visible()
            assert parse_qs(page.url.split("#")[1]) == exact_route
            page.reload()
            expect(page.get_by_role("button", name="查看原文 · 新增校验")).to_be_visible()
            assert parse_qs(page.url.split("#")[1]) == exact_route

            page.set_viewport_size({"width": 1250, "height": 1050})
            page.get_by_label("原型状态").select_option("list")
            page.get_by_role("button", name="适应宽度", exact=True).click()
            sync = canvas.get_by_role("button", name="同步月卡套餐")
            expect(sync).to_be_visible()
            bounds = image.bounding_box()
            hot = sync.bounding_box()
            assert bounds["width"] < WIDTH
            assert abs(hot["x"] - bounds["x"] - bounds["width"] * 0.48) < 1
            sync.click()
            expect(page.get_by_text("该动作的目标状态画面待补充，继续显示当前状态。")).to_be_visible()
            expect(page.get_by_role("button", name="查看原文 · 停车月卡")).to_be_visible()
            canvas.get_by_role("button", name="导出", exact=True).click()
            expect(page.get_by_text("当前可见范围内暂无关联说明。")).to_be_visible()
            expect(page.get_by_role("button", name="查看原文 · 停车月卡")).to_have_count(0)
            canvas.get_by_role("button", name="编辑收费项").click()
            canvas.get_by_role("button", name="违约金设置").focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("button", name="查看原文 · 违约金口径")).to_be_visible()
            page.go_back()
            expect(page.get_by_role("heading", name="编辑收费项", exact=True)).to_be_visible()
            page.go_forward()
            expect(page.get_by_role("button", name="查看原文 · 违约金口径")).to_be_visible()
        finally:
            browser.close()


def _open_project(page, site, pid, view=None, *, username="owner", password="owner"):
    route = f"project={pid}" + (f"&view={view}" if view else "")
    page.goto(f"{site[0]}/#{route}")
    page.get_by_label("登录名", exact=True).fill(username)
    page.get_by_label("密码", exact=True).fill(password)
    page.get_by_role("button", name="打开工作空间").click()


def test_project_open_and_search_restore_exact_raster_context(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-route", "name": "Route prototype"}, "route")["id"]
    aid = import_project(service, owner, pid, build_bundle(project="prototype-route"), "route-import")
    other = site[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        try:
            _open_project(page, site, pid)
            expect(page.get_by_role("heading", name="收费项管理（合成） · 列表")).to_be_visible()
            canvas = page.get_by_role("region", name="原型画布")
            canvas.get_by_role("button", name="新增收费项").click()
            expect(page.get_by_role("button", name="查看原文 · 新增校验")).to_be_visible()
            exact = parse_qs(page.url.split("#")[1])
            assert exact["baseline"] == [f"draft:{aid}"]
            page.get_by_label("当前项目").select_option(other)
            expect(canvas).to_have_count(0)
            page.get_by_label("当前项目").select_option(pid)
            expect(page.get_by_role("button", name="查看原文 · 新增校验")).to_be_visible()
            assert parse_qs(page.url.split("#")[1]) == exact

            page.locator(".global-search").click()
            search_url = page.url
            assert parse_qs(search_url.split("#")[1])["search"] == ["1"]
            search = page.get_by_role("dialog", name="搜索项目知识")
            keywords = search.get_by_placeholder("输入业务、接口、路径或文档关键词…")
            keywords.fill("新增校验")
            search.get_by_label("知识类型").select_option("section")
            result = search.get_by_role("button", name="新增校验", exact=False)
            result.click()
            expect(search).to_have_count(0)
            expect(page.get_by_role("heading", name="新增校验", exact=True)).to_be_visible()
            assert parse_qs(page.url.split("#")[1])["view"] == ["documents"]
            page.go_back()
            expect(page).to_have_url(search_url)
            expect(keywords).to_have_value("新增校验")
            expect(search.get_by_label("知识类型")).to_have_value("section")
            expect(result).to_be_visible()
        finally:
            browser.close()


def test_editor_preserves_multi_state_scope_and_local_input_on_conflict(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-edit", "name": "Edit prototype"}, "editor")["id"]

    def multi_state(manifest):
        manifest["states"][2]["anchors"].append(
            {"anchorKey": "extra.add", "actionKey": "charge.add", "rect": [0.15, 0.15, 0.1, 0.1]}
        )

    raw = build_bundle(project="prototype-edit", edit=multi_state)
    aid = import_project(service, owner, pid, raw, "editor-import")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            _open_project(page, site, pid, "workbench")
            page.get_by_role("button", name="维护页面与动作").click()
            canvas = page.get_by_role("region", name="原型画布")
            canvas.get_by_role("button", name="新增收费项").click()
            point = page.locator("form.prototype-v3-editor").filter(has_text="维护业务点")
            expect(point).to_be_visible()
            point.get_by_role("button", name="开始新建").click()
            point.get_by_label("业务点标题").fill("多状态合成规则")
            point.get_by_label("业务说明", exact=True).fill("同一操作的说明覆盖两个状态。")
            point.get_by_label("编辑抽屉", exact=True).check()
            point.get_by_label("显示顺序").fill("7")
            point.get_by_label("搜索原文章节").fill("新增校验")
            point.get_by_role("checkbox", name="新增校验", exact=False).check()
            point.get_by_role("button", name="保存业务点").click()
            expect(page.get_by_text("已保存业务点关联", exact=True)).to_be_visible()
            expect(point.get_by_label("复用当前说明和出处")).to_be_checked()
            expect(point.get_by_label("列表", exact=True)).to_be_checked()
            expect(point.get_by_label("编辑抽屉", exact=True)).to_be_checked()
            expect(point.get_by_label("显示顺序")).to_have_value("7")

            current = service.workspace(owner, pid, aid)
            added = next(row for row in current["entities"] if row["name"] == "多状态合成规则")
            binding = next(
                row
                for row in current["relations"]
                if row["relation_type"] == "has_business_point" and row["to_revision_id"] == added["entity_revision_id"]
            )
            assert binding["prototype_scope"] == {"states": ["edit-drawer", "list"], "priority": 7}
            action = next(row for row in current["actions"] if row["stable_key"] == "charge.add")
            endpoint = f"{site[0]}/api/v3/projects/{pid}/prototype/actions/charge.add/business-points"
            csrf = page.request.get(f"{site[0]}/api/v1/auth/session").json()["data"]["csrf_token"]

            point.get_by_label("复用当前说明和出处").uncheck()
            summary = point.get_by_label("业务说明", exact=True)
            summary.fill("浏览器尚未保存的说明")
            response = page.request.post(
                endpoint,
                headers={
                    "X-CSRF-Token": csrf,
                    "If-Match": str(current["selected_analysis_set"]["selection_version"]),
                    "Idempotency-Key": "external-binding-edit",
                },
                data={
                    "analysis_set_id": aid,
                    "action_revision_id": action["entity_revision_id"],
                    "page_revision_id": current["pages"][0]["entity_revision_id"],
                    "point_key": added["stable_key"],
                    "point_revision_id": added["entity_revision_id"],
                    "binding_version": binding["row_version"],
                    "state_keys": ["edit-drawer", "list"],
                    "priority": 12,
                },
            )
            assert response.ok, response.text()
            point.get_by_role("button", name="保存业务点").click()
            expect(point.get_by_text("保存冲突", exact=False)).to_be_visible()
            expect(summary).to_have_value("浏览器尚未保存的说明")
            page.get_by_label("原型状态").select_option("edit-drawer")
            expect(page.get_by_label("原型状态")).to_have_value("list")
            point.get_by_role("button", name="重新载入业务点").click()
            expect(point.get_by_label("显示顺序")).to_have_value("12")
            point.get_by_label("复用当前说明和出处").uncheck()
            expect(
                point.get_by_role("group", name="已选原文章节").get_by_role("checkbox", name="新增校验", exact=False)
            ).to_be_checked()
            summary.fill("从新版本合并并保存")
            point.get_by_role("button", name="保存业务点").click()
            expect(page.get_by_role("heading", name="多状态合成规则")).to_be_visible()
            expect(
                page.locator(".prototype-v3-context > section").get_by_text("从新版本合并并保存", exact=True)
            ).to_be_visible()
            current = service.workspace(owner, pid, aid)
            saved = next(row for row in current["entities"] if row["name"] == "多状态合成规则")
            assert any(
                row["relation_type"] == "supported_by_section"
                and not row["excluded"]
                and row["from_revision_id"] == saved["entity_revision_id"]
                for row in current["relations"]
            )
        finally:
            browser.close()


def test_new_state_previews_uploaded_image_and_drawn_hotspot_before_save(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-draw", "name": "Draw prototype"}, "draw")["id"]
    raw = build_bundle(project="prototype-draw")
    aid = import_project(service, owner, pid, raw, "draw-import")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1250})
        try:
            _open_project(page, site, pid, "workbench")
            page.get_by_role("button", name="维护页面与动作").click()
            page.get_by_role("button", name="新建状态").click()
            page.get_by_label("状态标识").fill("new-panel")
            page.get_by_label("状态名称").fill("补录面板")
            page.get_by_label("补录原始 PNG").set_input_files(
                {"name": "synthetic.png", "mimeType": "image/png", "buffer": original(raw)}
            )
            page.get_by_role("button", name="在画布绘制").click()
            overlay = page.locator(".prototype-v3-draw")
            overlay.scroll_into_view_if_needed()
            bounds = overlay.bounding_box()
            assert bounds
            page.mouse.move(bounds["x"] + bounds["width"] * 0.2, bounds["y"] + bounds["height"] * 0.2)
            page.mouse.down()
            page.mouse.move(bounds["x"] + bounds["width"] * 0.3, bounds["y"] + bounds["height"] * 0.3, steps=5)
            expect(overlay.locator(".prototype-v3-draw-preview")).to_be_visible()
            page.mouse.up()
            page.get_by_label("动作名称").fill("合成新热区")
            left = page.get_by_label("左", exact=True)
            before = float(left.input_value())
            left.press("ArrowUp")
            assert 0 < float(left.input_value()) - before < 0.002
            page.get_by_role("button", name="保存状态").click()
            expect(page.get_by_role("heading", name="收费项管理（合成） · 补录面板")).to_be_visible()
            workspace = service.workspace(owner, pid, aid)
            prototype = workspace["pages"][0]
            new_state = next(
                row for row in prototype["metadata"]["prototype_manifest"]["states"] if row["stateKey"] == "new-panel"
            )
            assert len(new_state["anchors"]) == 1
            assert prototype["stable_key"] == PAGE_KEY
        finally:
            browser.close()


def test_medium_drawer_preserves_canvas_focus_and_retries_context(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-drawer", "name": "Drawer prototype"}, "drawer")["id"]
    import_project(service, owner, pid, build_bundle(project="prototype-drawer"), "drawer-import")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 960, "height": 1080})
        try:
            _open_project(page, site, pid)
            canvas = page.get_by_role("region", name="原型画布")
            toggle = page.get_by_role("button", name="展开业务说明")
            expect(toggle).to_be_visible()
            bounds = canvas.bounding_box()
            dialog = page.get_by_role("dialog", name="业务说明")
            toggle.click()
            close = dialog.get_by_role("button", name="关闭业务说明")
            expect(close).to_be_focused()
            assert canvas.bounding_box() == bounds
            page.keyboard.press("Escape")
            expect(toggle).to_be_focused()
            expect(dialog).to_have_count(0)

            page.route("**/prototype/actions/charge.add/context?*", lambda route: route.abort(), times=1)
            trigger = canvas.get_by_role("button", name="新增收费项")
            trigger.focus()
            page.keyboard.press("Enter")
            expect(close).to_be_focused()
            dialog.get_by_role("button", name="重试业务说明").click()
            source = dialog.get_by_role("button", name="查看原文 · 新增校验")
            expect(source).to_be_visible()
            close.focus()
            page.keyboard.press("Shift+Tab")
            expect(source).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.get_by_role("heading", name="收费项管理（合成） · 新增抽屉")).to_be_focused()
            toggle.click()
            expect(source).to_be_visible()
            page.set_viewport_size({"width": 640, "height": 1080})
            expect(dialog).to_have_count(0)
            expect(page.get_by_role("button", name="查看原文 · 新增校验")).to_be_visible()
            assert page.locator(".prototype-v3-context").bounding_box()["y"] >= (
                canvas.bounding_box()["y"] + canvas.bounding_box()["height"]
            )
        finally:
            browser.close()


def test_revoked_draft_is_hidden_while_new_workspace_authorizes(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-revoked", "name": "Revocation prototype"}, "revoked")["id"]
    aid = import_project(service, owner, pid, build_bundle(project="prototype-revoked"), "revoked-import")
    username, password = "prototype-reader", "synthetic-prototype-reader-pass"
    user = identity.create_user(
        service,
        owner,
        {
            "username": username,
            "display_name": "Prototype reader",
            "password": password,
        },
    )
    service.set_member(
        owner,
        pid,
        {"user_id": user["id"], "role": "editor", "status": "active"},
        service.get_project(owner, pid)["row_version"],
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        try:
            _open_project(page, site, pid, "workbench", username=username, password=password)
            canvas = page.get_by_role("region", name="原型画布")
            expect(canvas.get_by_role("button", name="新增收费项")).to_be_visible()
            service.set_member(
                owner,
                pid,
                {"user_id": user["id"], "role": "viewer", "status": "active"},
                service.get_project(owner, pid)["row_version"],
            )
            held = []
            endpoint = f"/api/v1/projects/{pid}/workspace?analysis_set_id={aid}"
            page.route(f"**{endpoint}", lambda route: held.append(route), times=1)
            with page.expect_request(lambda request: request.url.endswith(endpoint)):
                page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(canvas).to_have_count(0)
            assert len(held) == 1
            held[0].continue_()
            expect(page.get_by_text("请求的知识版本已无法读取", exact=False)).to_be_visible()
            expect(page.get_by_role("button", name="维护页面与动作")).to_have_count(0)
            assert "baseline" not in parse_qs(page.url.split("#")[1])
        finally:
            browser.close()


def test_prototype_menu_groups_persist_and_keep_exact_page_under_filter(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-groups", "name": "Grouped prototype"}, "groups")["id"]
    aid = import_project(service, owner, pid, build_bundle(project="prototype-groups"), "groups-import")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        try:
            _open_project(page, site, pid, "settings")
            page.get_by_text("编辑快捷菜单分组", exact=True).click()
            group = page.get_by_role("group", name="快捷菜单分组")
            label = group.get_by_role("textbox", name="收费项管理（合成）", exact=False)
            label.fill("  收费设置  ")
            page.get_by_role("button", name="保存导览设置").click()
            expect(page.get_by_text("已保存原型导览设置", exact=True)).to_be_visible()

            page.reload()
            page.get_by_text("编辑快捷菜单分组", exact=True).click()
            expect(label).to_have_value("收费设置")
            nav = page.get_by_role("navigation", name="主导航")
            nav.get_by_role("button", name="原型导览", exact=True).click()
            pages = page.get_by_label("原型页面", exact=True)
            expect(pages).to_be_visible()
            option = pages.locator("optgroup[label='收费设置'] option")
            expect(option).to_have_text("收费项管理（合成）")
            selected = pages.input_value()
            exact = parse_qs(page.url.split("#")[1])
            assert exact["baseline"] == [f"draft:{aid}"]
            assert exact["ui_page"] == [selected]
            page.get_by_label("筛选页面").fill("无匹配项")
            expect(pages).to_have_value(selected)
            expect(option).to_have_count(1)
            assert parse_qs(page.url.split("#")[1]) == exact
        finally:
            browser.close()
