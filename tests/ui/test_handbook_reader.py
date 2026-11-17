"""The clean source framework is shared by Studio and portable file:// readers."""

import re
from base64 import b64encode

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.ui.test_frontend_browser import chromium_path


@pytest.fixture
def handbook_file(tmp_path):
    diagram = "```mermaid\nflowchart LR\n  A[Source] --> B[Result]\n```"
    markdown = (
        "# Overview\n\n## Introduction\n\n**Strong original text.**\n\n![Original asset](images/local.svg)\n\n"
        + diagram
    )
    payload = {
        "schema_version": 1,
        "reader_mode": "handbook",
        "project": {"id": "synthetic", "name": "Synthetic Atlas"},
        "snapshot": {"id": "v1", "name": "Synthetic handbook"},
        "prototypes": [],
        "documents": [
            {
                "id": "a",
                "title": "Overview",
                "path": "guide/overview.md",
                "namespace": "first",
                "markdown": markdown,
                "images": {
                    "images/local.svg": "data:image/svg+xml;base64,"
                    + b64encode(
                        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="teal"/></svg>'
                    ).decode()
                },
                "sections": [
                    {"id": "s0", "title": "Overview", "content": "# Overview", "heading_level": 1, "start_line": 1},
                    {
                        "id": "s1",
                        "title": "Introduction",
                        "content": markdown.split("\n\n", 1)[1],
                        "heading_level": 2,
                        "start_line": 3,
                    },
                ],
            },
            {
                "id": "b",
                "title": "Rules",
                "path": "business/rules.md",
                "namespace": "first",
                "sections": [
                    {
                        "id": "s2",
                        "title": "Rule detail",
                        "content": "## Rule detail\n\nExact rule body.",
                        "heading_level": 2,
                    }
                ],
            },
            {
                "id": "c",
                "title": "Other source",
                "path": "other.md",
                "namespace": "second",
                "sections": [{"id": "s3", "title": "Other source", "content": "# Other source"}],
            },
        ],
        "navigation": {
            "packages": [
                {
                    "id": "one",
                    "name": "First source",
                    "categories": [
                        {
                            "id": "intro",
                            "name": "Introduction",
                            "path": "guide",
                            "children": [
                                {
                                    "kind": "folder",
                                    "id": "guide",
                                    "name": "Guide",
                                    "path": "guide",
                                    "children": [
                                        {"kind": "document", "id": "a", "name": "Overview", "path": "guide/overview.md"}
                                    ],
                                }
                            ],
                        },
                        {
                            "id": "business",
                            "name": "Business",
                            "path": "business",
                            "children": [{"kind": "document", "id": "b", "name": "Rules", "path": "business/rules.md"}],
                        },
                        {
                            "id": "unselected",
                            "name": "Unselected category",
                            "path": "private",
                            "children": [
                                {"kind": "document", "id": "secret", "name": "Unselected secret", "path": "private.md"}
                            ],
                        },
                    ],
                },
                {
                    "id": "two",
                    "name": "Second source",
                    "categories": [
                        {
                            "id": "other",
                            "name": "Other",
                            "path": "",
                            "children": [{"kind": "document", "id": "c", "name": "Other source", "path": "other.md"}],
                        }
                    ],
                },
            ]
        },
        "bindings": [],
        "rag": {"chunks": []},
        "ai": {"model": ""},
    }
    path = tmp_path / "clean-handbook.html"
    path.write_bytes(html_library(payload))
    return path


def test_handbook_export_keeps_framework_markdown_exact_links_and_readonly(handbook_file, tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(handbook_file.as_uri())
            book = page.get_by_role("region", name="知识手册阅读模板")
            expect(book.get_by_role("heading", name="Synthetic Atlas 项目知识手册")).to_be_visible()
            expect(book.get_by_text("Unselected category", exact=True)).to_have_count(0)
            expect(book.get_by_text("Unselected secret", exact=True)).to_have_count(0)
            expect(book.get_by_text("没有匹配文档", exact=True)).to_have_count(0)
            expect(book.get_by_role("button", name="更新文档", exact=True)).to_have_count(0)
            assert book.get_attribute("data-template-version") == "1.0.0"
            page.screenshot(path=str(tmp_path / "portable-handbook-cover.png"))
            book.get_by_role("button", name="开始阅读", exact=True).click()
            expect(book.locator(".hb-article h1")).to_have_text("Overview")
            expect(book.locator(".hb-toc .document-link[aria-current='page']")).to_contain_text("Overview")
            expect(book.locator(".hb-article strong").get_by_text("Strong original text.", exact=True)).to_be_visible()
            expect(book.get_by_role("img", name="Original asset", exact=True)).to_be_visible()
            expect(book.get_by_role("img", name="Mermaid 图表", exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path / "portable-handbook-article.png"))
            book.get_by_role("navigation", name="文章翻页").get_by_role(
                "button", name="下一篇：Rules", exact=True
            ).click()
            expect(book.locator(".hb-article h1")).to_have_text("Rules")
            expect(
                book.get_by_role("navigation", name="文章翻页").get_by_role("button", name=re.compile("Other source"))
            ).to_have_count(0)
            book.get_by_role("button", name="搜索全文", exact=True).click()
            page.get_by_role("textbox", name="搜索知识库", exact=True).fill("Strong original")
            page.locator(".search-result").first.click()
            expect(page).to_have_url(re.compile(r"#section=s1$"))
            expect(book.locator(".hb-article h1")).to_have_text("Overview")
            book.get_by_role("button", name="向手册 AI 提问", exact=True).click()
            expect(page.get_by_role("tab", name="回答", exact=True)).to_be_visible()
            assert not errors, errors
            assert not [url for url in requests if url.startswith("http")]
        finally:
            browser.close()


def test_handbook_export_mobile_directory_and_deep_link(handbook_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 800})
        try:
            page.goto(handbook_file.as_uri() + "#section=s2")
            book = page.get_by_role("region", name="知识手册阅读模板")
            expect(book.locator(".hb-article h1")).to_have_text("Rules")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            book.get_by_role("button", name="打开手册目录", exact=True).click()
            expect(book.get_by_label("手册目录", exact=True)).to_be_visible()
            book.get_by_label("文档资料包", exact=True).select_option("two")
            expect(book.locator(".hb-article h1")).to_have_text("Other source")
            expect(book.get_by_role("button", name="关闭手册目录", exact=True)).to_be_hidden()
            page.keyboard.press("Control+k")
            expect(page.get_by_role("textbox", name="搜索知识库", exact=True)).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.get_by_role("textbox", name="搜索知识库", exact=True)).to_have_count(0)
        finally:
            browser.close()
