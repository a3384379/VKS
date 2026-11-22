"""The local Codex MCP App for opening the VKS read-only workspace."""

import os
import re
import sys
from base64 import b64encode
from functools import cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import anyio
from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from .config import validate_url

SIDEBAR_TOOL = "open_vks_workspace"
STUDIO_TOOL = "open_vks_studio"
IMAGE_TOOL = "read_vks_document_image"
SIDEBAR_URI = "ui://vks/workspace.html"
MIME_TYPE = "text/html;profile=mcp-app"
MARKDOWN_ASSETS = ("markdown-vendor.js", "markdown-vendor.css", "markdown-licenses.txt")
RASTER_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_MAX_BYTES = 2 * 1024 * 1024
ID_PATTERN = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
STUDIO_FRAGMENT = re.compile(
    rf"project={ID_PATTERN}&release={ID_PATTERN}&view=documents"
    rf"(?:&doc={ID_PATTERN}&section={ID_PATTERN}&reader=standard)?"
)
ICON = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#176b57"/><path d="m8 9 8 15 8-15" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>"""


def sidebar_tool():
    return types.Tool(
        name=SIDEBAR_TOOL,
        title="VKS",
        description="在 Codex 左栏打开版本固定的 VKS 知识检索面板。",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        icons=[{"src": "data:image/svg+xml;base64," + b64encode(ICON).decode(), "mimeType": "image/svg+xml"}],
        _meta={
            "ui": {"resourceUri": SIDEBAR_URI, "visibility": ["app"]},
            "openai/ui": {"entrypoints": [{"type": "global"}]},
        },
    )


def sidebar_actions():
    return {
        STUDIO_TOOL: sidebar_action(
            STUDIO_TOOL,
            "在系统浏览器打开已配置的 VKS Studio。",
            {"fragment": {"type": "string", "maxLength": 240}},
            ["fragment"],
        ),
        IMAGE_TOOL: sidebar_action(
            IMAGE_TOOL,
            "读取保存版本中的文档图片。",
            {
                **{
                    name: {"type": "string", "pattern": f"^{ID_PATTERN}$"}
                    for name in (
                        "release_id",
                        "document_id",
                        "document_revision_id",
                    )
                },
                "path": {"type": "string", "minLength": 1, "maxLength": 2048},
            },
            ["release_id", "document_id", "document_revision_id", "path"],
        ),
    }


def sidebar_action(name, description, properties, required, *, model_visible=False, read_only=True, open_world=False):
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=read_only, destructiveHint=False, openWorldHint=open_world),
        _meta={"ui": {"visibility": ["app", "model"] if model_visible else ["app"]}},
    )


async def open_browser_url(url):
    if sys.platform == "win32":
        await anyio.to_thread.run_sync(os.startfile, url)
    else:
        launcher = "/usr/bin/open" if sys.platform == "darwin" else "xdg-open"
        arguments = ["-u", url] if sys.platform == "darwin" and url.startswith("file:") else [url]
        with anyio.fail_after(8):
            await anyio.run_process([launcher, *arguments], stdin=None)


async def open_studio(base_url, fragment):
    if fragment and not STUDIO_FRAGMENT.fullmatch(fragment):
        raise ValueError("Studio 链接格式无效")
    base = urlsplit(validate_url(base_url, "Studio"))
    url = urlunsplit((base.scheme, base.netloc, "/", "", fragment))
    await open_browser_url(url)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="已请求系统打开 Studio")],
        structuredContent={"data": {"requested": True}},
    )


@cache
def markdown_assets():
    package = files("vks_connect")
    assets = package
    if not assets.joinpath(MARKDOWN_ASSETS[0]).is_file():
        # Editable development installs share the canonical checked-in reader assets.
        assets = Path(__file__).resolve().parents[4] / "services/api/vks_api/reader_assets"
    return tuple(assets.joinpath(name).read_text(encoding="utf-8") for name in MARKDOWN_ASSETS[:2])


@cache
def sidebar_page():
    page = files("vks_connect").joinpath("sidebar.html").read_text(encoding="utf-8")
    js, css = markdown_assets()
    return page.replace("<!-- markdown-css -->", f"<style>{css}</style>").replace(
        "<!-- markdown-js -->", f"<script>{js}</script>"
    )


def register_sidebar(server):
    @server.list_resources()
    async def list_resources():
        return [types.Resource(name="vks-workspace", title="VKS", uri=SIDEBAR_URI, mimeType=MIME_TYPE)]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != SIDEBAR_URI:
            raise ValueError("VKS_NOT_FOUND")
        return [
            ReadResourceContents(
                sidebar_page(),
                MIME_TYPE,
                meta={
                    "ui": {
                        "prefersBorder": False,
                        "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
                    },
                    "openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen"]},
                },
            )
        ]


async def launch_sidebar(bridge, default_project_id, studio_base_url):
    projects = await bridge.dispatch("list_projects", {"limit": 100})
    visible = [{"id": item["id"], "name": item["name"]} for item in projects["data"]]
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="打开 VKS 知识工作台")],
        _meta={
            "vks/workspace": {
                "projects": visible,
                "defaultProjectId": default_project_id,
                "studioBaseUrl": studio_base_url,
            },
        },
    )
