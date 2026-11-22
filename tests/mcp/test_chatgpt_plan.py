"""Exercise official OAuth/Responses shapes against synthetic, isolated transports."""

import base64
import gzip
import hashlib
import json
import os
import stat
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from vks_api import connector_source  # noqa: F401
from vks_connect import chatgpt_plan
from vks_connect.chatgpt_plan import ISSUER, PERMISSION, RESOURCE, PlanError, PlanSession
from vks_connect.credentials import CredentialError
from vks_connect.library import LibraryStore
from vks_connect.library_reader import LibraryReader, ReaderHandler, reader_page


class SecretStore:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def set(self, key, value):
        self.items[key] = value

    def remove(self, key):
        self.items.pop(key, None)


def completed(text):
    return {
        "status": "completed", "usage": {"input_tokens": 12, "output_tokens": 8},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


@pytest.mark.parametrize(("grant", "local"), [(True, False), (False, False), (True, True)])
def test_oauth_grant_reuse_rotation_and_fixed_responses(tmp_path, monkeypatch, grant, local):
    secrets = SecretStore()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key["kid"] = "synthetic-key"
    authorization = {}
    errors = []
    seen = []

    def browser(url):
        authorization.update({name: values[0] for name, values in parse_qs(urlsplit(url).query).items()})
        assert authorization["client_id"] == ("issued-client" if local else "dynamic_agent_client")
        if local:
            assert authorization["prompt"] == "consent"
        assert authorization["resource"] == RESOURCE
        def return_to_app():
            try:
                target = authorization["redirect_uri"]
                wrong = httpx.get(target, params={"state": "wrong", "code": "stranger"}, trust_env=False)
                assert wrong.status_code == 400
                response = httpx.get(target, params={
                    "state": authorization["state"], "code": "synthetic-code", "client_id": "issued-client",
                }, trust_env=False)
                assert response.status_code == 200
            except Exception as exc:
                errors.append(exc)
        threading.Thread(target=return_to_app, daemon=True).start()
        return True

    def provider(request):
        seen.append(urlsplit(str(request.url)).path)
        if request.url.path.endswith("/oauth/token"):
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["issued-client"]
            assert form["resource"] == [RESOURCE]
            scope = PERMISSION if grant else "openid profile"
            if form["grant_type"] == ["refresh_token"]:
                assert form["refresh_token"] == ["first-refresh"]
                return httpx.Response(200, json={"access_token": "rotated-access", "refresh_token": "rotated-refresh",
                                                "expires_in": 3600, "scope": scope})
            verifier = form["code_verifier"][0]
            assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == authorization["code_challenge"]
            assert form["redirect_uri"] == [authorization["redirect_uri"]]
            identity = jwt.encode({
                "iss": ISSUER, "aud": "issued-client", "sub": "test-sub", "nonce": authorization["nonce"],
                "email": "synthetic@example.test", "exp": int(time.time()) + 60,
            }, private, algorithm="RS256", headers={"kid": "synthetic-key"})
            return httpx.Response(200, json={"id_token": identity, "access_token": "first-access",
                                            "refresh_token": "first-refresh", "expires_in": 3600, "scope": scope})
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": ISSUER + "/jwks", "revocation_endpoint": ISSUER + "/revoke"})
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [key]})
        if request.url.path == "/v1/models":
            assert request.headers["authorization"] == "Bearer rotated-access"
            return httpx.Response(200, json={"models": [{"slug": "test", "display_name": "Test", "visibility": "list"}]})
        if request.url.path == "/revoke":
            assert parse_qs(request.content.decode())["token"] == ["rotated-refresh"]
            return httpx.Response(200)
        assert request.url.path == "/v1/responses" and request.url.host == "api.openai.com"
        body = json.loads(request.content)
        assert body["store"] is False and body["stream"] is True
        assert body["input"][0]["role"] == "developer"
        assert "max_output_tokens" not in body and "tools" not in body
        return httpx.Response(200, headers={"content-type": "Text/Event-Stream; charset=utf-8"}, text=
            "data: " + json.dumps({"type": "response.completed", "response": completed("synthetic")}) + "\n\n")

    http = httpx.Client(transport=httpx.MockTransport(provider))
    credentials = None if local else lambda: secrets
    if local:
        if os.name != "posix":
            pytest.skip("macOS file permissions")
        monkeypatch.setattr(chatgpt_plan, "sys", SimpleNamespace(platform="darwin"))
        monkeypatch.setattr(chatgpt_plan, "credential_store", lambda: pytest.fail("macOS opened Keychain"))
    session = PlanSession(tmp_path, credentials=credentials, client=http, open_browser=browser)
    legacy = {"id": "legacy-profile", "connected": True, "client_id": "issued-client"}
    if local:
        with session.database(write=True) as db:
            session.write(db, "profile", legacy)
        assert not session.status()["connected"]
    session.begin()
    session.pending.join(8)
    assert not session.pending.is_alive() and not errors
    active = session.status()
    assert active["connected"] is grant and active["account"] == "synthetic@example.test", active
    assert "access_token" not in str(active)
    if not grant:
        assert not secrets.items
        with pytest.raises(PlanError, match="授权"):
            session.models(active["profile_id"])
        assert "/v1/responses" not in seen
        return
    restored = PlanSession(tmp_path, credentials=credentials, client=http)
    assert restored.models(active["profile_id"]) == [{"id": "test", "name": "Test"}]
    answer = restored.generate(active["profile_id"], {
        "model": "test", "input": [{"role": "system", "content": "safe"}],
        "max_output_tokens": 2000, "tools": [{"type": "web_search"}],
    })
    assert answer["usage"]["input_tokens"] == 12
    stored = restored.credentials()
    reference = restored.ref(restored.profile())
    assert stored.get(reference) == "rotated-refresh"
    if local:
        path = stored.directory / reference
        assert stat.S_IMODE(stored.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        with pytest.raises(CredentialError, match="仅允许当前用户"):
            stored.get(reference)
        path.chmod(0o600)
    restored.disconnect()
    assert not session.status()["connected"] and stored.get(reference) is None
    if local:
        with session.database() as db:
            assert session.read(db, "profile") == legacy
    database = (tmp_path / "chatgpt/connections.sqlite3").read_bytes()
    assert b"first-refresh" not in database and b"rotated-access" not in database
    assert b"rotated-refresh" not in database


def test_unexpected_chatgpt_response_reports_type_without_body(tmp_path, monkeypatch):
    def provider(_):
        return httpx.Response(200, headers={"content-type": "text/html", "x-request-id": "req_test"}, text="private-body")

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        plan = PlanSession(tmp_path, client=http)
        monkeypatch.setattr(plan, "bearer", lambda _: "test-token")
        with pytest.raises(PlanError, match=r"HTTP 200，text/html，请求 ID req_test") as failure:
            plan.generate("test", {"model": "test", "input": []})
        assert failure.value.code == "VKS_CHATGPT_NETWORK" and "private-body" not in str(failure.value)


@pytest.mark.parametrize("finished", [True, False])
def test_chatgpt_without_content_type_requires_completion(tmp_path, monkeypatch, finished):
    frames = [("response.output_text.delta", {"delta": "synthetic"})]
    if finished:
        frames.append(("response.completed", {"response": completed("synthetic")}))
    sse = "".join(
        f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"
        for kind, fields in frames
    ).encode()

    def provider(_):
        # A binary response body leaves Content-Type unset in httpx.
        return httpx.Response(200, content=sse)

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        plan = PlanSession(tmp_path, client=http)
        monkeypatch.setattr(plan, "bearer", lambda _: "test-token")
        monkeypatch.setattr(plan, "profile", lambda: {"id": "test"})
        request = {"model": "test", "input": []}
        if finished:
            assert plan.generate("test", request) == completed("synthetic")
        else:
            with pytest.raises(PlanError, match="回复未完成"):
                plan.generate("test", request)


def library_fixture(path, *, eligible):
    payload = {
        "schema_version": 1, "project": {"id": "charges", "name": "合成知识库"},
        "snapshot": {"id": "saved", "name": "v1", "sha256": "a" * 64},
        "documents": [{"title": "收费手册", "sections": [
            {"id": "formula", "title": "收费标准公式", "content": "收费标准采用计费基数和单价。"},
            {"id": "other", "title": "收费标准内部说明", "content": "内部说明禁止发送。"},
        ]}],
        "bindings": [{"id": "source", "title": "收费设置", "summary": "示例关联", "section_ids": ["formula"]}],
        **({"chatgpt_plan": {"section_ids": ["formula"]}} if eligible else {}),
    }
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    path.write_text('<script id="vks-payload" type="application/octet-stream" data-encoding="gzip-base64">'
                    + data + "</script>", encoding="utf-8")


def test_reader_filters_outbound_evidence_and_keeps_old_files_offline(tmp_path):
    class Plan:
        answer = "计费依据见原文 [1]。"
        def status(self):
            return {"profile_id": "local", "connected": True}
        def models(self, _):
            return [{"id": "test"}]
        def generate(self, _, body):
            assert "内部说明" not in body["input"][0]["content"]
            evidence = json.loads(body["input"][0]["content"])["chapters"]
            assert [row["chapter"] for row in evidence] == ["收费标准公式"]
            return completed(self.answer)

    plan = Plan()
    for allowed in (True, False):
        export = tmp_path / f"bundle-{allowed}.html"
        library_fixture(export, eligible=allowed)
        store = LibraryStore(tmp_path / "library.json")
        library = store.select(export)
        reader = LibraryReader(store, library.ref, plan)
        assert reader.handle("search", {"question": "收费标准"})[0]["file_url"].startswith(export.as_uri())
        section = reader.handle("chapter", {"section_id": "formula"})
        assert section["binding_count"] == 1 and section["file_url"].endswith("#section=formula")
        question = {"question": "收费标准", "allow_external": True, "model": "test"}
        if allowed:
            with pytest.raises(PlanError, match="允许"):
                reader.handle("ask", {**question, "allow_external": False})
            assert reader.handle("ask", question)["sources"][0]["section_id"] == "formula"
            plan.answer = "计费依据见不存在的章节 [2]。"
            with pytest.raises(PlanError, match="引用"):
                reader.handle("ask", question)
        else:
            with pytest.raises(PlanError, match="重新导出"):
                reader.handle("ask", question)


def test_reader_uses_program_assets_and_rejects_other_origins(tmp_path):
    export = tmp_path / "library.html"
    library_fixture(export, eligible=False)
    store = LibraryStore(tmp_path / "library.json")
    library = store.select(export)
    reader = LibraryReader(store, library.ref, PlanSession(tmp_path))
    with ThreadingHTTPServer(("127.0.0.1", 0), ReaderHandler) as server:
        server.authority = f"127.0.0.1:{server.server_port}"
        server.route, server.reader, server.page = "/fixture", reader, reader_page()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://" + server.authority
        try:
            with httpx.Client(trust_env=False) as client:
                page = client.get(origin + "/fixture/")
                assert page.status_code == 200 and "VKS 成品库阅读器" in page.text
                assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
                query = {"question": "收费标准公式"}
                wrong = client.post(origin + "/fixture/search", json=query, headers={
                    "Origin": "http://example.test", "X-VKS-Reader": "1"})
                assert wrong.status_code == 403
                found = client.post(origin + "/fixture/search", json=query, headers={
                    "Origin": origin, "X-VKS-Reader": "1"})
                assert found.status_code == 200 and found.json()[0]["section_id"] == "formula"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.asyncio
async def test_library_analysis_mcp_uses_app_visibility_and_fixed_file(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    from vks_connect.library_mcp import ANALYZE, PLAN, create_library_server

    export, replacement = tmp_path / "eligible.html", tmp_path / "replacement.html"
    library_fixture(export, eligible=True)
    library_fixture(replacement, eligible=False)
    store = LibraryStore(tmp_path / "library.json")
    library = store.select(export)

    class Plan:
        change_file = False
        requests = []

        def status(self):
            return {"profile_id": "local-only", "connected": True, "account": "Test", "pending": False, "message": ""}

        def models(self, profile):
            assert profile == "local-only"
            return [{"id": "test-model", "name": "Test"}]

        def generate(self, profile, body):
            assert profile == "local-only"
            self.requests.append(body)
            if self.change_file:
                store.select(replacement)
            return completed("结论见 [1]。")

    plan = Plan()
    async with create_connected_server_and_client_session(create_library_server(store, "test", plan=plan)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools[ANALYZE].meta["ui"]["visibility"] == tools[PLAN].meta["ui"]["visibility"] == ["app"]
        scope = {"library_ref": library.ref}
        status = await client.call_tool(PLAN, {**scope, "action": "models"})
        assert status.structuredContent["data"]["models"] == [{"id": "test-model", "name": "Test"}]
        assert "profile_id" not in status.structuredContent["data"]
        args = {**scope, "question": "收费标准", "model": "test-model"}
        blocked = await client.call_tool(ANALYZE, {**args, "allow_external": False})
        assert blocked.isError and not plan.requests
        result = await client.call_tool(ANALYZE, {**args, "allow_external": True})
        assert not result.isError
        data = result.structuredContent["data"]
        assert data["sources"] == [{
            "section_id": "formula", "title": "收费标准公式", "document_title": "收费手册",
            "file_url": export.as_uri() + "#section=formula", "number": 1,
        }]
        assert "内部说明" not in plan.requests[-1]["input"][0]["content"]
        assert "计费基数" not in str(data)
        plan.change_file = True
        swapped = await client.call_tool(ANALYZE, {**args, "allow_external": True})
        assert swapped.isError and swapped.structuredContent["error"]["code"] == "VKS_LIBRARY_CHANGED"
