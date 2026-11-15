"""Credential-free browser runtime with policies registered by the trusted Studio."""

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
POLICY = "default-src 'none'; script-src 'self' blob: 'unsafe-inline' 'unsafe-eval'; style-src blob: data: 'unsafe-inline'; img-src blob: data:; font-src data: blob:; frame-src 'self' about: blob:; worker-src blob:; connect-src 'none'; form-action 'none'; object-src 'none'; base-uri 'none'"
REGISTRY_LOCK = RLock()
MAX_REGISTRATIONS = 2048
REGISTRATION_TTL = 24 * 60 * 60


def registrations(server):
    """Bound pending/reload capabilities without changing persistent origins."""
    if not hasattr(server, "runtime_registrations"):
        server.runtime_registrations = {}
    rows = server.runtime_registrations
    cutoff = monotonic() - REGISTRATION_TTL
    # Insertion order follows the monotonic creation clock. No source page is
    # retained here, and expiring a token does not erase its origin's storage.
    while rows and next(iter(rows.values()))["created_at"] <= cutoff:
        rows.pop(next(iter(rows)))
    return rows


def external_origin(value):
    """Only exact external HTTPS origins, never a URL-shaped CSP expression."""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("外部地址必须是准确的 HTTPS 来源")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("外部地址无效") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", ascii_host)
        or ".." in ascii_host
        or "." not in ascii_host
        or any(
            ascii_host == suffix or ascii_host.endswith("." + suffix)
            for suffix in (
                "localhost",
                "local",
                "localdomain",
                "internal",
                "lan",
                "home",
                "test",
                "invalid",
                "example",
                "home.arpa",
                "onion",
            )
        )
    ):
        raise ValueError("仅支持明确的外部 HTTPS 来源，不能使用本机或保留地址")
    try:
        ipaddress.ip_address(ascii_host)
    except ValueError:
        pass
    else:
        raise ValueError("外部地址必须使用公开域名")
    # Alternate numerical loopback spellings must not pass as DNS names.
    if all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part) for part in ascii_host.split(".")):
        raise ValueError("外部地址不能使用数值主机")
    return "https://" + ascii_host + (":" + str(port) if port not in (None, 443) else "")


def origin_secret():
    directory = Path(
        os.environ.get("VKS_RUNTIME_DATA_DIR")
        or Path(os.environ.get("VKS_DATA_DIR", ".vks/studio-preview/data")) / "runtime"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "origin.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
    raw = path.read_bytes()
    if len(raw) != 32 or path.stat().st_mode & 0o077:
        raise ValueError("Runtime 来源密钥文件无效，要求私有 0600 文件")
    return raw


def origin_identity(value):
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


class RuntimeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Registration capabilities stay in browser memory, never request logs.
        pass

    def trusted_origin(self):
        allowed = os.environ.get("VKS_STUDIO_ALLOWED_ORIGINS", "http://127.0.0.1:5193,http://localhost:5193")
        origin = self.headers.get("Origin", "")
        return origin if origin in {item.strip() for item in allowed.split(",") if item.strip()} else None

    def allowed_host(self):
        try:
            parsed = urlsplit("http://" + self.headers.get("Host", ""))
            return parsed.hostname if parsed.port == self.server.server_port else None
        except ValueError:
            return None

    def json_response(self, code, body, origin=None):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        origin = self.trusted_origin()
        if (
            urlsplit(self.path).path != "/register"
            or self.allowed_host() != "localhost"
            or not origin
            or self.headers.get("Access-Control-Request-Method") != "POST"
            or self.headers.get("Access-Control-Request-Headers", "").lower() != "content-type"
        ):
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self):
        origin = self.trusted_origin()
        if urlsplit(self.path).path != "/register" or self.allowed_host() != "localhost" or not origin:
            self.send_error(403)
            return
        if self.headers.get_content_type() != "application/json":
            self.json_response(415, {"error": "Runtime 登记必须使用 JSON"}, origin)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16 * 1024:
                raise ValueError("Runtime 登记大小无效")
            data = json.loads(self.rfile.read(length))
            mode = data.get("mode")
            digest = data.get("digest")
            scope = data.get("storageScope")
            if (
                mode not in ("prototype", "document")
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                raise ValueError("Runtime 模式或资料摘要无效")
            if not isinstance(scope, dict) or any(
                not isinstance(scope.get(key), str) or not 0 < len(scope[key]) <= 500
                for key in ("accountId", "projectId", "packageId")
            ):
                raise ValueError("Runtime 缺少账号、项目或资料包身份")
            values = data.get("networkOrigins", [])
            if not isinstance(values, list) or len(values) > 32:
                raise ValueError("外部来源列表无效")
            if mode == "prototype" and values:
                raise ValueError("原型不能登记外部联网地址")
            origins = sorted({external_origin(value) for value in values})
            trusted = {
                origin_identity(item.strip())
                for item in os.environ.get("VKS_STUDIO_ALLOWED_ORIGINS", "").split(",")
                if item.strip()
            }
            if any(origin_identity(value) in trusted for value in origins):
                raise ValueError("不能将工作台来源登记为文档外部地址")
            identity = json.dumps(
                [mode, scope["accountId"], scope["projectId"], scope["packageId"], digest], ensure_ascii=False
            )
            with REGISTRY_LOCK:
                name = (
                    "vks-"
                    + hmac.new(origin_secret(), identity.encode(), hashlib.sha256).hexdigest()[:48]
                    + ".localhost"
                )
                token = secrets.token_urlsafe(32)
                record = {
                    "host": origin,
                    "hostname": name,
                    "mode": mode,
                    "networkOrigins": origins,
                    "created_at": monotonic(),
                }
                rows = registrations(self.server)
                while len(rows) >= MAX_REGISTRATIONS:
                    rows.pop(next(iter(rows)))
                rows[token] = record
            self.json_response(
                200, {"origin": f"http://{name}:{self.server.server_port}", "registration": token}, origin
            )
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            self.json_response(400, {"error": str(exc)}, origin)

    def do_GET(self):
        parsed = urlsplit(self.path)
        hostname = self.allowed_host()
        if not hostname:
            self.send_error(403)
            return
        file = {"/": "index.html", "/bridge.js": "bridge.js"}.get(parsed.path)
        if not file:
            self.send_error(404)
            return
        if file == "bridge.js":
            # Only public fixed JavaScript is available without a capability.
            raw = (ROOT / file).read_bytes()
            policy = POLICY
        else:
            from urllib.parse import parse_qs

            params = parse_qs(parsed.query)
            token = params.get("registration", [""])[0]
            with REGISTRY_LOCK:
                record = registrations(self.server).get(token)
            if not record or record["hostname"] != hostname:
                self.send_error(403)
                return
            policy = POLICY
            if record["mode"] == "document" and record["networkOrigins"]:
                external = " ".join(record["networkOrigins"])
                policy = policy.replace("connect-src 'none'", "connect-src " + external)
                for directive in ("script-src", "style-src", "img-src", "font-src", "frame-src"):
                    policy = re.sub(r"(" + directive + r" [^;]+)", r"\1 " + external, policy)
            config = json.dumps({"host": record["host"], "mode": record["mode"]}).replace("<", "\\u003c")
            raw = (
                (ROOT / file)
                .read_bytes()
                .replace(
                    b"<!--RUNTIME_CONFIG-->",
                    ('<script id="vks-runtime-config" type="application/json">' + config + "</script>").encode(),
                )
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if file.endswith(".js") else "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Origin-Agent-Cluster", "?1")
        self.send_header("Permissions-Policy", "document-domain=(), camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5195)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), RuntimeHandler).serve_forever()
