"""The local connector consumes VKS data only and keeps links snapshot-bound."""

import base64
import gzip
import json
import struct
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from playwright.async_api import async_playwright
from vks_api import mcp_gateway  # noqa: F401  # Loads the source connector package.
from vks_connect import library, library_mcp
from vks_connect.library_embeddings import EmbeddingSettings, LibraryEmbeddings
from vks_connect.local_embeddings import DIMENSIONS, MODEL, PROFILE, LocalModelError

from tests.ui.test_frontend_browser import chromium_path


def write_library(path, version, *, duplicate=False, vectors=False, local=False):
    payload = {
        "schema_version": 1,
        "project": {"id": "charges", "name": "云收费"},
        "snapshot": {"id": f"release-{version}", "name": version, "sha256": "c" * 64},
        "documents": [
            {
                "title": "收费手册",
                "sections": [
                    {"id": "charge-formula", "title": "收费标准公式", "content": "**计费金额由计费基数与单价确定。**"},
                    {"id": "monthly-card", "title": "首次开通月卡", "content": "先核对车辆、套餐和收款信息。"},
                ],
                "images": {"figure.png": "data:image/png;base64,cGljdHVyZQ=="},
            },
        ],
        "bindings": [
            {
                "id": "payment-panel",
                "title": "收费控件",
                "summary": "**已保存说明。**",
                "section_ids": ["charge-formula", "monthly-card"],
            }
        ],
    }
    if vectors:
        payload["rag"] = {
            "model": "embedding-fixture",
            "dimensions": 2,
            "encoding": "float32-le-base64",
            "chunks": [
                {"section_id": sid, "vector": base64.b64encode(struct.pack("<2f", *values)).decode()}
                for sid, values in [
                    ("monthly-card", (1, 0)),
                    ("charge-formula", (0, 1)),
                    ("missing-section", (1, 0)),
                    ("charge-formula", (float("nan"), 0)),
                ]
            ],
        }
    if local:
        vector = struct.pack(f"<{DIMENSIONS}f", 1, *([0] * (DIMENSIONS - 1)))
        payload["local_rag"] = {
            "profile": PROFILE, "model": MODEL, "dimensions": DIMENSIONS, "encoding": "float32-le-base64",
            "chunks": [{"section_id": "monthly-card", "vector": base64.b64encode(vector).decode()}],
        }
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    envelope = f'<script id="vks-payload" type="application/octet-stream" data-encoding="gzip-base64">{data}</script>'
    path.write_text(
        "<!doctype html><script>throw Error('connector must never execute HTML')</script>"
        + envelope * (2 if duplicate else 1),
        encoding="utf-8",
    )


def test_library_selection_reads_saved_sections_and_associations(tmp_path):
    store = library.LibraryStore(tmp_path / "local" / "default.library.json")
    path = tmp_path / "云 收费#成品库.html"
    write_library(path, "v19")
    source = store.select(path)
    snapshot = source.context()
    assert snapshot["version"] == "v19" and snapshot["binding_count"] == 1
    assert store.current(snapshot["library_ref"]) is source
    assert library.LibraryStore(store.preference_path).current().context() == snapshot
    result = source.search("收费标准公式 单价", 1)[0]
    assert result["section_id"] == "charge-formula" and result["binding_count"] == 1
    assert "%23" in result["file_url"] and result["file_url"].endswith("#section=charge-formula")
    assert source.read("charge-formula", 0, 100)["content"] == "**计费金额由计费基数与单价确定。**"
    association = source.associations("charge-formula", 0, 8)["bindings"][0]
    assert association["summary"] == "**已保存说明。**"
    assert [section["section_id"] for section in association["sections"]] == ["charge-formula", "monthly-card"]
    assert source.image("charge-formula", "figure.png") == ("image/png", "cGljdHVyZQ==")

    invalid = tmp_path / "invalid.html"
    write_library(invalid, "v20", duplicate=True)
    with pytest.raises(library.LibraryError):
        store.select(invalid)
    assert store.current() is source

    write_library(path, "v20")
    with pytest.raises(library.LibraryError, match="成品库已切换"):
        store.current(snapshot["library_ref"])
    assert store.current().context()["version"] == "v20"
    assert store.current().ref != source.ref


