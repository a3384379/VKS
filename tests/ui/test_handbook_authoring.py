"""Downloadable local authoring saves editable HTML and reimports ordinary MD."""

import base64
import gzip
import json
from io import BytesIO

from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from vks_api.handbook_authoring import legacy_template_html as template_html
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.readonly_export import html_library
from vks_api.template_ingest import read_authoring

from tests.interactive import test_materials
from tests.ui.test_frontend_browser import chromium_path
from tests.ui.test_readonly_reader import open_connection_settings

materials = test_materials.materials


def embedded_payload(raw):
    soup = BeautifulSoup(raw, "html.parser")
    return json.loads(gzip.decompress(base64.b64decode(soup.select_one("#vks-payload").string)))


def test_local_directory_edit_personal_key_safe_save_reopen_and_platform_reimport(materials, tmp_path):
    original = "# Overview\r\n\r\n**Original strong text.**\r\n\r\n![Local picture](../images/photo.png)\r\n\r\n```mermaid\r\nflowchart LR\r\n A[Input] --> B[Output]\r\n```\r\n"
    source = tmp_path / "source"
    (source / "guide").mkdir(parents=True)
    (source / "images").mkdir()
    (source / "guide/overview.md").write_bytes(original.encode())
    (source / "guide/notes.txt").write_text("# Notes\n\nOriginal notes.")
    Image.new("RGB", (90, 40), "#148478").save(source / "images/photo.png")
    blank = tmp_path / "blank.html"
    blank.write_bytes(template_html())
    saved = tmp_path / "populated.html"
    key = "PERSONAL_KEY_MUST_NEVER_ENTER_SAVED_HTML"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(blank.as_uri())
            expect(page.get_by_label("本地手册编辑工具", exact=True)).to_be_visible()
            page.wait_for_function("!!window.VksHandbookAuthoring")
            page.get_by_label("选择文档目录", exact=True).set_input_files(source)
            expect(
                page.get_by_text("已在本页更新 2 篇文档与 1 张图片。请保存 HTML，原文件不会被自动覆盖。", exact=True)
            ).to_be_visible()
            book = page.get_by_role("region", name="知识手册阅读模板")
            book.get_by_role("button", name="开始阅读", exact=True).click()
            book.locator(".hb-toc").get_by_role("button", name="Overview", exact=True).click()
            expect(book.locator(".hb-article h1")).to_have_text("Overview")
            expect(book.get_by_role("img", name="Local picture", exact=True)).to_be_visible()
            expect(book.get_by_role("img", name="Mermaid 图表", exact=True)).to_be_visible()
            book.locator(".hb-toc").get_by_role("button", name="Notes", exact=True).click()
            page.get_by_role("button", name="整理文档", exact=True).click()
            editor = page.get_by_role("dialog", name="整理本地手册文档")
            expect(editor.get_by_label("文档相对路径", exact=True)).to_have_value("guide/notes.txt")
            editor.get_by_label("文档显示名称", exact=True).fill("Notes edited")
            editor.get_by_label("Markdown 原文", exact=True).fill("# Notes\n\n**Edited locally.**")
            editor.get_by_role("button", name="应用修改", exact=True).click()
            expect(book.locator(".hb-article h1")).to_have_text("Notes edited")
            open_connection_settings(page)
            page.get_by_label("API Key", exact=True).fill(key)
            with page.expect_download() as download:
                page.get_by_role("button", name="保存可编辑 HTML", exact=True).click()
            download.value.save_as(saved)
            raw = saved.read_bytes()
            assert key.encode() not in raw
            envelope = read_authoring(raw)
            assert dict(envelope["documents"])["guide/overview.md"] == original
            assert dict(envelope["documents"])["guide/notes.txt"] == "# Notes\n\n**Edited locally.**"
            assert dict(envelope["files"])["images/photo.png"] == (source / "images/photo.png").read_bytes()
            payload = embedded_payload(raw)
            assert payload["ai"] == {"base_url": "https://api.openai.com/v1", "protocol": "responses", "model": ""}
            soup = BeautifulSoup(raw, "html.parser")
            skeleton = gzip.decompress(base64.b64decode(soup.select_one("#vks-authoring-source").string))
            assert key.encode() not in skeleton and b"Original strong text" not in skeleton
            page.goto(saved.as_uri())
            expect(page.get_by_label("本地手册编辑工具", exact=True)).to_be_visible()
            page.wait_for_function("!!window.VksHandbookAuthoring")
            book.get_by_role("button", name="开始阅读", exact=True).click()
            book.locator(".hb-toc").get_by_role("button", name="Overview", exact=True).click()
            expect(book.get_by_role("img", name="Local picture", exact=True)).to_be_visible()
            open_connection_settings(page)
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
            readonly = tmp_path / "readonly.html"
            payload.pop("authoring")
            readonly.write_bytes(html_library(payload))
            page.goto(readonly.as_uri())
            expect(page.get_by_role("region", name="知识手册阅读模板")).to_be_visible()
            expect(page.get_by_label("本地手册编辑工具", exact=True)).to_have_count(0)
            assert page.evaluate("typeof window.VksHandbookAuthoring") == "undefined"
            assert not errors, errors
        finally:
            browser.close()
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    imported = test_materials.stage(
        service, owner, manager, pid, aid, "documents", saved.read_bytes(), filename="local-handbook.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [imported])
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    assert {doc["path"] for doc in catalog["documents"]} == {"guide/overview.md", "guide/notes.txt"}
    assert catalog["presentation"]["mode"] == "handbook"


def test_local_replace_utf8_validation_and_delete_remain_authoring_only(tmp_path):
    blank = tmp_path / "blank.html"
    blank.write_bytes(template_html())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(blank.as_uri())
            page.wait_for_function("!!window.VksHandbookAuthoring")
            picker = page.get_by_label("选择 Markdown 和图片文件", exact=True)
            picker.set_input_files([{"name": "A.md", "mimeType": "text/markdown", "buffer": b"# First\n\nOriginal."}])
            expect(page.locator(".vks-authoring-tools > strong")).to_contain_text("1 篇")
            picker.set_input_files(
                [{"name": "a.md", "mimeType": "text/markdown", "buffer": b"# Replacement\n\nUpdated."}]
            )
            expect(page.locator(".vks-authoring-tools > strong")).to_contain_text("1 篇")
            picker.set_input_files([{"name": "invalid.md", "mimeType": "text/markdown", "buffer": b"\xff\xfe"}])
            expect(page.locator(".vks-authoring-tools [role='alert']")).to_contain_text("不是有效 UTF-8")
            assert json.loads(page.locator("#vks-template-data").text_content())["documents"][0]["path"] == "a.md"
            page.get_by_role("button", name="整理文档", exact=True).click()
            editor = page.get_by_role("dialog", name="整理本地手册文档")
            page.once("dialog", lambda dialog: dialog.accept())
            editor.get_by_role("button", name="删除文档", exact=True).click()
            expect(page.locator(".vks-authoring-tools > strong")).to_contain_text("0 篇")
            editor.get_by_role("button", name="关闭", exact=True).click()
            expect(page.get_by_text("这个手册尚未包含文档。", exact=True)).to_be_visible()
        finally:
            browser.close()


def test_invalid_and_oversized_raster_images_are_rejected_before_they_can_be_saved(tmp_path):
    blank = tmp_path / "blank.html"
    blank.write_bytes(template_html())
    large = BytesIO()
    Image.new("RGB", (4097, 4097), "white").save(large, format="PNG")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(blank.as_uri())
            page.wait_for_function("!!window.VksHandbookAuthoring")
            picker = page.get_by_label("选择 Markdown 和图片文件", exact=True)
            picker.set_input_files([{"name": "broken.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\nX"}])
            expect(page.locator(".vks-authoring-tools [role='alert']")).to_contain_text("图片无法解码")
            assert json.loads(page.locator("#vks-template-data").text_content())["images"] == []
            picker.set_input_files([{"name": "large.png", "mimeType": "image/png", "buffer": large.getvalue()}])
            expect(page.locator(".vks-authoring-tools [role='alert']")).to_contain_text("1677 万像素")
            assert json.loads(page.locator("#vks-template-data").text_content())["images"] == []
        finally:
            browser.close()
