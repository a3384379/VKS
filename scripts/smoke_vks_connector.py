"""Exercise a relocated, installed connector with the official MCP SDK."""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import os
import struct
import subprocess
import tomllib
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN = "synthetic-vks-packaging-token"
INSTANCE = "b" * 64


class API(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/projects?"):
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                self.send_error(401)
                return
            data = {"data": [], "meta": {"request_id": str(uuid4()), "next_cursor": None}}
        elif self.path == "/api/v1/ready":
            data = {"status": "ready", "instance_id": INSTANCE}
        elif self.path == "/health":
            data = {"service": "vks-runtime"}
        elif self.path == "/":
            raw = "<title>知境 Studio</title>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(raw)
            return
        else:
            self.send_error(404)
            return
        raw = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


async def smoke_library(command, cwd, env, settings):
    env = {k: v for k, v in env.items() if k != "VKS_CONNECT_TOKEN"}
    env["VKS_CONNECT_CONFIG_DIR"] += "-library"
    library_file = cwd / "只读 成品库.html"
    payload = {
        "schema_version": 1,
        "project": {"id": "local-project", "name": "收费"},
        "snapshot": {"id": "saved", "name": "v7", "sha256": INSTANCE},
        "documents": [
            {"title": "收费标准公式", "sections": [{"id": "section-7", "title": "收费标准", "content": "核对计费基数与单价"}]}
        ],
        "bindings": [{"id": "binding-1", "title": "缴费关联", "summary": "原关联", "section_ids": ["section-7"]}],
    }
    bundle = command.parent.parent
    build_info = bundle / "build-info.json"
    expect_local = build_info.is_file() and "local_embedding_profile" in json.loads(build_info.read_text(encoding="utf-8"))
    if expect_local:
        from vks_connect.local_embeddings import DIMENSIONS, MODEL, PROFILE

        assert (bundle / "models" / PROFILE / "model.onnx").is_file()
        vector = struct.pack(f"<{DIMENSIONS}f", 1.0, *([0.0] * (DIMENSIONS - 1)))
        payload["local_rag"] = {
            "profile": PROFILE, "model": MODEL, "dimensions": DIMENSIONS, "encoding": "float32-le-base64",
            "chunks": [{"section_id": "section-7", "vector": base64.b64encode(vector).decode()}],
        }
    encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    library_file.write_text(
        '<script id="vks-payload" type="application/octet-stream" data-encoding="gzip-base64">' + encoded + "</script>",
        encoding="utf-8",
    )
    params = StdioServerParameters(command=str(command), args=settings, cwd=cwd, env=env)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client,
    ):
        await client.initialize()
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        panel = tools["open_vks_workspace"]
        assert panel.meta["openai/ui"]["entrypoints"] == [{"type": "global"}]
        assert tools["choose_vks_library"].meta["ui"]["visibility"] == ["app"]
        assert tools["analyze_vks_library"].meta["ui"]["visibility"] == ["app"]
        assert tools["vks_library_chatgpt_plan"].meta["ui"]["visibility"] == ["app"]
        assert tools["search_vks_library"].meta["ui"]["visibility"] == ["app", "model"]
        assert "search_knowledge" not in tools and "open_vks_studio" not in tools
        uri = panel.meta["ui"]["resourceUri"]
        resource = (await client.read_resource(uri)).contents[0]
        assert resource.mimeType == "text/html;profile=mcp-app" and "window.VksReaderMarkdown={" in resource.text
        blank = await client.call_tool(panel.name, {})
        assert not blank.isError and "message" in blank.meta["vks/library"]
        selected = await asyncio.to_thread(
            subprocess.check_output, [str(command), "library", "use", str(library_file)], cwd=cwd, env=env
        )
        context = json.loads(selected)
        picked = await client.call_tool(panel.name, {})
        assert picked.meta["vks/library"]["library"] == context and context["version"] == "v7"
        scope = {"library_ref": context["library_ref"]}
        search = await client.call_tool("search_vks_library", {**scope, "query": "收费标准公式"})
        assert search.structuredContent["data"][0]["file_url"] == library_file.resolve().as_uri() + "#section=section-7"
        if expect_local:
            assert search.structuredContent["retrieval"]["vector_source"] == "local"
        section = {**scope, "section_id": "section-7"}
        readback = await client.call_tool("read_vks_library_section", section)
        assert readback.structuredContent["data"]["content"] == "核对计费基数与单价"
        associations = await client.call_tool("read_vks_library_associations", section)
        assert associations.structuredContent["data"]["bindings"][0]["summary"] == "原关联"
        bad = await client.call_tool("open_vks_library", {**scope, "url": "https://untrusted.example"})
        assert bad.isError


