"""Read-only diagnostics for a configured VKS connection."""

from __future__ import annotations

from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from .config import Connection, is_loopback
from .credentials import CredentialError, load_token
from .gateway import READONLY_TOOLS, APIBridge, GatewayError


class DiagnosticError(ValueError):
    pass


def _client(url, **kwargs):
    return httpx.AsyncClient(
        follow_redirects=False, trust_env=not is_loopback(urlsplit(url).hostname), timeout=8, **kwargs
    )


async def doctor(connection: Connection, path, transport: str) -> dict:
    checks = {}

    async def inspect(name, operation):
        try:
            with anyio.fail_after(15):
                checks[name] = {"status": "ok", **await operation()}
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            httpx.HTTPError,
            GatewayError,
            McpError,
            ExceptionGroup,
        ) as exc:
            # Do not print upstream bodies, traffic, URLs, or credential text.
            if isinstance(exc, (CredentialError, DiagnosticError)):
                cause = str(exc)
            elif isinstance(exc, GatewayError):
                cause = "VKS API 拒绝读取，请检查只读凭据及项目权限"
            else:
                cause = "请检查服务地址、网络、代理或企业 CA，重定向不会被接受"
            checks[name] = {"status": "failed", "message": cause}

    async def probe_health(base):
        async with _client(base) as client:
            response = await client.get(base + "/api/v1/ready")
            response.raise_for_status()
            data = response.json()
        if data.get("status") != "ready" or not data.get("instance_id"):
            raise DiagnosticError("API 缺少 VKS 实例标识，请检查是否运行了本版本")
        return data["instance_id"]

    async def probe_api():
        return {"instance_id": await probe_health(connection.api_base_url)}

    async def probe_studio():
        base = connection.studio_base_url
        async with _client(base) as client:
            page = await client.get(base + "/")
            page.raise_for_status()
            if "<title>知境 Studio" not in page.text:
                raise DiagnosticError("此地址未返回知境 Studio 页面")
        return {"instance_id": await probe_health(base)}

    async def probe_runtime():
        async with _client(connection.runtime_url) as client:
            result = await client.get(connection.runtime_url + "/health")
            result.raise_for_status()
            if result.json().get("service") != "vks-runtime":
                raise DiagnosticError("此地址未返回 VKS Runtime 就绪状态")
        return {"registration": "requires_browser"}

    def projects(projects):
        return {"visible_project_count": len(projects.get("data", []))}

    def scope(context):
        data = context.get("data", {}).get("scope", {})
        if not data.get("release_id"):
            raise DiagnosticError("默认项目没有可读取的保存版本")
        return {"project_id": connection.default_project_id, "release_id": data["release_id"]}

    async def probe_stdio():
        url = connection.api_base_url
        token = load_token(path, "api", url)
        bridge = APIBridge(url, token)
        result = projects(await bridge.dispatch("list_projects", {"limit": 5}))
        if connection.default_project_id:
            result.update(
                scope(
                    await bridge.dispatch(
                        "get_project_context",
                        {
                            "project_id": connection.default_project_id,
                            "release_selector": connection.release_selector,
                        },
                    )
                )
            )
        return result

    async def probe_http():
        url = connection.mcp_url
        token = load_token(path, "mcp", url)
        identity = None

        async def capture(response):
            nonlocal identity
            identity = response.headers.get("x-vks-instance-id") or identity

        async with (
            _client(
                url,
                headers={"Authorization": f"Bearer {token}"},
                event_hooks={"response": [capture]},
            ) as client,
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if initialized.serverInfo.name != "visual-knowledge-studio" or names != set(READONLY_TOOLS):
                raise DiagnosticError("MCP 地址没有公开预期的 VKS 只读工具集")
            initial = await session.call_tool("list_projects", {"limit": 5})
            if initial.isError:
                raise DiagnosticError("MCP 凭据无法读取项目列表")
            result = projects(initial.structuredContent or {})
            if connection.default_project_id:
                context = await session.call_tool(
                    "get_project_context",
                    {
                        "project_id": connection.default_project_id,
                        "release_selector": connection.release_selector,
                    },
                )
                if context.isError:
                    raise DiagnosticError("MCP 无法读取默认项目的保存版本")
                result.update(scope(context.structuredContent or {}))
        if not identity:
            raise DiagnosticError("HTTP MCP 缺少 VKS 实例标识")
        return {"instance_id": identity, **result}

    await inspect("api", probe_api)
    await inspect("studio", probe_studio)
    if connection.runtime_url:
        await inspect("runtime", probe_runtime)
    else:
        checks["runtime"] = {"status": "disabled", "mode": "documents-only"}
    transport_check = "mcp_http" if transport == "http" else "api_credentials"
    await inspect(transport_check, probe_http if transport == "http" else probe_stdio)
    identities = {result["instance_id"] for result in checks.values() if "instance_id" in result}
    checks["instance_match"] = (
        {"status": "ok"}
        if len(identities) == 1 and all(checks[name]["status"] == "ok" for name in ("api", "studio", transport_check))
        else {"status": "failed", "message": "无法确认 API、Studio 和已选 MCP 传输使用同一 VKS 实例"}
    )
    return {"ok": all(row["status"] in {"ok", "disabled"} for row in checks.values()), "checks": checks}
