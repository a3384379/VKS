"""Synthetic end-to-end retrieval over saved, permission-filtered revisions."""

import json
import socket
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic
from urllib.parse import urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.types import LATEST_PROTOCOL_VERSION
from sqlalchemy import func, select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy
from vks_api.knowledge import KnowledgeService
from vks_api.knowledge_search import SearchControl
from vks_api.mcp_gateway import REQUIREMENTS_READONLY, APIBridge, create_http_app
from vks_api.models import Artifact, Job, Revision, uid
from vks_api.oauth import OAuthGrant, OAuthIssuer, OAuthPrincipal
from vks_api.service import DomainError
from vks_api.vector_rag import RagSettings

from tests.interactive import test_knowledge
from tests.mcp.test_requirements_readonly import connect
from tests.ui.test_frontend_browser import live_api

workspace = test_knowledge.workspace
REAL_REQUEST_MODEL = ai_gateway.request_model


@pytest.fixture
def corpus(workspace):
    app, api, service, owner, pid, aid, _, _ = workspace
    current = service.workspace(owner, pid, aid)
    document = current["documents"][0]
    content = (
        "# 调整手册\n\n"
        "## 服务条款\n\n"
        + "登记过程先保存单据。\n" * 350
        + "偏远区域批量减免必须逐笔确认。\n\n"
        "| 条件 | 金额 |\n| --- | --- |\n| 海岛例外 | $F = P \\times R$ |\n\n"
        "## 通用条款\n\n服务申请需要核验账号。\n\n"
        "## 通用条款\n\n服务申请允许线下记录。\n\n"
        "## 另一种条款\n\n服务申请保留记录。\n\n"
        "## 其他条款\n\n服务申请使用原价。\n"
    )
    service.save_document(
        owner, document["entity_id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": document["entity_revision_id"],
            "markdown": content,
        },
        service.get_document(owner, document["entity_id"], document["entity_revision_id"])["row_version"],
    )
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, uid())
    token = service.create_token(owner, pid, {"name": "Read only", "scopes": ["knowledge:read"]})
    return app, api, service, owner, pid, aid, release, token


def search(api, pid, release, token, query, **options):
    return api.post(
        f"/api/v1/projects/{pid}/search",
        headers={"Authorization": "Bearer " + token["token"]},
        json={"release_id": release["id"], "query": query, **options},
    )


