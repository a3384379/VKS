"""Named model connections, isolated credentials, discovery and explicit probes."""

import json
import os
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from services.browser_runner.network import NetworkDenied, NetworkPolicy, origin

from .model_credentials import require_admin
from .models import Base, Identified, Project, uid
from .service import Actor, DomainError


class ModelConnection(Identified, Base):
    __tablename__ = "model_connection"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(160))
    settings: Mapped[dict] = mapped_column(JSON)


class ConnectionForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str | None = Field(default=None, max_length=96)
    provider: Literal["openai", "deepseek", "qwen", "custom"] = "openai"
    name: str = Field(default="", max_length=160)
    endpoint: str = Field(min_length=1, max_length=2048)
    protocol: Literal["responses", "chat_completions"] = "responses"
    model_id: str = Field(default="", max_length=160)
    api_key: SecretStr | None = None
    max_output_tokens: int = Field(default=2500, ge=100, le=16000)
    daily_token_limit: int = Field(default=100000, ge=1000, le=100000000)
    enabled: bool = True
    allow_external: bool = True


def normalize_endpoint(value):
    value = value.strip().rstrip("/")
    try:
        p = urlsplit(value)
        origin(value)
        if p.query or p.fragment or p.scheme != "https" and p.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError()
        if p.scheme == "http" and os.getenv("VKS_MODEL_ALLOW_LOOPBACK") != "1":
            raise ValueError()
    except (ValueError, NetworkDenied):
        raise DomainError("VKS_INVALID_INPUT", "请填写不含密钥和查询参数的 HTTPS 服务地址", 422) from None
    for tail in ("/chat/completions", "/responses", "/models"):
        if value.endswith(tail):
            value = value[: -len(tail)]
            break
    return value


def setup_network(endpoint, path):
    return NetworkPolicy(
        endpoint,
        [endpoint],
        [x.strip() for x in os.getenv("VKS_MODEL_ALLOWED_CIDRS", "0.0.0.0/0,::/0").split(",") if x.strip()],
        allow_loopback=os.getenv("VKS_MODEL_ALLOW_LOOPBACK") == "1",
        write_paths=[urlsplit(endpoint).path.rstrip("/") + path],
    )


