"""Platform article pagination follows imported navigation in an isolated file reader."""

from base64 import b64decode

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.documents import parse_sections
from vks_api.readonly_export import html_library

from tests.ui.test_frontend_browser import chromium_path


@pytest.fixture
def navigation_library(tmp_path):
    def document(id, title, path, content, namespace="guide"):
        markdown = f"# {title}\n\n{content}"
        return {
            "id": id,
            "title": title,
            "path": path,
            "namespace": namespace,
            "markdown": markdown,
            "sections": [
                {
                    "id": f"{id}-section-{index}",
                    "title": section["title"],
                    "content": section["content"],
                    "start_line": section["startLine"],
                    "heading_level": len(section["heading_path"]),
                }
                for index, section in enumerate(parse_sections(markdown, title))
            ],
        }

    first = document("first", "Z 原始首篇", "intro/z.md", "首篇保留原始排序。")
    middle = document(
        "middle",
        "M 原始次篇",
        "intro/nested/m.md",
        """## 原文与图表

**正文不可改写。**紧跟中文。

| 字段 | 原文 |
| --- | --- |
| 金额 | 100.00 |

```mermaid
flowchart LR
  A[保留原始节点] --> B[保留原始关系]
```

[跨分类引用](../../core/deep.md#准确定位)
""",
    )
    nested = document("nested", "B 第二分类首篇", "core/deep.md", "## 准确定位\n\n跨分类引用的准确章节。")
    last = document(
        "last",
        "A 原始末篇与需要在窄屏完整换行的长文章名称",
        "core/a.md",
        "## 末篇小节\n\n末篇搜索唯一标记。",
    )
    solo = document("solo", "独立资料包单篇", "core/a.md", "不同资料包的同名路径。", namespace="other")

    def leaf(doc):
        return {"kind": "document", "id": doc["id"], "name": doc["title"], "path": doc["path"]}

    payload = {
        "schema_version": 1,
        "project": {"id": "navigation-synthetic", "name": "合成导航知识库"},
        "snapshot": {"id": "fixed", "name": "固定版本"},
        "prototypes": [],
        # This deliberately differs from both the navigation and title order.
        "documents": [last, nested, first, middle, solo],
        "bindings": [],
        "rag": {"chunks": []},
        "ai": {},
        "navigation": {
            "packages": [
                {
                    "id": "guide",
                    "name": "导入资料包",
                    "categories": [
                        {
                            "id": "intro",
                            "name": "第一分类",
                            "children": [
                                leaf(first),
                                {
                                    "kind": "folder",
                                    "id": "intro-nested",
                                    "name": "第一文件夹",
                                    "children": [leaf(middle)],
                                },
                            ],
                        },
                        {
                            "id": "core",
                            "name": "第二分类",
                            "children": [
                                {
                                    "kind": "folder",
                                    "id": "core-nested",
                                    "name": "第二文件夹",
                                    "children": [leaf(nested)],
                                },
                                leaf(last),
                            ],
                        },
                    ],
                },
                {
                    "id": "other",
                    "name": "独立资料包",
                    "categories": [{"id": "solo", "name": "单篇分类", "children": [leaf(solo)]}],
                },
            ]
        },
    }
    path = tmp_path / "navigation.html"
    path.write_bytes(html_library(payload))
    return path, payload


@pytest.fixture
def navigation_page(navigation_library):
    path, payload = navigation_library
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Observe the decoded source data to detect accidental mutations by UI navigation.
        page.add_init_script("""(() => {
          const parse = JSON.parse;
          JSON.parse = function (...args) {
            const value = parse.apply(this, args);
            if (value?.project?.id === 'navigation-synthetic')
              window.navigationSourceDocuments = value.documents;
            return value;
          };
        })();""")
        page.goto(path.as_uri())
        expect(page.locator(".document > h1")).to_have_text("Z 原始首篇")
        try:
            yield page, payload
            assert page.evaluate("window.navigationSourceDocuments") == payload["documents"]
            assert not [url for url in requests if url.startswith(("http:", "https:"))]
            assert not errors
        finally:
            browser.close()


def pagination(page):
    return page.get_by_role("navigation", name="文章翻页", exact=True)