def test_keyword_and_shared_studio_returns_real_end_of_section_and_locator(corpus, monkeypatch):
    _, api, service, _, pid, _, release, token = corpus
    service.studio_base_url = "http://127.0.0.1:5193/"
    monkeypatch.setattr(ai_gateway, "request_model", lambda *_: pytest.fail("keyword search called a model"))
    legacy = search(api, pid, release, token, "偏远区域批量减免")
    assert legacy.status_code == 200 and "retrieval" not in legacy.json()
    assert all("逐笔确认" not in hit["excerpt"] for hit in legacy.json()["data"])
    response = search(
        api, pid, release, token, "如果偏远区域发生批量减免的情形，哪些操作需要逐笔确认？",
        retrieval_mode="keyword", max_excerpt_chars=140,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scope"]["manifest_sha256"] == release["manifest_sha256"]
    assert data["retrieval"] == {"requested_mode": "keyword", "used_mode": "keyword", "truncated": True}
    hit = data["data"][0]
    assert hit["entity"]["name"] == "服务条款" and hit["source_kind"] == "original_section"
    assert "偏远区域批量减免必须逐笔确认。" in hit["excerpt"]
    assert hit["excerpt_truncated"] and hit["locator"]["section_revision_id"] == hit["entity"]["entity_revision_id"]
    assert hit["locator"]["document_revision_id"] == hit["document_revision_id"]
    assert hit["locator"]["release_id"] == release["id"]
    assert "section=" + hit["locator"]["section_revision_id"] in hit["studio_url"]
    documents = search(
        api, pid, release, token, "批量减免逐笔确认", retrieval_mode="keyword", kinds=["document"],
    ).json()["data"]
    assert len(documents) == 1 and documents[0]["entity"]["type"] == "document"
    assert documents[0]["locator"] == hit["locator"] and "逐笔确认" in documents[0]["excerpt"]
    studio = api.post(
        f"/api/v3/projects/{pid}/document-presentation/search",
        headers={"Authorization": "Bearer " + token["token"]},
        json={"release_id": release["id"], "query": "偏远区域 批量减免 逐笔确认"},
    )
    assert studio.status_code == 200, studio.text
    assert studio.json()["data"][0]["section"] == hit["locator"]["section_revision_id"]
    formula = search(api, pid, release, token, "海岛例外", retrieval_mode="keyword").json()["data"][0]
    assert "| 海岛例外 | $F = P \\times R$ |" in formula["excerpt"]
    titles = search(api, pid, release, token, "通用条款", retrieval_mode="keyword", kinds=["section"]).json()["data"]
    matching = [row for row in titles if row["match_kind"] == "exact"]
    assert len(matching) == 2 and len({row["locator"]["section_revision_id"] for row in matching}) == 2
    assert len({row["locator"]["document_revision_id"] for row in matching}) == 1


def test_keyword_excerpt_finds_the_dense_rule_after_earlier_isolated_terms(workspace):
    _, api, service, owner, pid, aid, _, _ = workspace
    original = service.workspace(owner, pid, aid)["documents"][0]
    entity_id, revision_id = original["entity_id"], original["entity_revision_id"]
    padding = "\n\n" + "Unrelated text. " * 35 + "\n\n"
    rule = "Rule: apply refund only when the adjustment exceeds the threshold."
    content = (
        "# Payments\n\nRefund introduction" + padding + "Adjustment glossary" + padding + "Threshold definition"
        + padding + rule
    )
    service.save_document(
        owner,
        entity_id,
        {"analysis_set_id": aid, "base_revision_id": revision_id, "markdown": content},
        service.get_document(owner, entity_id, revision_id)["row_version"],
    )
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, uid())
    reader = service.create_token(owner, pid, {"name": "excerpt reader", "scopes": ["knowledge:read"]})
    response = search(
        api, pid, release, reader, "refund adjustment threshold", retrieval_mode="keyword",
        kinds=["section"], max_excerpt_chars=100,
    )
    assert response.status_code == 200, response.text
    hit = response.json()["data"][0]
    assert rule in hit["excerpt"]
    assert hit["excerpt"] in content and hit["excerpt_truncated"]


def test_keyword_caps_budgets_and_filters_before_ranking(corpus):
    _, api, service, owner, pid, aid, release, token = corpus
    draft_id = KnowledgeService(service).clone(owner, pid, aid, "Draft Search", uid())["id"]
    options = {"retrieval_mode": "keyword", "kinds": ["section"], "max_excerpt_chars": 10, "max_total_chars": 17}
    result = search(api, pid, release, token, "服务申请", **options).json()
    assert len(result["data"]) == 3
    assert sum(len(hit["excerpt"]) for hit in result["data"]) <= 17
    assert result["retrieval"]["truncated"]
    assert all(len(hit["excerpt"]) <= 10 and hit["excerpt_truncated"] for hit in result["data"])
    restricted = result["data"][0]["document_revision_id"]
    with service.db() as db:
        row = db.get(Revision, restricted)
        db.get(Artifact, row.artifact_id).sensitivity = "confidential"
    result = search(api, pid, release, token, "服务申请", **options).json()
    assert result["data"] == []
    assert not result["retrieval"]["truncated"]
    assert search(api, pid, release, token, "a" * 1001, retrieval_mode="keyword").status_code == 422
    assert search(api, pid, release, token, "服务申请", retrieval_mode="hybrid", limit=21).status_code == 422
    assert search(api, pid, release, token, "a" * 1001, limit=21).status_code == 200
    draft = api.post(
        f"/api/v1/projects/{pid}/search",
        headers={"Authorization": "Bearer " + token["token"]},
        json={"analysis_set_id": draft_id, "query": "服务申请", "retrieval_mode": "keyword"},
    )
    assert draft.status_code == 403


