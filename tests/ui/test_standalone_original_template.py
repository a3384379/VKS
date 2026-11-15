"""The downloadable template is the independent source handbook, without Studio UI."""

import base64
import gzip
import json
from hashlib import sha256

import pytest
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from vks_api.handbook_authoring import template_html
from vks_api.service import DomainError
from vks_api.template_ingest import read_authoring

from tests.interactive import test_materials
from tests.interactive.test_native_export import project
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def unpack(raw):
    outer = BeautifulSoup(raw, "html.parser")
    meta = json.loads(outer.find(id="fh-pack-meta").string)
    decoded = gzip.decompress(base64.b64decode(outer.find(id="fh-pack-data").string))
    assert sha256(decoded).hexdigest() == meta["sha256"] and len(decoded) == meta["bytes"]
    return decoded


def test_new_download_is_empty_original_framework_and_rejects_damaged_compression():
    raw = template_html()
    source = unpack(raw)
    book = BeautifulSoup(source, "html.parser")
    assert not book.select("article.document,iframe,#appbar,#vks-payload,#vks-authoring-runtime")
    for id in ("search-data", "source-data", "fh-book-pages", "fh-ai-corpus"):
        assert not json.loads(book.find(id=id).string)
    index = json.loads(book.find(id="fh-ai-index").string)
    assert index["count"] == index["completed"] == 0 and index["base64"] == ""
    assert book.find(id="fh-ai-runtime") and book.find(id="handbook-template-integration")
    assert read_authoring(raw)["template"]["template_version"] == "2.0.0"
    with pytest.raises(DomainError, match="清单"):
        read_authoring(raw.replace(sha256(source).hexdigest().encode(), b"0" * 64, 1))


