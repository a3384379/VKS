from io import BytesIO
from urllib.parse import parse_qs

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright
from test_frontend_browser import chromium_path
from test_frontend_browser import site as site  # noqa: PLC0414 - pytest fixture

from scripts.build_prototype_demo import HEIGHT, WIDTH, build_bundle
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