def test_saved_search_keeps_older_section_and_explicit_embedding_downgrade(corpus):
    _, api, service, owner, pid, aid, release, token = corpus
    aid = KnowledgeService(service).clone(owner, pid, aid, "Draft Revision", uid())["id"]
    document = service.workspace(owner, pid, aid)["documents"][0]
    revision = service.get_document(owner, document["entity_id"], document["entity_revision_id"])
    service.save_document(
        owner, document["entity_id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": document["entity_revision_id"],
            "markdown": revision["markdown"].replace("逐笔确认", "统一核销"),
        },
        revision["row_version"],
    )
    latest = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, uid())
    options = {"retrieval_mode": "auto", "kinds": ["section"]}
    old = search(api, pid, release, token, "偏远区域批量减免", **options).json()
    new = search(api, pid, latest, token, "偏远区域批量减免", **options).json()
    assert old["retrieval"]["degraded_reason"] == new["retrieval"]["degraded_reason"] == "embedding_disabled"
    assert "逐笔确认" in old["data"][0]["excerpt"] and "统一核销" in new["data"][0]["excerpt"]
    assert old["data"][0]["locator"]["section_revision_id"] != new["data"][0]["locator"]["section_revision_id"]


@pytest.fixture
def indexed(corpus, monkeypatch):
    _, _, service, owner, pid, _, _, _ = corpus
    calls = []

    def embed(config, body):
        assert config["protocol"] == "embeddings"
        calls.append(body["input"])
        assert body["model"].startswith("text-embedding")
        return {
            "usage": {"total_tokens": len(body["input"])},
            "data": [
                {
                    "index": index,
                    "embedding": ([1, 0] if "偏远区域" in value or "waiver" in value else [0, 1])
                    + [0] * (body["dimensions"] - 2),
                }
                for index, value in enumerate(body["input"])
            ],
        }

    monkeypatch.setattr(ai_gateway, "request_model", embed)
    with service.db() as db:
        policy = ModelPolicy(
            id=uid(),
            project_id=pid,
            settings={
                "enabled": True,
                "allow_external": True,
                "endpoint": "https://synthetic-provider.invalid/v1",
                "model_id": "unused-answer-model",
            },
        )
        db.add(policy)
    rag = service._search_assistant.rag
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    return (*corpus, rag, calls)


def _query_count(rag):
    with rag.store.db() as db:
        return db.execute("SELECT COUNT(*) FROM queries").fetchone()[0]


def test_vector_lookup_uses_only_existing_exact_index_and_query_cache(indexed):
    _, api, service, owner, pid, _, release, token, rag, calls = indexed
    response = search(api, pid, release, token, "bulk waiver", retrieval_mode="hybrid")
    assert response.json()["retrieval"]["degraded_reason"] == "index_unavailable"
    assert not calls and _query_count(rag) == 0

    export = rag.export_index(owner, pid, release_id=release["id"])
    assert export["coverage"]["complete"]
    calls.clear()
    with service.db() as db:
        jobs_before = db.scalar(select(func.count(Job.id)))
        revisions_before = db.scalar(select(func.count(Revision.id)))
    keyword = search(api, pid, release, token, "bulk waiver", retrieval_mode="keyword").json()
    assert keyword["data"] == []
    response = search(api, pid, release, token, "bulk waiver", retrieval_mode="auto")
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == [["bulk waiver"]]
    assert result["retrieval"]["used_mode"] == "hybrid"
    semantic = result["data"][0]
    assert semantic["match_kind"] == "semantic" and "偏远区域批量减免" in semantic["excerpt"]
    assert _query_count(rag) == 1
    assert search(api, pid, release, token, "bulk waiver", retrieval_mode="auto").json()["data"] == result["data"]
    assert calls == [["bulk waiver"]]
    with service.db() as db:
        assert db.scalar(select(func.count(Job.id))) == jobs_before
        assert db.scalar(select(func.count(Revision.id))) == revisions_before

    # Same text in a different model/dimensional space cannot reuse query or
    # chapter vectors from the previous model.
    rag.save(owner, pid, RagSettings(enabled=True, model_id="text-embedding-other", dimensions=128))
    changed = search(api, pid, release, token, "bulk waiver", retrieval_mode="auto").json()
    assert changed["retrieval"]["degraded_reason"] == "index_unavailable"
    assert changed["data"] == [] and calls == [["bulk waiver"]]


