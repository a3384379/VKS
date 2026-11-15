"""Local and reimported image resolution use the same package boundary."""

import base64
import gzip
import json
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from vks_api.handbook_authoring import legacy_template_html as template_html
from vks_api.template_ingest import read_authoring

from tests.ui.test_frontend_browser import chromium_path


def test_authoring_saved_payload_does_not_resolve_absolute_traversing_or_query_image_aliases(tmp_path):
    markdown = "# Images\n\n![Valid](photo.png)\n\n![Absolute](/photo.png)\n\n![Escape](../photo.png)\n\n![Query](photo.png?x=1)\n\n![Fragment](photo.png#rule)\n"
    image = BytesIO()
    Image.new("RGB", (4, 3), "teal").save(image, "PNG")
    blank = tmp_path / "template.html"
    blank.write_bytes(template_html())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
            page.goto(blank.as_uri())
            page.wait_for_function("!!window.VksHandbookAuthoring")
            page.get_by_label("选择 Markdown 和图片文件", exact=True).set_input_files(
                [
                    {"name": "guide.md", "mimeType": "text/markdown", "buffer": markdown.encode()},
                    {"name": "photo.png", "mimeType": "image/png", "buffer": image.getvalue()},
                ]
            )
            expect(page.locator(".vks-authoring-tools > strong")).to_contain_text("1 篇")
            page.get_by_role("button", name="开始阅读", exact=True).click()
            expect(page.locator('.hb-article img[alt="Valid"]')).to_be_visible()
            expect(page.locator(".hb-article img")).to_have_count(1)
            with page.expect_download() as downloaded:
                page.get_by_role("button", name="保存可编辑 HTML", exact=True).click()
            path = tmp_path / "images.html"
            downloaded.value.save_as(path)
            raw = path.read_bytes()
            source = read_authoring(raw)
            assert source["documents"] == [("guide.md", markdown)]
            assert source["files"] == [("photo.png", image.getvalue())]
            soup = BeautifulSoup(raw, "html.parser")
            payload = json.loads(gzip.decompress(base64.b64decode(soup.select_one("#vks-payload").string)))
            assert set(payload["documents"][0]["images"]) == {"photo.png"}
            assert payload["documents"][0]["markdown"] == markdown
        finally:
            browser.close()


def test_corrupt_raster_is_rejected_before_it_becomes_an_unreimportable_saved_book(tmp_path):
    blank = tmp_path / "template.html"
    blank.write_bytes(template_html())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(blank.as_uri())
            page.wait_for_function("!!window.VksHandbookAuthoring")
            page.get_by_label("选择 Markdown 和图片文件", exact=True).set_input_files(
                [
                    {"name": "guide.md", "mimeType": "text/markdown", "buffer": b"# Guide\n\n![broken](photo.png)"},
                    {
                        "name": "photo.png",
                        "mimeType": "image/png",
                        "buffer": bytes([137, 80, 78, 71, 13, 10, 26, 10, 0]),
                    },
                ]
            )
            expect(page.locator(".vks-authoring-tools [role='alert']")).to_contain_text("图片")
            envelope = json.loads(page.locator("#vks-template-data").text_content())
            assert envelope["documents"] == [] and envelope["images"] == []
        finally:
            browser.close()
