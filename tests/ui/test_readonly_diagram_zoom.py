"""Offline Mermaid zoom keeps one clean SVG at every scale and after saving."""

from base64 import b64decode
from math import log
from xml.etree import ElementTree

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.ui.test_frontend_browser import chromium_path
from tests.ui.test_handbook_authoring_reader import payload


def test_offline_classic_diagram_zoom_and_saved_svg_reopen(tmp_path):
    source = "```mermaid\nflowchart LR\n A[导入文档] --> B{校验资料}\n B --> C[保存知识]\n```\n"
    path = tmp_path / "reader.html"
    path.write_bytes(html_library(payload("图表放大", source)))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(path.as_uri())
            page.get_by_role("button", name="开始阅读", exact=True).click()
            preview = page.locator(".document img[alt='Mermaid 图表']")
            expect(preview).to_be_visible(timeout=20000)
            uri = preview.get_attribute("src")
            svg = b64decode(uri.split(",", 1)[1]).decode()
            looks = {node.get("data-look") for node in ElementTree.fromstring(svg).iter() if node.get("data-look")}
            assert looks == {"classic"}
            assert "导入文档" in svg and "校验资料" in svg and "保存知识" in svg
            preview.click()
            dialog = page.get_by_role("dialog", name="Mermaid 图表", exact=True)
            image = dialog.locator("img")
            canvas = dialog.get_by_role("region", name="图片平移缩放画布")
            expect(dialog).to_be_visible()
            expect(image).to_be_visible()
            expect(dialog.get_by_label("图片缩放比例")).to_have_text("100%")
            width = image.bounding_box()["width"]
            for scale in (2, 3):
                dialog.get_by_role("button", name="图片缩放比例", exact=True).click()
                rect = canvas.bounding_box()
                page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                delta = log(scale) / 0.0025
                while delta > 0:
                    step = min(150, delta)
                    page.mouse.wheel(0, -step)
                    delta -= step
                expect(dialog.get_by_label("图片缩放比例")).to_have_text(f"{scale * 100}%")
                assert image.bounding_box()["width"] == pytest.approx(width * scale, abs=1)
                expect(dialog.locator("img")).to_have_count(1)
                assert image.get_attribute("src") == uri
                expect(page.locator(".document img[alt='Mermaid 图表']")).to_have_count(1)
            with page.expect_download() as downloaded:
                # Save the exact displayed SVG, as a browser's Save image action
                # does; this reader uses an image, not a separate SVG toolbar.
                page.evaluate("""uri => {
                    const link=document.createElement('a');link.href=uri;link.download='diagram.svg';
                    document.body.append(link);link.click();link.remove();
                }""", uri)
            saved = tmp_path / "saved.svg"
            downloaded.value.save_as(saved)
            assert saved.read_text() == svg
            reopened = browser.new_page()
            reopened.goto(saved.as_uri())
            expect(reopened.locator("svg")).to_have_count(1)
            assert "导入文档" in reopened.locator("svg").text_content()
            assert reopened.locator("svg [data-look='neo']").count() == 0
            assert reopened.locator("svg [data-look='classic']").count() > 0
            assert reopened.locator("svg .node, svg .node > *, svg .cluster rect").evaluate_all(
                "nodes=>nodes.every(node=>getComputedStyle(node).filter==='none')"
            )
            assert not [url for url in requests if url.startswith(("http://", "https://"))]
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("dimensions", [(200, 2400), (2400, 200), (40, 24)])
def test_offline_complete_thumbnail_fit_width_and_source_lifecycle(tmp_path, dimensions):
    from base64 import b64encode
    from io import BytesIO
    from PIL import Image

    image_bytes = BytesIO()
    Image.new("RGB", dimensions, "#168568").save(image_bytes, "PNG")
    uri = "data:image/png;base64," + b64encode(image_bytes.getvalue()).decode()
    source = "![完整图片](" + uri + ")"
    data = payload("完整缩略图", source)
    path = tmp_path / "reader.html"
    path.write_bytes(html_library(data))
    original_bytes = path.read_bytes()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(path.as_uri())
            page.get_by_role("button", name="开始阅读", exact=True).click()
            preview = page.locator(".document img[alt='完整图片']")
            expect(preview).to_be_visible()
            box = preview.bounding_box()
            assert box["height"] <= min(dimensions[1], 480) + 1
            assert box["width"] <= min(dimensions[0], preview.evaluate("n=>n.parentElement.clientWidth")) + 1
            assert box["width"] / box["height"] == pytest.approx(dimensions[0]/dimensions[1], rel=0.01)
            preview.focus()
            preview.press("Enter")
            viewer = page.get_by_role("dialog", name="完整图片", exact=True)
            expect(viewer).to_be_visible()
            large = viewer.locator("img")
            canvas = viewer.get_by_role("region", name="图片平移缩放画布")
            expect(large).to_be_visible()
            fit = large.bounding_box()
            bounds = canvas.bounding_box()
            assert fit["width"] <= bounds["width"] - 30
            assert fit["height"] <= bounds["height"] - 30
            viewer.get_by_role("button", name="按宽度", exact=True).click()
            wide = large.bounding_box()
            assert wide["width"] == pytest.approx(min(bounds["width"]-32, dimensions[0]*8), abs=2)
            if dimensions[1] > dimensions[0]:
                assert wide["y"] == pytest.approx(bounds["y"]+16, abs=2)
            viewer.get_by_role("button", name="图片缩放比例", exact=True).click()
            expect(viewer.get_by_role("button", name="图片缩放比例", exact=True)).to_have_text("100%")
            natural = large.bounding_box()
            assert natural["width"] == pytest.approx(dimensions[0], abs=1)
            assert natural["height"] == pytest.approx(dimensions[1], abs=1)
            assert large.get_attribute("referrerpolicy") == "no-referrer"
            # A new reading context removes/replaces its old source node.
            preview.evaluate("node=>node.remove()")
            expect(viewer).to_have_count(0)
            assert path.read_bytes() == original_bytes
        finally:
            browser.close()
