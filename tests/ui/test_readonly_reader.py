"""Single-file reader acceptance against file:// with isolated Chromium."""

import gzip
import json
import re
import struct
from base64 import b64decode, b64encode
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

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
                    }
                ],
            },
            {
                "id": "d2",
                "title": "其他文档",
                "path": "other.md",
                "sections": [{"id": "s2", "title": "负责人设置", "content": "# 负责人设置\n\n负责人从当前项目成员中选择。"}],
            },
        ],
        "bindings": [
            {
                "id": "b1",
                "package_id": "p1",
                "scope": "control",
                "target": {"route": "/", "frame_path": [], "selector": "#taskcreate", "label": "创建任务", "state": "", "open_steps": []},
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
    source = (ASSETS / "template.html").read_text()
    source = source.replace("/* VKS_READER_CSS */", (ASSETS / "reader.css").read_text())
    source = source.replace("VKS_READER_PAYLOAD", b64encode(gzip.compress(json.dumps(payload, ensure_ascii=False).encode())).decode())
    source = source.replace("/* VKS_READER_JS */", (ASSETS / "reader.js").read_text())
    path = tmp_path / "library.html"
    path.write_text(source)
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
            page.get_by_role("button", name="▤ 任务操作手册 · 创建任务", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            expect(page.locator(".document table")).to_be_visible()
            assert page.evaluate("window.docExecuted") is None
            expect(page.get_by_text("<script>window.docExecuted=true</script>", exact=True)).to_be_visible()
            page.get_by_role("link", name="其他文档", exact=True).click()
            expect(page.locator(".document h1")).to_have_text("其他文档")
            page.get_by_role("button", name="搜索文档", exact=True).click()
            page.get_by_role("textbox", name="搜索知识库", exact=True).fill("任务名称")
            page.locator(".search-result").first.click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            page.get_by_role("button", name="↗ 创建任务", exact=True).click()
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
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator(".answer").last).to_contain_text("任务名称和负责人均为必填")
            expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
            page.locator(".chat-citations button").first.click()
            expect(page.locator(".document h1")).to_have_text("任务操作手册")
            assert len([call for call in calls if call[0].endswith("/chat/completions")]) == 1
            assert page.evaluate("localStorage.length") == 0
            assert page.evaluate("sessionStorage.length") == 0
            assert "fixture-recipient-only" not in reader_file.read_text()
            page.reload()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            page.get_by_role("button", name="连接设置", exact=True).click()
            expect(page.get_by_label("API Key", exact=True)).to_have_value("")
        finally:
            browser.close()


def test_response_stream_truncation_is_reported_and_context_does_not_trigger_model(reader_file):
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
                route.fulfill(headers={**headers, "Content-Type": "text/event-stream"}, body='data: {"type":"response.output_text.delta","delta":"部分回答"}\n\n')

        page.route("https://api.example.test/v1/**", respond)
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("接口协议", exact=True).select_option("responses")
            page.get_by_role("button", name="应用并开始提问", exact=True).click()
            frame = page.frame_locator("iframe.prototype-frame")
            frame.get_by_role("button", name="创建任务", exact=True).click(button="right")
            page.get_by_role("button", name="AI 讲解这个功能", exact=False).click()
            assert not [call for call in calls if call.endswith("/responses")]
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator(".chat-message .error")).to_have_text("连接提前结束，回答可能不完整，请重试。")
            expect(page.locator(".answer").last).to_have_text("部分回答")
            assert len([call for call in calls if call.endswith("/responses")]) == 1
        finally:
            browser.close()


def rewrite_library(path, update):
    source = path.read_text()
    encoded = re.search(r'id="vks-payload"[^>]*>(.*?)</script>', source, re.DOTALL).group(1)
    payload = json.loads(gzip.decompress(b64decode(encoded)))
    update(payload)
    source = (ASSETS / "template.html").read_text()
    source = source.replace("/* VKS_READER_CSS */", (ASSETS / "reader.css").read_text())
    source = source.replace("VKS_READER_PAYLOAD", b64encode(gzip.compress(json.dumps(payload, ensure_ascii=False).encode())).decode())
    source = source.replace("/* VKS_READER_JS */", (ASSETS / "reader.js").read_text())
    path.write_text(source)


def test_nested_static_site_and_document_links_stay_in_material_namespace(reader_file):
    from vks_api.material_site import site_html

    def update(payload):
        payload["prototypes"][0]["html"] = site_html(
            "index.html",
            [
                ("index.html", '<!doctype html><meta charset=utf-8><title>任务管理</title><a id="next" href="help.html">查看说明</a>'.encode()),
                ("help.html", '<!doctype html><meta charset=utf-8><title>使用说明</title><h1>使用说明页面</h1><a href="index.html">返回任务</a>'.encode()),
            ],
        ).decode()
        for doc in payload["documents"]:
            doc["namespace"] = "package-A"
        payload["documents"].extend([
            {"id": "d3", "namespace": "package-B", "path": "任务操作手册.md", "title": "B 操作手册", "sections": [{"id": "s3", "title": "B 操作", "content": "# B 操作\n\n[其他文档](other.md)"}]},
            {"id": "d4", "namespace": "package-B", "path": "other.md", "title": "B 其他文档", "sections": [{"id": "s4", "title": "B 负责人", "content": "# B 负责人\n\n这里是 B 包的说明。"}]},
        ])

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
            page.get_by_role("button", name="模块知识", exact=True).click()
            expect(page.locator(".assistant-context")).to_contain_text("当前模块")
            page.get_by_role("button", name="知识文档", exact=True).click()
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
        stream = 'data: ' + json.dumps({"type": "response.output_text.delta", "delta": text}, ensure_ascii=False) + '\r\n\r\n' + 'data: {"type":"response.completed","response":{"status":"completed"}}\r\n\r\n'
        page.add_init_script("""stream => {
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
        }""".replace("stream => {", "(() => { const stream=" + json.dumps(stream) + ";") + ")();")
        page.route("https://api.example.test/v1/models", lambda route: route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}))
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_role("button", name="连接设置", exact=True).click()
            page.get_by_label("接口协议", exact=True).select_option("responses")
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