def test_library_rejects_out_of_budget_and_missing_targets(tmp_path, monkeypatch):
    path = tmp_path / "export.html"
    write_library(path, "v19")
    source, _ = library.read_library(path)
    with pytest.raises(library.LibraryError):
        source.url("https://untrusted.example")
    with pytest.raises(library.LibraryError):
        source.image("charge-formula", "https://untrusted.example/tracker.png")
    monkeypatch.setattr(library, "MAX_DATA_BYTES", 100)
    with pytest.raises(library.LibraryError):
        library.read_library(path)


@pytest.mark.asyncio
async def test_default_library_search_never_uses_cloud(tmp_path, monkeypatch):
    class Remote:
        def __init__(self):
            self.calls = []

        async def query(self, index, question):
            self.calls.append(question)
            return [0, 1], None

    def infer(question):
        assert question == "如何平均分配公共用电"
        return [1] + [0] * (DIMENSIONS - 1)

    monkeypatch.setattr(library_mcp, "local_query", infer)
    path = tmp_path / "export.html"
    store = library.LibraryStore(tmp_path / "default.library.json")
    write_library(path, "v19", vectors=True, local=True)
    remote = Remote()
    server = library_mcp.create_library_server(store, "test", remote)
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        args = {"library_ref": store.select(path).ref, "query": "如何平均分配公共用电"}
        result = (await client.call_tool("search_vks_library", args)).structuredContent
        assert result["retrieval"] == {"requested": "auto", "used": "hybrid", "fallback": None, "vector_source": "local"}
        assert result["data"][0]["section_id"] == "monthly-card"
        assert not remote.calls

        def broken_model(_):
            raise LocalModelError("missing model")

        monkeypatch.setattr(library_mcp, "local_query", broken_model)
        args["query"] = "收费标准公式 单价"
        result = (await client.call_tool("search_vks_library", args)).structuredContent
        assert result["retrieval"]["fallback"] == "local_model_unavailable"
        assert result["data"][0]["section_id"] == "charge-formula" and not remote.calls

        write_library(path, "v19", vectors=True)
        args["library_ref"] = store.select(path).ref
        result = (await client.call_tool("search_vks_library", args)).structuredContent
        assert result["retrieval"]["fallback"] == "no_local_index" and not remote.calls
        result = (await client.call_tool("search_vks_library", {**args, "retrieval_mode": "hybrid"})).structuredContent
        assert result["retrieval"]["vector_source"] == "cloud" and len(remote.calls) == 1