def test_partial_invalid_index_and_provider_errors_degrade_safely(indexed, monkeypatch):
    _, api, _, owner, pid, _, release, token, rag, calls = indexed
    rag.export_index(owner, pid, release_id=release["id"])
    calls.clear()
    with rag.store.db() as db:
        db.execute("DELETE FROM chunks WHERE id IN (SELECT id FROM chunks WHERE project=? LIMIT 1)", (pid,))
    partial = search(api, pid, release, token, "服务申请", retrieval_mode="auto").json()
    assert partial["retrieval"]["degraded_reason"] == "index_partial"
    assert partial["retrieval"]["used_mode"] == "hybrid"

    def limited(_config, _body):
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "private provider details", 503, {"upstream_status": 429})

    monkeypatch.setattr(ai_gateway, "request_model", limited)
    response = search(api, pid, release, token, "批量减免", retrieval_mode="hybrid")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["retrieval"]["degraded_reason"] == "provider_unavailable" and data["data"]
    assert data["retrieval"]["used_mode"] == "keyword" and "private provider" not in response.text

    with rag.store.db() as db:
        db.execute("UPDATE chunks SET content='unverified search text' WHERE project=?", (pid,))
    response = search(api, pid, release, token, "批量减免", retrieval_mode="auto").json()
    assert response["retrieval"]["degraded_reason"] == "index_revision_mismatch"
    assert "unverified search text" not in json.dumps(response)


