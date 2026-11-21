"""Optional native Codex App Server acceptance with an ephemeral local session."""

import json
import os
import re
import shutil
import subprocess
import tarfile
from queue import Queue
from threading import Thread
from uuid import uuid4

import pytest
from vks_api.app import create_app

from scripts.build_prototype_demo import build_bundle
from tests.prototype.test_raster_prototype import import_project
from tests.ui.test_frontend_browser import live_api


def _cli_settings(codex, directory, connector, connection, address, env):
    original = json.loads(subprocess.check_output([codex, "mcp", "list", "--json"], text=True))
    key = "vks_isolated_trial"
    cli = [codex]
    for server in original:
        name = server["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Existing MCP alias cannot be safely isolated by this acceptance test")
        # Some built-ins only exist after config load. An explicit local dummy
        # keeps those disabled entries valid without contacting their backends.
        transport = server["transport"]["type"]
        field, value = ("command", shutil.which("false")) if transport == "stdio" else ("url", address + "/unused")
        assert value
        cli.extend(["-c", f"mcp_servers.{name}.{field}={json.dumps(value)}"])
        cli.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    for field, value in {
        "command": str(connector),
        "args": ["--config-dir", str(connection), "mcp"],
        "enabled": True,
        "env_vars": ["VKS_CONNECT_TOKEN"],
        "startup_timeout_sec": 12,
    }.items():
        cli.extend(["-c", f"mcp_servers.{key}.{field}={json.dumps(value, ensure_ascii=False)}"])
    listed = json.loads(subprocess.check_output([*cli, "mcp", "list", "--json"], text=True, cwd=directory, env=env))
    assert [item["name"] for item in listed if item["enabled"]] == [key]
    for override in (
        "analytics.enabled=false",
        "features.plugins=false",
        "features.apps=false",
        'model_provider="synthetic_vks_host_check"',
        'model_providers.synthetic_vks_host_check.name="VKS synthetic"',
        f'model_providers.synthetic_vks_host_check.base_url="{address}"',
    ):
        cli.extend(["-c", override])
    return [*cli, "app-server", "--listen", "stdio://"], key


class LocalCodex:
    def __init__(self, arguments, directory, env):
        self.process = subprocess.Popen(
            arguments,
            cwd=directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.messages = Queue()
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()
        self.next_id = 1

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        finally:
            self.messages.put(None)

    def rpc(self, method, params, *, notification=False):
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            request["id"] = self.next_id
            self.next_id += 1
        self.process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        if notification:
            return None
        while (reply := self.messages.get(timeout=25)) is not None:
            if reply.get("id") == request["id"]:
                assert not reply.get("error"), (method, reply["error"].get("code"))
                return reply["result"]
        raise RuntimeError(f"Native host exited before completing {method}")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


@pytest.mark.skipif(
    not os.getenv("VKS_CODEX_HOST_PACKAGE"), reason="Requires a local native Codex and released-format tarball"
)
def test_ephemeral_native_codex_receives_version_and_registered_image(tmp_path):
    codex = shutil.which("codex")
    assert codex, "VKS_CODEX_HOST_PACKAGE requires the native Codex CLI"
    destination = tmp_path / "合成作品"
    destination.mkdir()
    with tarfile.open(os.environ["VKS_CODEX_HOST_PACKAGE"]) as package:
        package.extractall(destination, filter="data")
    connector = destination / "bin/vks-connect"
    env = {key: value for key, value in os.environ.items() if not key.startswith("VKS_")}
    app = create_app(tmp_path / "synthetic-api", enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "prototype-demo", "name": "VKS synthetic host probe"}, str(uuid4()))[
        "id"
    ]
    baseline = import_project(service, owner, pid, build_bundle(), str(uuid4()))
    release = service.save_snapshot(owner, pid, {"analysis_set_id": baseline}, str(uuid4()))["id"]
    env["VKS_CONNECT_TOKEN"] = service.create_token(
        owner,
        pid,
        {
            "name": "isolated-cli",
            "scopes": ["knowledge:read"],
        },
    )["token"]
    connection = tmp_path / "连接设置"
    with live_api(app) as address:
        subprocess.run(
            [
                str(connector),
                "--config-dir",
                str(connection),
                "configure",
                "--api-url",
                address,
                "--studio-url",
                address,
                "--runtime-url",
                address,
            ],
            capture_output=True,
            check=True,
            env=env,
        )
        cli, key = _cli_settings(codex, tmp_path, connector, connection, address, env)
        host = LocalCodex(cli, tmp_path, env)
        try:
            host.rpc(
                "initialize",
                {
                    "clientInfo": {"name": "VKS isolated contract acceptance", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            host.rpc("initialized", {}, notification=True)
            inventory = host.rpc("mcpServerStatus/list", {"serverName": key, "detail": "toolsAndAuthOnly"})
            tools = inventory["data"][0]["tools"]
            assert "get_prototype_preview" in tools and "save_workspace_document" not in tools and len(tools) == 8
            session = host.rpc(
                "thread/start",
                {
                    "cwd": str(tmp_path),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "environments": [],
                },
            )
            reference = {"project_id": pid, "release_id": release}
            target = {"threadId": session["thread"]["id"], "server": key}
            context = host.rpc("mcpServer/tool/call", {**target, "tool": "get_project_context", "arguments": reference})
            assert not context.get("isError")
            data = context["structuredContent"]["data"]
            assert data["scope"]["release_id"] == release
            page = next(row for row in data["prototype_pages"] if row["format"] == "raster")
            preview = host.rpc(
                "mcpServer/tool/call",
                {
                    **target,
                    "tool": "get_prototype_preview",
                    "arguments": {**reference, "locator": page["locator"], "max_width": 320},
                },
            )
            assert not preview.get("isError")
            assert [item["type"] for item in preview["content"]] == ["text", "image"]
            assert preview["content"][1]["mimeType"] == "image/png"
        finally:
            host.stop()