@pytest.mark.asyncio
async def test_embeddings_use_stored_vectors_and_send_only_the_query(tmp_path, monkeypatch):
    path = tmp_path / "export.html"
    write_library(path, "v19", vectors=True)
    source, _ = library.read_library(path)
    assert source.context()["vector_index"]["indexed_chunks"] == 2
    queries = []

    def respond(request):
        assert str(request.url) == "http://127.0.0.1:5151/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fixture-only"
        body = json.loads(request.content)
        assert set(body) == {"model", "dimensions", "input"}
        queries.append(body["input"])
        return httpx.Response(200, json={"model": body["model"], "data": [{"embedding": [1, 0]}]})

    service = LibraryEmbeddings(tmp_path / "embedding.json", httpx.MockTransport(respond))
    monkeypatch.setenv("VKS_CONNECT_EMBEDDING_KEY", "fixture-only")
    question = "如何平均分配公共用电"
    assert await service.query(source.vectors, question) == (None, "embedding_not_configured")
    service.configure(EmbeddingSettings("http://127.0.0.1:5151/v1", "wrong-model", 2))
    assert await service.query(source.vectors, question) == (None, "embedding_model_mismatch")
    assert queries == []
    service.configure(EmbeddingSettings("http://127.0.0.1:5151/v1", "embedding-fixture", 2))
    vector, reason = await service.query(source.vectors, question)
    assert reason is None and vector == [1, 0] and queries == [question]
    assert "fixture-only" not in service.path.read_text()
    assert source.search(question, 1) == []
    row = source.search(question, 1, vector)[0]
    assert row["section_id"] == "monthly-card" and row["match"] == "semantic"
    assert row["file_url"].endswith("#section=monthly-card") and row["binding_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,reason",
    [
        (httpx.Response(302, headers={"Location": "https://example.invalid"}), "embedding_unavailable"),
        (
            httpx.Response(200, json={"model": "embedding-fixture", "data": [{"embedding": [1]}]}),
            "embedding_response_invalid",
        ),
        (
            httpx.Response(200, json={"model": "imposter", "data": [{"embedding": [1, 0]}]}),
            "embedding_response_invalid",
        ),
    ],
)
async def test_unusable_embedding_response_returns_reason(tmp_path, monkeypatch, reply, reason):
    path = tmp_path / "export.html"
    write_library(path, "v19", vectors=True)
    source, _ = library.read_library(path)
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return reply

    monkeypatch.setenv("VKS_CONNECT_EMBEDDING_KEY", "fixture-only")
    service = LibraryEmbeddings(tmp_path / "embedding.json", httpx.MockTransport(respond))
    service.configure(EmbeddingSettings("http://127.0.0.1:5151/v1", "embedding-fixture", 2))
    assert await service.query(source.vectors, "收费标准公式 单价") == (None, reason)
    assert calls == ["127.0.0.1"]
    assert source.search("收费标准公式 单价", 1)[0]["section_id"] == "charge-formula"


@pytest.mark.asyncio
async def test_library_system_chooser_uses_native_arguments(monkeypatch):
    async def pick(command, check):
        assert command[:2] == ["/usr/bin/osascript", "-e"] and check is False
        return type("Picker", (), {"stdout": "/tmp/云收费.html\n".encode(), "stderr": b"", "returncode": 0})

    monkeypatch.setattr(library_mcp.sys, "platform", "darwin")
    monkeypatch.setattr(library_mcp.anyio, "run_process", pick)
    assert await library_mcp.choose_library_file() == "/tmp/云收费.html"


@pytest.mark.asyncio
async def test_chapter_open_preserves_exact_fragment_across_native_file_handoff(tmp_path, monkeypatch):
    source = tmp_path / "项目 A # 成品库.html"
    write_library(source, "v24")
    store = library.LibraryStore(tmp_path / "current.library.json")
    book = store.select(source)
    launched = []

    async def launch(url):
        if library_mcp.sys.platform == "win32":
            assert Path(url).is_file()
        else:
            assert not urlsplit(url).fragment
        launched.append(url)

    monkeypatch.setattr(library_mcp, "open_browser_url", launch)
    server = library_mcp.create_library_server(store, "test")
    context = {"library_ref": book.ref}
    async with create_connected_server_and_client_session(server) as client:
        missing = await client.call_tool(library_mcp.OPEN, {**context, "section_id": "missing"})
        assert missing.isError and not launched
        home = await client.call_tool(library_mcp.OPEN, context)
        assert not home.isError
        assert launched.pop() == (str(source.resolve()) if library_mcp.sys.platform == "win32" else book.url())
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_path(playwright))
            page = await browser.new_page()
            try:
                for section in ("charge-formula", "monthly-card"):
                    opened = await client.call_tool(library_mcp.OPEN, {**context, "section_id": section})
                    assert not opened.isError
                    launcher = launched.pop()
                    if library_mcp.sys.platform == "win32":
                        launcher = Path(launcher).as_uri()
                    await page.goto(launcher)
                    await page.wait_for_url(book.url(section), wait_until="domcontentloaded")
            finally:
                await browser.close()
