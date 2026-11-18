"""Actual SSE sockets: prose is visible before completion, cancellation and fences."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api import ai_gateway, model_credentials
from vks_api.ai_gateway import ModelPolicy, ModelUsage
from vks_api.model_stream import CURRENT_MODEL_STREAM, ModelStream, explanation_prefix
from vks_api.models import Relation

from tests.interactive import test_assistant
from tests.interactive.test_native_documents_browser import native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
REAL_REQUEST_MODEL = ai_gateway.request_model


@pytest.fixture(params=["responses", "chat_completions"])
def streaming_provider(monkeypatch, request):
    state = SimpleNamespace(
        protocol=request.param, entered=threading.Event(), release=threading.Event(), calls=[], cut=False, invalid=False
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(payload)
            messages = payload.get("input", payload.get("messages"))
            source = json.loads(messages[-1]["content"])
            rid = source["chapters"][0]["revision_id"]
            answer = json.dumps(
                {
                    "title": "流式回答",
                    "explanation": "第一段 **流式内容**。\n\n第二段已完成。",
                    "section_revision_ids": ["invalid" if state.invalid else rid],
                    "confidence": 0.95,
                    "unknowns": [],
                },
                ensure_ascii=False,
            )
            first, last = answer.split("第二段", 1)
            last = "第二段" + last
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def event(data):
                self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\r\n\r\n").encode())
                self.wfile.flush()

            def delta(text):
                event(
                    {"type": "response.output_text.delta", "delta": text}
                    if state.protocol == "responses"
                    else {
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                    }
                )

            try:
                delta(first)
                state.entered.set()
                assert state.release.wait(15)
                if state.cut:
                    return
                delta(last)
                if state.protocol == "responses":
                    event(
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "usage": {"input_tokens": 123, "output_tokens": 45},
                                "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
                            },
                        }
                    )
                else:
                    event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    event({"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 45}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    monkeypatch.setattr(model_credentials, "key_for_ref", lambda *args: "test-stream-key")
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configure(assistant, provider, monkeypatch):
    monkeypatch.setattr(ai_gateway, "request_model", REAL_REQUEST_MODEL)
    (web, service, owner, pid, aid, grant, package, section, _), body, _calls = assistant
    with service.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = {
            **policy.settings,
            "endpoint": provider.endpoint,
            "protocol": provider.protocol,
            "max_output_tokens": 8000,
        }
    return web, service, pid, aid, grant, body


def test_prefix_parser_does_not_expose_json_or_broken_unicode():
    value = {"title": 'The word "explanation"', "section_revision_ids": [], "explanation": '你好\n"quoted" 😀 \\ end'}
    source = json.dumps(value, ensure_ascii=True)
    prior = ""
    for n in range(len(source) + 1):
        current = explanation_prefix(source[:n])
        assert current.startswith(prior)
        assert not any(0xD800 <= ord(c) <= 0xDFFF for c in current)
        prior = current
    assert prior == value["explanation"]
    assert explanation_prefix('{"title":"explanation: fake') == ""


def test_transport_publishes_before_terminal_and_preserves_usage(streaming_provider):
    provider = streaming_provider
    parts = []
    state = ModelStream(lambda kind, value: parts.append(value["text"]))
    config = {"endpoint": provider.endpoint, "protocol": provider.protocol, "allow_external": True}
    messages = [{"role": "user", "content": json.dumps({"chapters": [{"revision_id": "chapter"}]})}]
    body = (
        {"model": "synthetic", "max_output_tokens": 8000, "input": messages}
        if provider.protocol == "responses"
        else {"model": "synthetic", "max_completion_tokens": 8000, "messages": messages}
    )

    def run():
        token = CURRENT_MODEL_STREAM.set(state)
        try:
            return ai_gateway.request_model(config, body)
        finally:
            CURRENT_MODEL_STREAM.reset(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run)
        assert provider.entered.wait(5)
        deadline = time.monotonic() + 5
        while not parts and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "流式内容" in "".join(parts) and not task.done()
        provider.release.set()
        result = task.result(timeout=5)
    assert "第二段已完成" in "".join(parts)
    assert result["usage"].get("output_tokens", result["usage"].get("completion_tokens")) == 45
    assert provider.calls[0]["stream"] is True
    assert provider.calls[0].get("max_output_tokens", provider.calls[0].get("max_completion_tokens")) == 8000
    if provider.protocol == "chat_completions":
        assert provider.calls[0]["stream_options"]["include_usage"] is True


@pytest.mark.parametrize("failure", ["truncated", "bad_citation"])
def test_stream_failure_never_publishes_a_verified_answer(assistant, streaming_provider, failure, monkeypatch):
    provider = streaming_provider
    web, service, pid, aid, grant, body = configure(assistant, provider, monkeypatch)
    with service.db() as s:
        before = {
            r.id: (r.from_revision_id, r.to_revision_id, r.row_version)
            for r in s.scalars(select(Relation).where(Relation.analysis_set_id == aid))
        }
    provider.release.set()
    provider.cut = failure == "truncated"
    provider.invalid = failure == "bad_citation"
    response = web.post(
        test_assistant.root(pid) + "/conversation",
        json={**body, "question": "新增收费项需要什么"},
        headers={**_headers(grant, 0, "stream"), "Accept": "text/event-stream"},
    )
    assert response.status_code == 200 and "event: delta" in response.text
    assert "event: error" in response.text and "event: complete" not in response.text
    with service.db() as s:
        assert {
            r.id: (r.from_revision_id, r.to_revision_id, r.row_version)
            for r in s.scalars(select(Relation).where(Relation.analysis_set_id == aid))
        } == before
        assert s.scalar(select(ModelUsage).where(ModelUsage.project_id == pid)).state == "failed"


def test_browser_live_deltas_stop_and_completed_only_history(assistant, streaming_provider, tmp_path, monkeypatch):
    provider = streaming_provider
    web, service, pid, aid, _grant, _body = configure(assistant, provider, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            page.goto(url + f"/#project={pid}&analysis={aid}&view=prototype")
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            source.get_by_role("button", name="Add", exact=True).click()
            source.get_by_label("Code", exact=True).click()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            panel = page.get_by_role("complementary", name="知识助手")
            panel.get_by_role("tab", name="回答", exact=True).click()
            field = panel.get_by_label("向知识助手提问", exact=True)
            field.fill("新增收费项需要什么")
            panel.get_by_role("button", name="发送问题", exact=True).click()
            preview = panel.get_by_label("正在生成的回答", exact=True)
            expect(preview).to_contain_text("流式内容")
            assert not provider.release.is_set()
            expect(panel.locator(".chat-turn")).to_have_count(0)
            expect(panel.locator(".chat-sources")).to_have_count(0)
            provider.release.set()
            expect(panel.locator(".chat-turn")).to_have_count(1)
            expect(panel.locator(".chat-answer")).to_contain_text("第二段已完成")
            expect(panel.locator(".chat-sources")).to_have_count(1)
            provider.entered.clear()
            provider.release.clear()
            field.fill("继续说明使用要求")
            panel.get_by_role("button", name="发送问题", exact=True).click()
            expect(preview).to_contain_text("流式内容")
            panel.get_by_role("button", name="停止等待", exact=True).click()
            expect(panel.locator(".chat-turn")).to_have_count(2)
            expect(panel.locator(".chat-turn").last).to_contain_text("未完成")
            expect(field).to_have_value("继续说明使用要求")
            deadline = time.monotonic() + 3
            while True:
                with service.db() as s:
                    states = list(s.scalars(select(ModelUsage.state).where(ModelUsage.project_id == pid)))
                if "reserved" not in states or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            assert sorted(states) == ["failed", "succeeded"]
            with service.db() as s:
                stopped = s.scalar(select(ModelUsage).where(ModelUsage.project_id == pid, ModelUsage.state == "failed"))
                assert stopped.input_tokens + stopped.output_tokens > 0
            provider.release.set()
            field.fill("请详细说明注意事项")
            panel.get_by_role("button", name="发送问题", exact=True).click()
            expect(panel.locator(".chat-turn")).to_have_count(3)
            last = provider.calls[-1]
            data = json.loads(last.get("input", last.get("messages"))[-1]["content"])
            assert len(data["history"]) == 2
        finally:
            provider.release.set()
            browser.close()
