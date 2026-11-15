"""The optional local author updates source facts without exporting credentials."""

from copy import deepcopy

from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.ui.test_frontend_browser import chromium_path
from tests.ui.test_readonly_reader import configure


def payload(title="Original", content="OLD_SOURCE_FACT"):
    return {
        "schema_version": 1,
        "authoring": "vks-handbook-authoring-1",
        "reader_mode": "handbook",
        "project": {"id": "vks-handbook-template", "name": "本地知识手册"},
        "snapshot": {"id": "authoring", "name": "可编辑模板"},
        "prototypes": [],
        "documents": [
            {
                "id": "doc-" + title,
                "title": title,
                "path": title + ".md",
                "namespace": "authoring",
                "markdown": "# " + title + "\n\n" + content,
                "sections": [
                    {
                        "id": "section-" + title,
                        "title": title,
                        "heading_level": 1,
                        "start_line": 1,
                        "content": "# " + title + "\n\n" + content,
                    }
                ],
                "images": {},
            }
        ],
        "navigation": {"packages": []},
        "bindings": [],
        "native_documents": [],
        "rag": {"chunks": []},
        "warnings": [],
        "ai": {"base_url": "https://api.openai.com/v1", "protocol": "responses", "model": ""},
    }


def test_authoring_replace_preserves_personal_connection_and_fences_old_stream(tmp_path):
    path = tmp_path / "authoring-reader.html"
    path.write_bytes(html_library(payload()))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            pending, calls = [], []

            def respond(route):
                headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"}
                if route.request.method == "OPTIONS":
                    route.fulfill(status=204, headers=headers)
                elif route.request.url.endswith("/models"):
                    route.fulfill(json={"data": [{"id": "fixture-chat"}]}, headers=headers)
                else:
                    calls.append((route.request.headers, route.request.post_data_json))
                    pending.append(route)

            page.route("https://api.example.test/**", respond)
            page.goto(path.as_uri())
            page.wait_for_function("!!window.VksHandbookAuthoring")
            configure(page)
            page.get_by_label("向 AI 提问", exact=True).fill("Original")
            with page.expect_request("https://api.example.test/v1/chat/completions"):
                page.get_by_role("button", name="发送", exact=True).click()
            updated = payload("Updated", "NEW_SOURCE_FACT")
            updated["ai"] = {
                "key": "INCOMING_KEY_MUST_BE_IGNORED",
                "base_url": "https://wrong.invalid",
                "model": "wrong",
            }
            with page.expect_request("https://api.example.test/v1/chat/completions"):
                page.evaluate(
                    """next => {
              VksHandbookAuthoring.replace(next);VksHandbookAuthoring.open('doc-Updated');
              const input=document.querySelector('[aria-label="向 AI 提问"]');
              input.value='Updated';input.dispatchEvent(new Event('input',{bubbles:true}));
              [...document.querySelectorAll('.assistant button')].find(node=>node.textContent==='发送').click();
            }""",
                    updated,
                )
            expect(page.get_by_role("button", name="停止", exact=True)).to_be_visible()
            expect(page.get_by_text("使用个人密钥", exact=True)).to_be_visible()
            page.wait_for_function("document.querySelector('.chat-message-list')?.textContent.includes('Updated')")
            for _ in range(50):
                if len(pending) == 2:
                    break
                page.wait_for_timeout(20)
            assert len(pending) == 2 and len(calls) == 2
            assert calls[1][0]["authorization"] == "Bearer fixture-recipient-only"
            assert "NEW_SOURCE_FACT" in str(calls[1][1]) and "OLD_SOURCE_FACT" not in str(calls[1][1])
            pending[1].fulfill(
                headers={"Access-Control-Allow-Origin": "*", "Content-Type": "text/event-stream"},
                body='data: {"choices":[{"delta":{"content":"Updated answer [1]"}}]}\n\ndata: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            )
            expect(page.get_by_text("Updated answer", exact=False)).to_be_visible()
            assert page.evaluate("VksHandbookAuthoring.currentDocument()") == "doc-Updated"
            invalid = deepcopy(updated)
            invalid["prototypes"] = [{"id": "not-authoring"}]
            assert page.evaluate(
                """value=>{try{VksHandbookAuthoring.replace(value);return false}catch{return true}}""", invalid
            )
            assert page.evaluate("VksHandbookAuthoring.currentDocument()") == "doc-Updated"
            invalid = deepcopy(updated)
            invalid["navigation"] = {"packages": [{"categories": [{}]}]}
            assert page.evaluate(
                """value=>{try{VksHandbookAuthoring.replace(value);return false}catch{return true}}""", invalid
            )
            assert page.evaluate("VksHandbookAuthoring.currentDocument()") == "doc-Updated"
            assert "fixture-recipient-only" not in page.content()
        finally:
            browser.close()


def test_readonly_handbook_never_exposes_authoring_hook(tmp_path):
    readonly = payload()
    readonly.pop("authoring")
    path = tmp_path / "readonly-handbook.html"
    path.write_bytes(html_library(readonly))
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(path.as_uri())
            expect(page.get_by_role("region", name="知识手册阅读模板")).to_be_visible()
            assert page.evaluate("typeof window.VksHandbookAuthoring") == "undefined"
            expect(page.get_by_role("button", name="保存可编辑 HTML", exact=True)).to_have_count(0)
        finally:
            browser.close()