async def smoke(command: Path, cwd: Path, url: str, server_settings):
    # Keep a 200-character config directory and run the connector with no
    # repository Python path, GUI shell setup or user's Codex configuration.
    base = cwd / "连接测试"
    profile = base / ("x" * max(8, 200 - len(str(base))))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VKS_", "PYTHONPATH", "PYTHONHOME"))}
    env.update(
        {
            "PATH": "",
            "PYTHONUTF8": "0",
            "VKS_CONNECT_CONFIG_DIR": str(profile),
            "VKS_CONNECT_TOKEN": TOKEN,
        }
    )

    def run(*args):
        return subprocess.check_output([str(command), *args], env=env, cwd=cwd, text=True, encoding="utf-8").strip()

    run("configure", "--api-url", url, "--studio-url", url, "--runtime-url", url)
    assert json.loads(run("doctor"))["ok"]
    config = run("codex-config", "--dry-run", "--command", str(command))
    assert TOKEN not in config
    server = tomllib.loads(config)["mcp_servers"]["vks"]
    assert server["args"][:2] == ["--config-dir", str(profile.resolve())]
    current = cwd / "existing-config.toml"
    contents = b'[mcp_servers.other.env]\nOTHER_TOKEN = "synthetic-config-key"\n'
    current.write_bytes(contents)
    diff = run("config-diff", "--existing", str(current), "--command", str(command))
    assert "synthetic-config-key" not in diff and tomllib.loads(diff) == tomllib.loads(config)
    assert current.read_bytes() == contents
    current.write_text('[plugins."vks@test"]\nenabled = true\n', encoding="utf-8")
    rejected = await asyncio.to_thread(
        subprocess.run,
        [str(command), "config-diff", "--existing", str(current), "--command", str(command)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert rejected.returncode == 1 and "保留一种注册方式" in rejected.stderr
    settings = server_settings["args"] if server_settings else []
    if "--codex-library" in settings:
        await smoke_library(command, cwd, env, settings)
        settings = []
    args = settings or server["args"]
    params = StdioServerParameters(command=str(command), args=args, cwd=cwd, env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        initialized = await client.initialize()
        assert initialized.serverInfo.name == "visual-knowledge-studio"
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {"list_projects", "get_prototype_preview"} <= set(tools)
        assert "save_workspace_document" not in tools
        assert tools["get_workspace_document"].inputSchema["required"].count("release_id") == 1
        if settings:
            panel = tools["open_vks_workspace"]
            for action in ("open_vks_studio", "read_vks_document_image"):
                assert tools[action].meta["ui"]["visibility"] == ["app"]
            assert panel.meta["ui"]["visibility"] == ["app"]
            assert panel.meta["openai/ui"]["entrypoints"] == [{"type": "global"}]
            resource_uri = panel.meta["ui"]["resourceUri"]
            assert str((await client.list_resources()).resources[0].uri) == resource_uri
            resource = (await client.read_resource(resource_uri)).contents[0]
            assert resource.mimeType == "text/html;profile=mcp-app" and "VKS 知识工作台" in resource.text
            assert "window.VksReaderMarkdown={" in resource.text
            opened = await client.call_tool(panel.name, {})
            assert not opened.isError and opened.meta["vks/workspace"]["projects"] == []
            invalid = await client.call_tool(panel.name, {"project_id": "other"})
            assert invalid.isError
            invalid = await client.call_tool("open_vks_studio", {"fragment": "https://untrusted.example/"})
            assert invalid.isError and invalid.structuredContent["error"]["code"] == "VKS_INVALID_INPUT"
        else:
            assert "open_vks_workspace" not in tools
        outcome = await client.call_tool("list_projects", {"limit": 5})
        assert not outcome.isError and outcome.structuredContent["data"] == []
        denied = await client.call_tool("save_workspace_document", {})
        assert denied.isError and denied.structuredContent["error"]["code"] == "VKS_READONLY_PROFILE"
    print("Installed connector configure, doctor, and MCP SDK smoke passed.")


def main():
    parser = argparse.ArgumentParser()
    entry = parser.add_mutually_exclusive_group(required=True)
    entry.add_argument("--command", type=Path)
    entry.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    server_settings = None
    if args.manifest:
        bundle = args.manifest.resolve(strict=True).parent
        entry = json.loads(args.manifest.read_text(encoding="utf-8"))["mcpServers"]["vks"]
        server_settings = {"args": entry["args"]}
        if not entry["command"].startswith("./"):
            raise ValueError("Portable MCP commands must use a contained relative path")
        command = (bundle / entry["command"]).resolve(strict=True)
        command.relative_to(bundle)
    else:
        command = args.command.resolve(strict=True)
    with ThreadingHTTPServer(("127.0.0.1", 0), API) as api, TemporaryDirectory(prefix="vks 验证 ") as directory:
        thread = Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(smoke(command, Path(directory), f"http://127.0.0.1:{api.server_port}", server_settings))
        finally:
            api.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