class ConnectionService:
    def __init__(self, service):
        self.service = service

    def list(self, actor, pid):
        from .ai_gateway import AiService
        from .model_catalog import PROVIDERS
        from .model_credentials import key_for_ref

        require_admin(self.service, actor, pid)
        policy = AiService(self.service).get_policy(actor, pid)
        with self.service.db() as s:
            rows = list(
                s.scalars(
                    select(ModelConnection)
                    .where(ModelConnection.project_id == pid)
                    .order_by(ModelConnection.created_at)
                )
            )
            out = [
                {
                    "id": r.id,
                    "provider": r.provider,
                    "name": r.name,
                    "settings": {k: v for k, v in r.settings.items() if k != "secret_ref"},
                    "configured": bool(key_for_ref(r.settings.get("secret_ref"), r.settings["endpoint"])),
                    "active": (r.id == policy.get("connection_id"))
                    if policy.get("connection_id")
                    else (
                        r.settings.get("secret_ref") == policy.get("secret_ref")
                        and r.settings["endpoint"] == policy["endpoint"]
                        and r.settings["model_id"] == policy["model_id"]
                    ),
                }
                for r in rows
            ]
        return {
            "providers": PROVIDERS,
            "connections": out,
            "policy": policy,
            "legacy_configured": bool(key_for_ref("model-default", "https://api.openai.com/v1")),
        }

    def _prepare(self, actor, pid, form):
        from .ai_gateway import ModelPolicy
        from .model_credentials import key_for_ref

        require_admin(self.service, actor, pid)
        endpoint = normalize_endpoint(form.endpoint)
        with self.service.db() as s:
            row = s.get(ModelConnection, form.connection_id) if form.connection_id else None
            if form.connection_id and (not row or row.project_id != pid):
                raise DomainError("VKS_NOT_FOUND", "未找到连接", 404)
            prior = dict(row.settings) if row else None
            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
            legacy_endpoint = (policy.settings.get("endpoint") if policy else "") or "https://api.openai.com/v1"
            legacy_ref = policy.settings.get("secret_ref") if policy else "model-default"
        entered = form.api_key.get_secret_value().strip() if form.api_key else ""
        if entered and (len(entered) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in entered)):
            raise DomainError("VKS_INVALID_INPUT", "API Key 格式无效", 422)
        ref = None
        if not entered and prior and prior["endpoint"] == endpoint and row.provider == form.provider:
            ref = prior.get("secret_ref")
        # An unbound legacy key came from the OpenAI-only setup screen.
        if (
            not entered
            and not ref
            and endpoint == legacy_endpoint
            and legacy_ref == "model-default"
            and (policy or form.provider == "openai")
        ):
            ref = "model-default"
        if not entered and not ref and form.provider == "openai" and endpoint == "https://api.openai.com/v1":
            ref = "model-default"
        key = entered or (key_for_ref(ref, endpoint) if ref else "")
        if not key:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "请填写此服务商的 API Key；其他服务的密钥不会自动复用", 422)
        return endpoint, key, ref

    def _request(self, endpoint, key, path, body=None):
        method = "GET" if body is None else "POST"
        try:
            status, _, raw = setup_network(endpoint, path).fetch(
                endpoint + path,
                method=method,
                body=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"},
                trusted_headers={"Authorization": "Bearer " + key},
                allow_write=body is not None,
                max_bytes=2 * 1024 * 1024,
                timeout=25,
            )
        except (NetworkDenied, OSError, ValueError):
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "无法连接该服务，请检查地址、网络和服务状态", 503) from None
        if status != 200:
            messages = {
                401: "API Key 无效或已过期",
                403: "此 Key 无权访问该服务或模型",
                404: "接口或模型不存在，请检查服务地址与协议",
                429: "服务额度不足或请求过于频繁",
            }
            raise DomainError(
                "VKS_MODEL_REQUEST_FAILED", messages.get(status, "模型服务暂时不可用"), 502, {"provider_status": status}
            )
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise DomainError("VKS_INVALID_INPUT", "服务未返回有效 JSON，请检查接口地址", 422) from None

    def discover(self, actor, pid, form):
        endpoint, key, _ = self._prepare(actor, pid, form)
        native_qwen = form.provider == "qwen" and endpoint.endswith("/compatible-mode/v1")
        truncated = False
        if native_qwen:
            native = endpoint.removesuffix("/compatible-mode/v1") + "/api/v1"
            items = []
            for page in range(1, 11):
                data = self._request(native, key, f"/models?capabilities=TG&page_size=100&page_no={page}")
                output = data.get("output", {}) if isinstance(data, dict) else {}
                batch = output.get("models")
                if not isinstance(batch, list):
                    raise DomainError("VKS_INVALID_INPUT", "服务未返回有效的百炼模型目录", 422)
                items.extend(batch)
                total = output.get("total")
                if not batch or type(total) is int and len(items) >= total or len(batch) < 100:
                    break
                if page == 10:
                    truncated = True
        else:
            data = self._request(endpoint, key, "/models")
            items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise DomainError("VKS_INVALID_INPUT", "服务未提供兼容的模型列表，可使用预设或自定义模型", 422)
        out = {}
        for item in items[:5000]:
            if not isinstance(item, dict):
                continue
            ident = item.get("id") or item.get("model")
            if not isinstance(ident, str) or not ident.strip() or len(ident) > 160:
                continue
            # Keep opaque IDs intact; the account may expose text and other models.
            out[ident] = {"id": ident, "name": str(item.get("name") or ident)[:160], "description": "当前 Key 可见模型"}
        with self.service.db() as s:
            self.service.audit(s, actor, pid, "model.connection.discover", pid)
        return {
            "models": sorted(out.values(), key=lambda r: r["id"]),
            "source": "account",
            "endpoint": endpoint,
            "truncated": truncated,
        }

    def test(self, actor, pid, form):
        from .ai_gateway import AiService, PolicyInput

        endpoint, key, _ = self._prepare(actor, pid, form)
        if not form.model_id.strip():
            raise DomainError("VKS_INVALID_INPUT", "请先选择模型", 422)
        ai = AiService(self.service)
        config = PolicyInput(
            **{k: v for k, v in ai.get_policy(actor, pid).items() if k in PolicyInput.model_fields}
        ).model_dump()
        config.update(model_id=form.model_id.strip(), max_output_tokens=min(config["max_output_tokens"], 256))
        messages = [{"role": "user", "content": "Reply with OK."}]
        body = (
            {
                "model": config["model_id"],
                "input": messages,
                "max_output_tokens": config["max_output_tokens"],
                "store": False,
            }
            if form.protocol == "responses"
            else {
                "model": config["model_id"],
                "messages": messages,
                "max_completion_tokens": config["max_output_tokens"],
            }
        )
        # Only a fixed connection probe is sent, never project documents.
        try:
            setup_network(endpoint, "/responses" if form.protocol == "responses" else "/chat/completions").resolve(
                endpoint
            )
        except (NetworkDenied, ValueError, OSError):
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "无法解析或访问此服务地址", 503) from None
        usage_id = ai.reserve(actor, pid, config, len(json.dumps(body).encode()) + 256)
        started = monotonic()
        ins = outs = None
        try:
            data = self._request(
                endpoint, key, "/responses" if form.protocol == "responses" else "/chat/completions", body
            )
            usage = data.get("usage", {})
            a = usage.get("input_tokens", usage.get("prompt_tokens"))
            b = usage.get("output_tokens", usage.get("completion_tokens"))
            if type(a) is int and type(b) is int and 0 <= a <= 1000000000 and 0 <= b <= 1000000000:
                ins, outs = a, b
            if form.protocol == "responses":
                result = "".join(
                    c.get("text", "")
                    for item in data.get("output", [])
                    if item.get("type") == "message"
                    for c in item.get("content", [])
                    if c.get("type") == "output_text"
                )
            else:
                result = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not isinstance(result, str) or not result.strip():
                raise DomainError(
                    "VKS_INVALID_INPUT", "接口已响应，但没有生成文本；请检查模型类型、协议或输出预算", 422
                )
            with self.service.db() as s:
                self.service.require(s, actor, pid, "project:manage")
            ai.settle(usage_id, config, ins, outs, "succeeded")
            return {
                "ok": True,
                "model_id": config["model_id"],
                "latency_ms": round((monotonic() - started) * 1000),
                "message": "所选模型已成功生成测试回答",
                "input_tokens": ins,
                "output_tokens": outs,
            }
        except Exception as exc:
            ai.settle(
                usage_id,
                config,
                ins,
                outs,
                "failed",
                exc.code if isinstance(exc, DomainError) else "VKS_MODEL_RESPONSE_INVALID",
            )
            if isinstance(exc, DomainError):
                raise
            raise DomainError("VKS_INVALID_INPUT", "模型响应格式不兼容，请检查协议与模型", 422) from None

    def save(self, actor, pid, form, expected):
        from .ai_gateway import ModelPolicy, PolicyInput, endpoint_policy
        from .model_catalog import PROVIDERS
        from .model_credentials import register_origin, store_connection_key

        endpoint, key, ref = self._prepare(actor, pid, form)
        if not form.model_id.strip():
            raise DomainError("VKS_INVALID_INPUT", "请先选择模型", 422)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            p = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid).with_for_update())
            self.service._version(p.row_version if p else 1, expected)
            row = s.get(ModelConnection, form.connection_id) if form.connection_id else None
            if form.connection_id and (not row or row.project_id != pid):
                raise DomainError("VKS_NOT_FOUND", "未找到连接", 404)
            if not ref:
                ref = store_connection_key(self.service, endpoint, key)
            register_origin(self.service, endpoint)
            config = PolicyInput(
                **{k: v for k, v in (p.settings if p else {}).items() if k in PolicyInput.model_fields}
            ).model_dump()
            connection_id = row.id if row else uid()
            config.update(
                connection_id=connection_id,
                endpoint=endpoint,
                protocol=form.protocol,
                model_id=form.model_id.strip(),
                secret_ref=ref,
                max_output_tokens=form.max_output_tokens,
                daily_token_limit=form.daily_token_limit,
                enabled=form.enabled,
                allow_external=form.allow_external,
            )
            if config["enabled"]:
                endpoint_policy(config)
            provider = next(r for r in PROVIDERS if r["id"] == form.provider)
            row = row or ModelConnection(id=connection_id, project_id=pid, provider=form.provider, name="", settings={})
            row.provider = form.provider
            row.name = form.name.strip() or provider["name"]
            row.settings = config
            s.add(row)
            if p:
                p.settings = config
                p.row_version += 1
            else:
                p = ModelPolicy(id=uid(), project_id=pid, settings=config, row_version=2)
                s.add(p)
            self.service.audit(s, actor, pid, "model.connection.save", row.id)
            s.flush()
        return self.list(actor, pid)


def install_connections(app, service, actor, envelope):
    shared = ConnectionService(service)
    ActorDep = Annotated[Actor, Depends(actor)]
    prefix = "/api/v1/projects/{pid}/model-connections"

    @app.get(prefix)
    def connections(pid: str, request: Request, a: ActorDep):
        return envelope(request, shared.list(a, pid))

    @app.post(prefix + "/models")
    def discover(pid: str, form: ConnectionForm, request: Request, a: ActorDep):
        return envelope(request, shared.discover(a, pid, form))

    @app.post(prefix + "/test")
    def probe(pid: str, form: ConnectionForm, request: Request, a: ActorDep):
        return envelope(request, shared.test(a, pid, form))

    @app.post(prefix)
    def save(pid: str, form: ConnectionForm, request: Request, a: ActorDep, if_match: Annotated[str, Header()]):
        return envelope(request, shared.save(a, pid, form, if_match))
