"""Unbound local knowledge matches use the shared safe Markdown reader offline."""

from base64 import b64decode, b64encode
from io import BytesIO

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from tests.ui import test_readonly_reader as reader_tests
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader_tests.reader_file


@pytest.fixture
def related_library(reader_file):
    image = BytesIO()
    Image.new("RGB", (96, 48), "#28795b").save(image, format="PNG")
    section = """### 3. **创建任务**与`任务名称`

**中文加粗。**紧跟中文，必须填写`任务名称`。

1. 点击创建任务
2. 填写名称
   - 保留嵌套列表
   - 选择负责人

| 字段 | 要求 |
| --- | --- |
| 任务名称 | **必填** |

![章节原始图片][picture]

```mermaid
flowchart LR
  A[完整原始节点] --> B[完整原始关系]
```

[跨文章引用](other.md#负责人设置)

末尾原文仍然保留，章节预览不得按一百六十字截断 Markdown。

<script>window.localPreviewExecuted=true</script>
"""
    documents = []

    def update(payload):
        payload["bindings"] = []
        doc = payload["documents"][0]
        doc["namespace"] = "synthetic-manual"
        payload["documents"][1]["namespace"] = "synthetic-manual"
        doc["markdown"] = section + "\n[picture]: images/local.png\n"
        doc["images"] = {"images/local.png": "data:image/png;base64," + b64encode(image.getvalue()).decode()}
        doc["sections"] = [
            {
                "id": "s1",
                "title": "3. **创建任务**与`任务名称`",
                "content": section,
                "heading_level": 3,
                "start_line": 1,
            }
        ]
        documents.extend(payload["documents"])

    reader_tests.rewrite_library(reader_file, update)
    return reader_file, documents


@pytest.mark.parametrize("width", [1440, 390])
def test_unbound_local_matches_render_complete_safe_markdown_and_open_original(related_library, width):
    path, documents = related_library
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""(() => {
          const parse = JSON.parse;
          JSON.parse = function (...args) {
            const value = parse.apply(this, args);
            if (value?.project?.id === 'test' && value.documents)
              window.relatedSourceDocuments = value.documents;
            return value;
          };
        })();""")
        try:
            page.goto(path.as_uri())
            page.frame_locator(".prototype-frame").get_by_role("button", name="创建任务", exact=True).click(
                button="right"
            )
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.get_by_role("tab", name="相关知识", exact=True)).to_have_attribute("aria-selected", "true")
            expect(page.locator(".assistant-body > .eyebrow")).to_have_text("本地自动匹配")
            card = page.locator(".knowledge-match").first
            title = card.locator(".knowledge-match-title")
            expect(title.locator("h3")).to_have_text("3. 创建任务与任务名称")
            expect(title.locator("strong")).to_have_text("创建任务")
            expect(title.locator("code")).to_have_text("任务名称")
            assert not any(mark in title.inner_text() for mark in ["###", "**", "`"])
            preview = card.locator(".knowledge-match-preview")
            assert preview.evaluate("node => node.clientHeight") <= 224
            expect(preview.locator("p").first.locator("strong")).to_have_text("中文加粗。")
            assert (
                preview.locator("p").first.locator("strong").evaluate("node => getComputedStyle(node).display")
                == "inline"
            )
            expect(preview.locator("p").first.locator("code")).to_have_text("任务名称")
            expect(preview.locator("ol > li")).to_have_count(2)
            expect(preview.locator("ol ul > li")).to_have_text(["保留嵌套列表", "选择负责人"])
            card.get_by_role("button", name="展开章节预览", exact=True).click()
            expect(card.get_by_role("button", name="收起章节预览", exact=True)).to_have_attribute(
                "aria-expanded", "true"
            )
            expect(preview.locator("thead th")).to_have_text(["字段", "要求"])
            expect(preview.locator("tbody strong")).to_have_text("必填")
            image = preview.get_by_role("img", name="章节原始图片", exact=True)
            image.scroll_into_view_if_needed()
            expect(image).to_be_visible()
            assert image.evaluate("node => node.naturalWidth") == 96
            diagram = preview.get_by_role("img", name="Mermaid 图表", exact=True)
            preview.locator("figure.md-diagram").scroll_into_view_if_needed()
            expect(diagram).to_be_visible(timeout=20000)
            svg = b64decode(diagram.get_attribute("src").split(",", 1)[1]).decode()
            assert "完整原始节点" in svg and "完整原始关系" in svg
            expect(preview).to_contain_text("末尾原文仍然保留，章节预览不得按一百六十字截断 Markdown。")
            assert page.evaluate("window.localPreviewExecuted") is None
            assert page.locator(".assistant").evaluate("node => node.scrollWidth <= node.clientWidth")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            preview.get_by_role("link", name="跨文章引用", exact=True).click()
            expect(page.locator(".document > h1")).to_have_text("其他文档")
            expect(page.locator(".chapter-link[aria-current='location']")).to_have_text("负责人设置")
            page.get_by_role("button", name="交互原型", exact=True).click()
            page.get_by_role("tab", name="相关知识", exact=True).click()
            card = page.locator(".knowledge-match").first
            card.get_by_role("button", name="▤ 任务操作手册", exact=True).click()
            expect(page.locator(".document > h1")).to_have_text("任务操作手册")
            assert page.url.endswith("#section=s1")
            expect(page.locator(".document .rich-markdown strong").first).to_have_text("创建任务")
            assert page.evaluate("window.relatedSourceDocuments") == documents
            assert not [url for url in requests if url.startswith(("http:", "https:"))]
            assert not errors
        finally:
            browser.close()
