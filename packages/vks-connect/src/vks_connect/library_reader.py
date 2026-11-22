"""Serve one user-selected library through a trusted local reader."""

import asyncio
import json
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from .chatgpt_plan import PlanError, PlanSession
from .config import default_directory
from .credentials import CredentialError
from .library import LibraryError, LibraryStore
from .library_analysis import analyze_library, local_search, string
from .library_mcp import choose_library_file, open_library_section
from .sidebar import markdown_assets


class LibraryReader:
    def __init__(self, store, library_ref, plan, link_directory=None):
        self.store, self.library_ref, self.plan = store, library_ref, plan
        self.link_directory = link_directory

    def handle(self, action, data):
        library = self.store.current(self.library_ref)
        if action == "status":
            status = self.plan.status()
            status.pop("profile_id")
            return {**status, "can_ask": bool(library.plan_sections), "library": library.context()}
        if action == "connect":
            if not library.plan_sections:
                raise PlanError("VKS_LIBRARY_PLAN_UNAVAILABLE", "请用新版 Studio 导出可问答的成品库。")
            self.plan.begin()
            return {}
        if action == "disconnect":
            self.plan.disconnect()
            return {}
        if action == "models":
            return self.plan.models(self.plan.status()["profile_id"])
        if action == "search":
            return local_search(library, string(data, "question", 1000))
        if action == "chapter":
            page = library.read(string(data, "section_id", 96), 0, 64000)
            return {**page, "file_url": library.url(page["section_id"])}
        if action == "image":
            media, encoded = library.image(string(data, "section_id", 96), unquote(string(data, "path", 2048)))
            return {"url": f"data:{media};base64,{encoded}"}
        if action == "open":
            try:
                asyncio.run(open_library_section(library, string(data, "section_id", 96), self.link_directory))
            except (OSError, TimeoutError, RuntimeError, subprocess.CalledProcessError) as exc:
                raise PlanError("VKS_LIBRARY_OPEN_FAILED", "无法打开原文件，可复制预览底部的地址。") from exc
            return {}
        if action != "ask":
            raise ValueError("未知成品库操作。")
        return analyze_library(library, self.plan, data, lambda: self.store.current(self.library_ref))


def reader_page():
    js, css = markdown_assets()
    template = files("vks_connect").joinpath("library_reader.html").read_text(encoding="utf-8")
    return template.replace("<!-- markdown-css -->", f"<style>{css}</style>").replace(
        "<!-- markdown-js -->", f"<script>{js}</script>"
    ).encode()


class ReaderHandler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, *_):
        pass

    def reply(self, status, value, mime="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src data: blob:; font-src data:; connect-src 'self'; frame-src blob:; "
                         "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Host") != self.server.authority or self.path != self.server.route + "/":
            self.reply(404, {"error": "未找到页面。"})
            return
        self.reply(200, self.server.page, "text/html")

    def do_POST(self):
        parsed = urlsplit(self.path)
        allowed = (
            self.headers.get("Host") == self.server.authority
            and self.headers.get("Origin") == "http://" + self.server.authority
            and self.headers.get("Content-Type") == "application/json"
            and self.headers.get("X-VKS-Reader") == "1"
            and not parsed.query and not parsed.netloc and parsed.path.startswith(self.server.route + "/")
        )
        if not allowed:
            self.reply(403, {"error": "此操作仅限本机成品库阅读器。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            result = self.server.reader.handle(parsed.path[len(self.server.route) + 1:], data)
        except (PlanError, LibraryError, CredentialError, ValueError) as exc:
            self.reply(getattr(exc, "status", 422), {"error": str(exc) or "无效请求。"})
            return
        self.reply(200, result)


def open_reader(path, root=None):
    import webbrowser

    root = root or default_directory()
    # Keep the Codex sidebar's selected file independent of standalone reading.
    store = LibraryStore(root / "reader.library.json")
    if path is None:
        from pathlib import Path

        selected = asyncio.run(choose_library_file())
        if not selected:
            return
        path = Path(selected)
    library = store.select(path)
    with (ThreadingHTTPServer(("127.0.0.1", 0), ReaderHandler) as server,
          TemporaryDirectory(prefix="vks-reader-links-") as links):
        server.authority = f"127.0.0.1:{server.server_port}"
        server.route = "/" + secrets.token_urlsafe(24)
        server.reader = LibraryReader(store, library.ref, PlanSession(root), links)
        server.page = reader_page()
        url = "http://" + server.authority + server.route + "/"
        print("成品库阅读器已启动，按 Ctrl+C 退出。", flush=True)
        if not webbrowser.open(url):
            print(url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