def test_original_template_directory_update_safe_export_reopen_and_native_reimport(materials, tmp_path):
    original = "# Overview\r\n\r\n**Exact original Markdown.**\r\n\r\n![Local picture](../images/photo.png)\r\n\r\n```mermaid\r\nflowchart LR\r\n A[Import] --> B[Read]\r\n```\r\n"
    directory = tmp_path / "knowledge"
    (directory / "guide").mkdir(parents=True)
    (directory / "images").mkdir()
    (directory / "guide/overview.md").write_bytes(original.encode())
    (directory / "guide/notes.md").write_text(
        "# Notes\n\nA second independent document.\n\n<script>window.MD_EXECUTED=true</script>"
    )
    Image.new("RGB", (60, 30), "teal").save(directory / "images/photo.png")
    blank = tmp_path / "blank.html"
    blank.write_bytes(template_html())
    saved = tmp_path / "saved.html"
    marker = "PERSONAL_KEY_MUST_NOT_ENTER_TEMPLATE_EXPORT"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        errors = []
        network = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: network.append(r.url) if r.url.startswith(("https://", "http://")) else None)
        page.add_init_script(
            "localStorage.setItem('cloud-charge-ai-remembered-key-v1',"
            "JSON.stringify({version:1,key:'OLD_HANDBOOK_PERSONAL_KEY'}))"
        )
        try:
            page.goto(blank.as_uri())
            expect(page.locator("#book-start h1")).to_contain_text("从你的文档开始")
            expect(page.get_by_role("button", name="文档更新", exact=True)).to_be_visible()
            expect(page.get_by_text("交互原型", exact=True)).to_have_count(0)
            page.locator("#fh-ai-launch").click()
            page.locator('[data-ai-tab="settings"]').click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
            page.get_by_label("API Key", exact=True).fill(marker)
            page.locator('[data-ai-tab="knowledge"]').click()
            expect(page.locator("#fh-update-build-index")).not_to_be_checked()
            page.get_by_label("选择手册文档目录", exact=True).set_input_files(directory)
            expect(page.locator("#fh-update-download")).to_be_enabled()
            with page.expect_download(timeout=30000) as downloaded:
                page.locator("#fh-update-download").click()
            downloaded.value.save_as(saved)
            raw = saved.read_bytes()
            inner = unpack(raw)
            assert marker.encode() not in raw and marker.encode() not in inner
            authored = read_authoring(raw)
            assert dict(authored["documents"])["guide/overview.md"] == original
            assert dict(authored["files"])["images/photo.png"] == (directory / "images/photo.png").read_bytes()
            page.goto(saved.as_uri())
            page.locator(".module-tab").filter(has_text="guide").click()
            expect(page.locator(".prose .doc-title")).to_have_text("Notes")
            assert page.evaluate("window.MD_EXECUTED") is None
            page.get_by_role("link", name="Overview", exact=True).click()
            expect(page.locator(".prose .doc-title")).to_have_text("Overview")
            expect(page.get_by_role("img", name="Local picture", exact=True)).to_be_visible()
            expect(page.locator(".prose svg")).to_have_count(1)
            page.locator("#fh-ai-launch").click()
            page.locator('[data-ai-tab="settings"]').click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
            # Updating the same paths replaces content rather than appending
            # duplicate articles, and remains usable without model credentials.
            updated = original.replace("Exact original Markdown.", "Updated original Markdown.")
            (directory / "guide/overview.md").write_bytes(updated.encode())
            page.locator('[data-ai-tab="knowledge"]').click()
            page.get_by_label("选择手册文档目录", exact=True).set_input_files(directory)
            expect(page.locator("#fh-update-download")).to_be_enabled()
            updated_file = tmp_path / "updated.html"
            with page.expect_download(timeout=30000) as downloaded:
                page.locator("#fh-update-download").click()
            downloaded.value.save_as(updated_file)
            content = read_authoring(updated_file.read_bytes())
            assert len(content["documents"]) == 2
            assert dict(content["documents"])["guide/overview.md"] == updated
            assert len(content["files"]) == 1
            # Replacing a picture alone must not require an unrelated Markdown
            # edit to enable saving or refresh the rendered article image.
            page.goto(updated_file.as_uri())
            page.locator("#fh-ai-launch").click()
            page.locator('[data-ai-tab="knowledge"]').click()
            Image.new("RGB", (60, 30), "purple").save(directory / "images/photo.png")
            page.get_by_label("选择手册文档目录", exact=True).set_input_files(directory)
            expect(page.locator("#fh-update-download")).to_be_enabled()
            pictures_file = tmp_path / "pictures.html"
            with page.expect_download(timeout=30000) as downloaded:
                page.locator("#fh-update-download").click()
            downloaded.value.save_as(pictures_file)
            pictures = read_authoring(pictures_file.read_bytes())
            assert dict(pictures["documents"])["guide/overview.md"] == updated
            expected_image = (directory / "images/photo.png").read_bytes()
            assert dict(pictures["files"])["images/photo.png"] == expected_image
            updated_pages = json.loads(
                BeautifulSoup(unpack(pictures_file.read_bytes()), "html.parser").find(id="fh-book-pages").string
            )
            assert base64.b64encode(expected_image).decode() in json.dumps(updated_pages)
            assert not network and not errors, (network, errors)
        finally:
            browser.close()
    payload, _, _, _ = project(materials, saved.read_bytes(), filename="independent-handbook.html")
    assert payload["reader_mode"] == "original" and len(payload["native_documents"]) == 1
    native = payload["native_documents"][0]
    assert native["format"] == "standalone-handbook"
    assert len([d for d in payload["documents"] if "native" in d]) == 2
    view = BeautifulSoup(native["pages"]["__handbook__"], "html.parser")
    assert not view.select("#fh-ai-runtime,#handbook-template-integration,#template-document-update,#vks-template-data")
    readonly = tmp_path / "readonly.html"
    readonly.write_text(native["pages"]["__handbook__"])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(readonly.as_uri())
            page.locator(".module-tab").filter(has_text="guide").click()
            page.get_by_role("link", name="Overview", exact=True).click()
            expect(page.get_by_role("img", name="Local picture", exact=True)).to_be_visible()
            expect(page.locator(".prose svg")).to_have_count(1)
            assert not errors
        finally:
            browser.close()
