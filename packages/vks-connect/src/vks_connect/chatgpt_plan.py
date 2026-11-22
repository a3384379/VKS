"""Local Sign in with ChatGPT runtime shared by Studio and the library reader."""

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt

from .config import default_directory
from .credentials import CredentialError, LocalPlanCredentials, credential_store

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
PERMISSION = "chatgpt.tokens.use.direct"
SCOPE = "openid profile email offline_access resource.invoke " + PERMISSION
USAGE_URL = "https://chatgpt.com/#settings/usage"
CALLBACK = "/auth/callback"
TOKEN_FAILURE = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                 "refresh_token_invalidated", "refresh_token_reused"}
MAX_RESPONSE = 2 * 1024 * 1024


class PlanError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


def response_error(response):
    try:
        payload = response.json()
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        code = None
    if code in TOKEN_FAILURE:
        return PlanError("VKS_CHATGPT_RECONNECT", "ChatGPT 授权已失效，请重新登录。", 401)
    if code == "subscription_sharing_usage_limit_exceeded" or response.status_code == 429:
        return PlanError("VKS_CHATGPT_LIMIT", "已达到 ChatGPT 用量限制。请在 ChatGPT 设置中查看用量。", 429)
    if response.status_code in (401, 403):
        return PlanError("VKS_CHATGPT_UNAVAILABLE", "当前 ChatGPT 账户或工作区无法使用订阅额度。", response.status_code)
    if response.status_code == 400:
        return PlanError("VKS_CHATGPT_REQUEST", "ChatGPT 拒绝了本次请求，请检查所选模型是否支持该功能。")
    return PlanError("VKS_CHATGPT_NETWORK", "ChatGPT 服务暂不可用，请稍后重试。", 503)


def checked(response):
    if not response.is_success:
        raise response_error(response)
    return response


def response_text(result):
    return "".join(
        part["text"]
        for item in result.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )


def responses_body(body):
    """Limit the preview route to documented fields and in-band evidence."""
    return {
        "model": body["model"],
        "input": [
            {**message, "role": "developer"} if message.get("role") == "system" else message
            for message in body["input"]
        ],
        **{key: body[key] for key in ("text", "instructions") if key in body},
        "stream": True,
        "store": False,
    }


class CallbackHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_):
        pass

    def do_GET(self):
        given = urlsplit(self.path)
        try:
            values = parse_qs(given.query, max_num_fields=16) if len(self.path) <= 8192 else {}
        except ValueError:
            values = {}
        valid = (
            self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
            and given.path == CALLBACK and not given.netloc
            and len(values.get("state", [])) == 1
            and secrets.compare_digest(values["state"][0].encode(), self.server.oauth_state.encode())
            and all(len(parts) == 1 for parts in values.values())
        )
        if valid:
            self.server.result = {name: parts[0] for name, parts in values.items()}
        self.respond(200 if valid else 400, "已收到授权结果，请返回 VKS。" if valid else "此授权回调无效。")

    def respond(self, status, message):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(message.encode())