def test_export_pagination_uses_imported_tree_order_and_stops_at_package_edges(navigation_page):
    page, _payload = navigation_page
    expect(pagination(page).get_by_role("button", name="上一篇", exact=False)).to_have_count(0)
    expect(pagination(page).get_by_role("button", name="下一篇：M 原始次篇", exact=True)).to_be_visible()
    pagination(page).get_by_role("button", name="下一篇：M 原始次篇", exact=True).click()
    expect(page.locator(".document > h1")).to_have_text("M 原始次篇")
    expect(page.locator(".document-breadcrumb")).to_contain_text("第一文件夹")
    expect(pagination(page).get_by_role("button", name="上一篇：Z 原始首篇", exact=True)).to_be_visible()
    pagination(page).get_by_role("button", name="下一篇：B 第二分类首篇", exact=True).click()
    expect(page.get_by_role("tab", name="第二分类", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator(".document-breadcrumb")).to_contain_text("第二文件夹")
    expect(page.locator(".document-link[aria-current='page']")).to_contain_text("B 第二分类首篇")
    expect(page.locator(".document > h1")).to_have_text("B 第二分类首篇")
    assert page.url.endswith("#doc=nested")
    pagination(page).get_by_role("button", name="下一篇", exact=False).click()
    expect(page.locator(".document > h1")).to_have_text("A 原始末篇与需要在窄屏完整换行的长文章名称")
    expect(pagination(page).get_by_role("button", name="下一篇", exact=False)).to_have_count(0)
    pagination(page).get_by_role("button", name="上一篇：B 第二分类首篇", exact=True).click()
    pagination(page).get_by_role("button", name="上一篇：M 原始次篇", exact=True).click()
    expect(page.get_by_role("tab", name="第一分类", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator(".document-link[aria-current='page']")).to_contain_text("M 原始次篇")
    page.get_by_label("文档资料包", exact=True).select_option("other")
    expect(page.locator(".document > h1")).to_have_text("独立资料包单篇")
    expect(pagination(page)).to_have_count(0)


def test_export_pagination_keeps_reference_search_source_text_and_mermaid(navigation_page):
    page, _payload = navigation_page
    pagination(page).get_by_role("button", name="下一篇：M 原始次篇", exact=True).click()
    expect(page.locator(".document strong")).to_have_text("正文不可改写。")
    expect(page.locator(".document thead th")).to_have_text(["字段", "原文"])
    expect(page.locator(".document tbody td")).to_have_text(["金额", "100.00"])
    diagram = page.locator(".document figure.md-diagram img[alt='Mermaid 图表']")
    diagram.scroll_into_view_if_needed()
    expect(diagram).to_be_visible(timeout=20000)
    source_svg = b64decode(diagram.get_attribute("src").split(",", 1)[1]).decode()
    assert "保留原始节点" in source_svg and "保留原始关系" in source_svg
    expect(page.locator(".document .document-pagination")).to_have_count(0)
    page.get_by_role("link", name="跨分类引用", exact=True).click()
    expect(page.get_by_role("tab", name="第二分类", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator(".chapter-link[aria-current='location']")).to_have_text("准确定位")
    expect(pagination(page).get_by_role("button", name="上一篇：M 原始次篇", exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role("tab", name="第二分类", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator(".chapter-link[aria-current='location']")).to_have_text("准确定位")
    pagination(page).get_by_role("button", name="上一篇：M 原始次篇", exact=True).click()
    expect(page.get_by_role("tab", name="第一分类", exact=True)).to_have_attribute("aria-selected", "true")
    page.get_by_role("button", name="搜索文档", exact=True).click()
    page.get_by_role("textbox", name="搜索知识库", exact=True).fill("末篇搜索唯一标记")
    page.locator(".search-result").first.click()
    expect(page.locator(".document > h1")).to_have_text("A 原始末篇与需要在窄屏完整换行的长文章名称")
    expect(page.get_by_role("tab", name="第二分类", exact=True)).to_have_attribute("aria-selected", "true")
    expect(pagination(page).get_by_role("button", name="下一篇", exact=False)).to_have_count(0)
    pagination(page).get_by_role("button", name="上一篇：B 第二分类首篇", exact=True).click()
    pagination(page).get_by_role("button", name="上一篇：M 原始次篇", exact=True).click()
    expect(page.locator(".document strong")).to_have_text("正文不可改写。")
    expect(page.locator(".document thead th")).to_have_text(["字段", "原文"])
    expect(page.locator(".document tbody td")).to_have_text(["金额", "100.00"])
    diagram = page.locator(".document figure.md-diagram img[alt='Mermaid 图表']")
    diagram.scroll_into_view_if_needed()
    expect(diagram).to_be_visible(timeout=20000)


@pytest.mark.parametrize("width", [390, 640])
def test_export_pagination_is_keyboard_usable_and_fits_narrow_screens(navigation_page, width):
    page, _payload = navigation_page
    page.set_viewport_size({"width": width, "height": 960})
    for title in ["M 原始次篇", "B 第二分类首篇", "A 原始末篇与需要在窄屏完整换行的长文章名称"]:
        link = pagination(page).get_by_role("button", name=f"下一篇：{title}", exact=True)
        link.focus()
        link.press("Enter")
        expect(page.locator(".document > h1")).to_have_text(title)
        pagination(page).scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator(".doc-scroll").evaluate("node => node.scrollWidth <= node.clientWidth")
        for card in pagination(page).get_by_role("button").all():
            box = card.bounding_box()
            assert box and 0 <= box["x"] < box["x"] + box["width"] <= width
    expect(pagination(page).get_by_role("button", name="下一篇", exact=False)).to_have_count(0)