@pytest.mark.parametrize("revoke", ["credential", "visibility"])
def test_revocation_during_external_search_fails_closed(indexed, monkeypatch, revoke):
    _, api, service, owner, pid, _, release, token, rag, _ = indexed
    rag.export_index(owner, pid, release_id=release["id"])
    provider = ai_gateway.request_model
    start = _query_count(rag)

    def reclassify(config, body):
        if revoke == "credential":
            service.revoke_token(owner, pid, token["id"])
        else:
            with service.db() as db:
                artifacts = db.scalars(select(Artifact).where(Artifact.project_id == pid))
                for artifact in artifacts:
                    artifact.sensitivity = "confidential"
        return provider(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", reclassify)
    response = search(api, pid, release, token, "bulk waiver", retrieval_mode="hybrid")
    assert response.status_code in (401, 412), response.text
    assert response.json()["error"]["code"] == (
        "VKS_UNAUTHENTICATED" if revoke == "credential" else "VKS_VERSION_CONFLICT"
    )
    assert "data" not in response.json() and _query_count(rag) == start


@pytest.mark.parametrize("invalidate", ["expiry", "key_rotation"])
def test_oauth_credential_revalidated_after_external_search(indexed, monkeypatch, invalidate):
    _, api, service, owner, pid, _, release, _, rag, calls = indexed
    rag.export_index(owner, pid, release_id=release["id"])
    calls.clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="search-test", alg="RS256", use="sig")
    issuer_id = uid()
    principal_id = uid()
    issuer_url = "https://search-test.invalid"
    audience = "https://vks-test.invalid/mcp"
    with service.db() as db:
        db.add(OAuthIssuer(id=issuer_id, issuer=issuer_url, audience=audience, jwks={"keys": [public]}))
        db.flush()
        db.add(OAuthPrincipal(id=principal_id, issuer_id=issuer_id, subject="reader", client_id="search"))
        db.flush()
        db.add(
            OAuthGrant(
                id=uid(), principal_id=principal_id, project_id=pid, created_by=owner.id, scopes=["knowledge:read"]
            )
        )
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=1)
    token = jwt.encode(
        {
            "iss": issuer_url,
            "aud": audience,
            "sub": "reader",
            "client_id": "search",
            "scope": "knowledge:read",
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
        key,
        algorithm="RS256",
        headers={"kid": "search-test"},
    )
    provider = ai_gateway.request_model

    class ExpiredJWTClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (expires_at + timedelta(seconds=1)).astimezone(tz)

    def invalidate_credential(config, body):
        if invalidate == "expiry":
            monkeypatch.setattr(jwt.api_jwt, "datetime", ExpiredJWTClock)
        else:
            replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            replacement_public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(replacement.public_key()))
            with service.db() as db:
                db.get(OAuthIssuer, issuer_id).jwks = {"keys": [{**replacement_public, "kid": "replacement"}]}
        return provider(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", invalidate_credential)
    start = _query_count(rag)
    response = search(api, pid, release, {"token": token}, "bulk waiver", retrieval_mode="hybrid")
    assert calls == [["bulk waiver"]]
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "VKS_UNAUTHENTICATED"
    assert "data" not in response.json() and _query_count(rag) == start


def test_cancellation_reaches_worker_during_embedding(indexed, monkeypatch):
    _, _, service, owner, pid, _, release, _, rag, _ = indexed
    rag.export_index(owner, pid, release_id=release["id"])
    control, started = SearchControl(), Event()
    result = {}

    def mock(config, _body):
        started.set()
        assert control.cancelled.wait(3)
        config["_search_call"].check()
        pytest.fail("cancelled lookup reached the provider result")

    def work():
        try:
            service.search(
                owner, pid, {"release_id": release["id"], "query": "bulk waiver", "retrieval_mode": "hybrid"},
                control=control,
            )
        except DomainError as exc:
            result["code"] = exc.code

    monkeypatch.setattr(ai_gateway, "request_model", mock)
    worker = Thread(target=work)
    worker.start()
    try:
        assert started.wait(3)
        before = monotonic()
        control.cancel()
        worker.join(timeout=3)
    finally:
        control.cancel()
        worker.join(timeout=3)
    assert not worker.is_alive() and monotonic() - before < 3
    assert result["code"] == "VKS_CANCELLED" and _query_count(rag) == 0


@pytest.mark.parametrize("stop", ["timeout", "disconnect", "mcp_disconnect"])
def test_live_embedding_transport_closes_on_timeout_or_disconnect(indexed, monkeypatch, stop):
    app, api, service, owner, pid, _, release, token, rag, _ = indexed
    connected, closed = Event(), Event()

    class Observer(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            connected.set()
            self.connection.settimeout(8)
            if self.rfile.read(1) == b"":
                closed.set()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Observer) as provider:
        thread = Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{provider.server_port}"
            monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", origin)
            monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
            with service.db() as db:
                row = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                row.settings = {**row.settings, "endpoint": origin + "/v1"}
            rag.export_index(owner, pid, release_id=release["id"])
            monkeypatch.setattr(ai_gateway, "request_model", REAL_REQUEST_MODEL)
            if stop == "timeout":
                monkeypatch.setattr("vks_api.knowledge_search.EMBEDDING_SECONDS", 0.75)
                started = monotonic()
                result = search(api, pid, release, token, "偏远区域", retrieval_mode="hybrid").json()
                assert monotonic() - started < 3
                assert connected.is_set()
                assert result["retrieval"]["used_mode"] == "keyword" and result["data"]
                assert result["retrieval"]["degraded_reason"] == "provider_unavailable"
            else:
                search_args = {
                    "release_id": release["id"], "query": "偏远区域", "retrieval_mode": "hybrid",
                }
                with ExitStack() as stack:
                    url = stack.enter_context(live_api(app))
                    path = f"/api/v1/projects/{pid}/search"
                    extra_headers = ""
                    body = json.dumps(search_args).encode()
                    if stop == "mcp_disconnect":
                        gateway = create_http_app(APIBridge(url), REQUIREMENTS_READONLY)
                        url = stack.enter_context(live_api(gateway))
                        path = "/mcp"
                        extra_headers = (
                            "Accept: application/json, text/event-stream\r\n"
                            f"Mcp-Protocol-Version: {LATEST_PROTOCOL_VERSION}\r\n"
                        )
                        body = json.dumps({
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "search_knowledge", "arguments": {"project_id": pid, **search_args}},
                        }).encode()
                    parsed = urlsplit(url)
                    with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as client:
                        headers = (
                            f"POST {path} HTTP/1.1\r\n"
                            f"Host: {parsed.netloc}\r\n"
                            f"Authorization: Bearer {token['token']}\r\n"
                            f"{extra_headers}"
                            f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n"
                        )
                        client.sendall(headers.encode() + body)
                        assert connected.wait(3)
                    assert closed.wait(2)
            assert closed.wait(2) and _query_count(rag) == 0
        finally:
            provider.shutdown()
            thread.join(timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_sdk_reads_enhanced_search_without_edit_or_export_scope(corpus, transport):
    app, _, _, _, pid, _, release, token = corpus
    with live_api(app) as url:
        async with connect(transport, url, token["token"]) as sdk:
            await sdk.initialize()
            result = await sdk.call_tool(
                "search_knowledge",
                {
                    "project_id": pid,
                    "release_id": release["id"],
                    "query": "偏远区域批量减免逐笔确认",
                    "retrieval_mode": "auto",
                    "kinds": ["section"],
                },
            )
            assert not result.isError, result
            public = result.structuredContent
            assert public["scope"]["release_id"] == release["id"]
            assert public["retrieval"]["degraded_reason"] == "embedding_disabled"
            hit = public["data"][0]
            assert hit["locator"]["document_revision_id"] == hit["document_revision_id"]
            assert "逐笔确认" in hit["excerpt"]