class PlanSession:
    def __init__(self, root=None, *, credentials=None, client=None, open_browser=webbrowser.open):
        self.root = (root or default_directory()) / "chatgpt"
        local = credentials is None and sys.platform == "darwin"
        self.profile_key = "local-profile" if local else "profile"
        self.credentials = (lambda: LocalPlanCredentials(self.root / "private")) if local else credentials or credential_store
        self.open_browser = open_browser
        self.http = client or httpx.Client(timeout=httpx.Timeout(45, connect=10), trust_env=False)
        self.pending = None
        self.guard = threading.Lock()
        self.message = ""
        self.access = None

    @contextmanager
    def database(self, *, write=False):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / "connections.sqlite3", timeout=50)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def read(db, key, default=None):
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def write(db, key, value):
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    @staticmethod
    def ref(profile):
        return "chatgpt-" + hashlib.sha256(profile["client_id"].encode()).hexdigest()

    def profile(self):
        with self.database() as db:
            return self.read(db, self.profile_key, {})

    def status(self):
        profile = self.profile()
        return {
            "connected": profile.get("connected", False),
            "account": profile.get("account", ""),
            "profile_id": profile.get("id", ""),
            "pending": bool(self.pending and self.pending.is_alive()),
            "message": self.message,
        }

    def setting(self, key, value=None):
        with self.database(write=value is not None) as db:
            if value is not None:
                self.write(db, key, value)
            return self.read(db, key, {})

    def claim_studio(self, owner):
        with self.database(write=True) as db:
            current = self.read(db, "studio-owner")
            if not current:
                current = {"owner": owner}
                self.write(db, "studio-owner", current)
            return current["owner"] == owner

    def token_request(self, **form):
        try:
            data = checked(self.http.post(TOKEN_URL, data={**form, "resource": RESOURCE})).json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except PlanError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise PlanError("VKS_CHATGPT_NETWORK", "无法完成 ChatGPT 授权请求。", 503) from exc

    def begin(self):
        with self.guard:
            if self.pending and self.pending.is_alive():
                return self.status()
            with self.database(write=True) as db:
                host = self.read(db, "host")
                if not host:
                    host = "urn:uuid:" + str(uuid4())
                    self.write(db, "host", host)
                previous = self.read(db, self.profile_key, {})
                # Reuse the installed OAuth client without reading its previous refresh token.
                client_id = previous.get("client_id") or self.read(db, "profile", {}).get("client_id")
            server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
            server.timeout = 0.5
            server.oauth_state = secrets.token_urlsafe(32)
            server.result = None
            nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            redirect = f"http://127.0.0.1:{server.server_port}{CALLBACK}"
            params = {
                "client_id": client_id or "dynamic_agent_client",
                "ext_agent_host_id": host,
                "response_type": "code", "redirect_uri": redirect, "scope": SCOPE, "resource": RESOURCE,
                "state": server.oauth_state, "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
                **({} if client_id else {"agent_name_hint": "VKS"}),
                **({"prompt": "consent"} if client_id and not previous.get("connected") else {}),
            }
            self.message = "请在浏览器完成 ChatGPT 授权。"
            self.pending = threading.Thread(
                target=self._sign_in, args=(server, previous, nonce, verifier, redirect, params), daemon=True
            )
            self.pending.start()
        return self.status()

    def _sign_in(self, server, previous, nonce, verifier, redirect, params):
        try:
            with server:
                self.open_browser(ISSUER + "/api/accounts/authorize?" + urlencode(params))
                deadline = time.monotonic() + 180
                while server.result is None and time.monotonic() < deadline:
                    server.handle_request()
                reply = server.result or {}
            if not reply or reply.get("error"):
                raise PlanError("VKS_CHATGPT_RECONNECT", "授权未完成，请重新登录。")
            requested_client = params["client_id"]
            registered_client = "" if requested_client == "dynamic_agent_client" else requested_client
            client_id = reply.get("client_id") or registered_client
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", client_id)
                or client_id == "dynamic_agent_client"
                or registered_client and registered_client != client_id
                or not reply.get("code")
            ):
                raise PlanError("VKS_CHATGPT_RECONNECT", "ChatGPT 授权回调无法验证，请重新登录。")
            data = self.token_request(
                grant_type="authorization_code", client_id=client_id, code=reply["code"],
                code_verifier=verifier, redirect_uri=redirect,
            )
            token = data["id_token"]
            discovery = checked(self.http.get(ISSUER + "/.well-known/openid-configuration")).json()
            jwks_url = discovery["jwks_uri"]
            if urlsplit(jwks_url).netloc != "auth.openai.com" or not jwks_url.startswith(ISSUER + "/"):
                raise ValueError("issuer")
            jwks = checked(self.http.get(jwks_url)).json()
            kid = jwt.get_unverified_header(token)["kid"]
            key = next(key for key in jwt.PyJWKSet.from_dict(jwks).keys if key.key_id == kid)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ISSUER, audience=client_id,
                                options={"require": ["exp", "sub", "nonce"]})
            if claims["nonce"] != nonce or previous.get("subject", claims["sub"]) != claims["sub"]:
                raise ValueError("identity")
            profile = {
                "id": str(uuid4()), "client_id": client_id, "subject": claims["sub"],
                "account": str(claims.get("email") or claims.get("name") or "ChatGPT")[:240],
                "connected": bool(PERMISSION in data.get("scope", "").split() and data.get("refresh_token")),
            }
            with self.database(write=True) as db:
                if self.read(db, self.profile_key, {}) != previous:
                    raise PlanError("VKS_CHATGPT_RECONNECT", "本机连接已变化，请重新登录。")
                if profile["connected"]:
                    self.credentials().set(self.ref(profile), data["refresh_token"])
                    self._remember_access(profile["id"], data)
                elif previous.get("connected"):
                    self.credentials().remove(self.ref(previous))
                self.write(db, self.profile_key, profile)
            self.message = "已连接 ChatGPT。" if profile["connected"] else "已登录，请授权 VKS 使用 ChatGPT 额度。"
        except PlanError as exc:
            self.message = str(exc)
        except Exception:
            self.message = "ChatGPT 登录未完成，无法验证或保存本机授权。"

    def _remember_access(self, profile_id, data):
        value = data["access_token"]
        if not isinstance(value, str) or not value.isascii() or not re.fullmatch(r"[!-~]{1,16384}", value):
            raise PlanError("VKS_CHATGPT_RECONNECT", "ChatGPT 返回的凭据无效，请重新登录。", 401)
        expires = min(max(int(data["expires_in"]), 1), 86400)
        self.access = profile_id, value, time.monotonic() + expires
        return value

    def bearer(self, profile_id):
        reconnect = None
        with self.database(write=True) as db:
            profile = self.read(db, self.profile_key, {})
            if not profile.get("connected") or profile.get("id") != profile_id:
                raise PlanError("VKS_CHATGPT_RECONNECT", "请连接并授权当前 ChatGPT 账户。", 401)
            if self.access and self.access[0] == profile_id and self.access[2] > time.monotonic() + 30:
                return self.access[1]
            storage = self.credentials()
            token = storage.get(self.ref(profile))
            if not token:
                raise PlanError("VKS_CHATGPT_RECONNECT", "未找到 ChatGPT 授权，请重新登录。", 401)
            try:
                data = self.token_request(
                    grant_type="refresh_token", client_id=profile["client_id"], refresh_token=token,
                )
                if "scope" in data and PERMISSION not in data["scope"].split():
                    raise PlanError("VKS_CHATGPT_RECONNECT", "ChatGPT 使用额度授权已取消，请重新登录。", 401)
                value = self._remember_access(profile_id, data)
                if data.get("refresh_token"):
                    storage.set(self.ref(profile), data["refresh_token"])
                return value
            except PlanError as exc:
                if exc.code != "VKS_CHATGPT_RECONNECT":
                    raise
                storage.remove(self.ref(profile))
                self.write(db, self.profile_key, {**profile, "connected": False})
                self.access = None
                reconnect = exc
        raise reconnect

    def disconnect(self):
        with self.database(write=True) as db:
            profile = self.read(db, self.profile_key, {})
            revoked = False
            storage = self.credentials() if profile.get("client_id") else None
            token = storage.get(self.ref(profile)) if storage else None
            if token:
                try:
                    metadata = checked(self.http.get(ISSUER + "/.well-known/openid-configuration")).json()
                    revoke = metadata["revocation_endpoint"]
                    if revoke.startswith(ISSUER + "/") and urlsplit(revoke).netloc == "auth.openai.com":
                        revoked = self.http.post(revoke, data={
                            "token": token, "token_type_hint": "refresh_token", "client_id": profile["client_id"],
                        }).status_code == 200
                except (httpx.HTTPError, PlanError, KeyError, ValueError):
                    pass
            if storage:
                storage.remove(self.ref(profile))
            # Change the generation so an in-flight sign-in cannot restore the session.
            self.write(db, self.profile_key, {**profile, "id": str(uuid4()), "connected": False})
            self.access = None
        self.message = "已退出 ChatGPT。" if revoked else "已退出本机连接，可在 ChatGPT 设置中查看远程连接。"
        return self.status()

    def models(self, profile_id):
        try:
            bearer = self.bearer(profile_id)
            data = checked(self.http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + bearer})).json()
            return [
                {"id": model["slug"], "name": model.get("display_name") or model["slug"]}
                for model in data.get("models", [])
                if model.get("visibility") == "list" and isinstance(model.get("slug"), str)
            ]
        except (httpx.HTTPError, CredentialError) as exc:
            raise PlanError("VKS_CHATGPT_NETWORK", "无法读取当前账号的 ChatGPT 模型。", 503) from exc

    def generate(self, profile_id, body, *, check=lambda: None, preview=None):
        token = self.bearer(profile_id)
        headers = {"Authorization": "Bearer " + token, "Accept": "text/event-stream"}
        size, events, parts = 0, [], []
        deadline = time.monotonic() + 120
        try:
            with self.http.stream("POST", RESOURCE + "/responses", headers=headers, json=responses_body(body)) as reply:
                if not reply.is_success:
                    body = bytearray()
                    for chunk in reply.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            break
                    reply = httpx.Response(reply.status_code, content=bytes(body[:65536]))
                    raise response_error(reply)
                media_type = reply.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                # The subscription endpoint can omit Content-Type on an SSE response.
                if media_type and media_type != "text/event-stream":
                    reported_type = media_type if re.fullmatch(r"[a-z0-9.+-]{1,40}/[a-z0-9.+-]{1,60}", media_type) else "未知类型"
                    request_id = reply.headers.get("x-request-id", "")
                    receipt = f"，请求 ID {request_id}" if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id) else ""
                    raise PlanError(
                        "VKS_CHATGPT_NETWORK",
                        f"ChatGPT 没有返回流式回复。（HTTP {reply.status_code}，{reported_type}{receipt}）",
                        503,
                    )
                for line in reply.iter_lines():
                    check()
                    size += len(line) + 1
                    if size > MAX_RESPONSE or time.monotonic() > deadline:
                        raise PlanError("VKS_CHATGPT_REQUEST", "回答超出读取上限，请缩小提问范围。")
                    if line.startswith("data:"):
                        events.append(line[5:].lstrip())
                        continue
                    if line or not events:
                        continue
                    event = json.loads("\n".join(events))
                    events.clear()
                    kind = event.get("type")
                    if kind == "response.output_text.delta" and preview:
                        parts.append(event.get("delta", ""))
                        preview("".join(parts))
                    elif kind == "response.completed":
                        result = event["response"]
                        if result.get("status") == "completed" and response_text(result).strip():
                            check()
                            if self.profile().get("id") == profile_id:
                                return result
                            raise PlanError("VKS_CHATGPT_RECONNECT", "ChatGPT 连接在回答期间已变化。", 409)
                    elif kind in {"response.failed", "response.incomplete", "response.cancelled", "error"}:
                        error = event.get("error") or (event.get("response") or {}).get("error") or event
                        if error.get("code") == "subscription_sharing_usage_limit_exceeded":
                            raise PlanError("VKS_CHATGPT_LIMIT", "已达到 ChatGPT 用量限制。", 429)
                        break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            if isinstance(exc, PlanError):
                raise
            raise PlanError("VKS_CHATGPT_NETWORK", "ChatGPT 回复未完成，可重新发起。", 503) from exc
        raise PlanError("VKS_CHATGPT_NETWORK", "ChatGPT 回复未完成，可重新发起。", 503)
