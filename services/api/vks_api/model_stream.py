"""Bounded model SSE transport; partial prose stays provisional until validation."""

import hashlib
import http.client
import json
import socket
import threading
import time
from contextvars import ContextVar

from .service import DomainError

CURRENT_MODEL_STREAM = ContextVar("vks_model_stream", default=None)
LIMIT = 2 * 1024 * 1024


def explanation_prefix(source):
    """Decode only a top-level explanation string, including partial escapes."""
    decoder = json.JSONDecoder()
    i = 0
    try:
        while i < len(source) and source[i].isspace():
            i += 1
        if source[i : i + 1] != "{":
            return ""
        i += 1
        while True:
            while i < len(source) and source[i].isspace():
                i += 1
            key, i = decoder.raw_decode(source, i)
            while i < len(source) and source[i].isspace():
                i += 1
            if source[i : i + 1] != ":":
                return ""
            i += 1
            while i < len(source) and source[i].isspace():
                i += 1
            if key == "explanation":
                if source[i : i + 1] != '"':
                    return ""
                i += 1
                result = []
                escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                while i < len(source):
                    char = source[i]
                    if char == '"' or ord(char) < 32:
                        break
                    if char != "\\":
                        if 0xD800 <= ord(char) <= 0xDFFF:
                            break
                        result.append(char)
                        i += 1
                        continue
                    if i + 1 >= len(source):
                        break
                    char = source[i + 1]
                    if char in escapes:
                        result.append(escapes[char])
                        i += 2
                    elif char == "u":
                        if i + 6 > len(source):
                            break
                        code = int(source[i + 2 : i + 6], 16)
                        i += 6
                        if 0xD800 <= code <= 0xDBFF:
                            if source[i : i + 2] != "\\u" or i + 6 > len(source):
                                break
                            low = int(source[i + 2 : i + 6], 16)
                            if not 0xDC00 <= low <= 0xDFFF:
                                break
                            code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                            i += 6
                        elif 0xDC00 <= code <= 0xDFFF:
                            break
                        result.append(chr(code))
                    else:
                        break
                return "".join(result)
            _, i = decoder.raw_decode(source, i)
            while i < len(source) and source[i].isspace():
                i += 1
            if source[i : i + 1] != ",":
                return ""
            i += 1
    except (ValueError, IndexError):
        return ""


class ModelStream:
    def __init__(self, emit):
        self.emit = emit
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.connection = self.sock = None
        self.validate = None
        self.checked_at = 0
        self.published = ""

    def check(self):
        if self.cancelled.is_set():
            raise DomainError("VKS_CANCELLED", "本次回答已停止", 499)

    def preview(self, raw):
        self.check()
        value = explanation_prefix(raw)
        if not value.startswith(self.published) or value == self.published:
            return
        if self.validate and time.monotonic() - self.checked_at >= 1:
            self.validate()
            self.checked_at = time.monotonic()
        self.emit("delta", {"text": value[len(self.published) :]})
        self.published = value

    def attach(self, connection):
        with self.lock:
            self.connection = connection
            self.sock = getattr(connection, "sock", None) or self.sock
        self.check()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            conn, sock = self.connection, self.sock
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if conn is not None:
            conn.close()


def request_stream(config, body, stream):
    from . import ai_gateway as gateway
    from .model_credentials import key_for_ref
    from .model_transport import MODEL_CONNECTIONS, PinnedTLSConnection
    from services.browser_runner.network import NetworkDenied

    with gateway._model_preflight():
        stream.check()
        timeout = config.get("_request_timeout_seconds", 120)
        if type(timeout) is not int or not 30 <= timeout <= 900:
            gateway.failure("VKS_INVALID_INPUT", "模型请求等待上限无效")
        policy, url = gateway.endpoint_policy(config)
        try:
            p, ip, port = policy.resolve(url)
        except (NetworkDenied, ValueError):
            gateway.failure("VKS_INVALID_INPUT", "模型端点解析或出站策略拒绝请求")
        key = key_for_ref(config.get("secret_ref"), config["endpoint"])
        if config.get("secret_ref") and not key:
            gateway.failure("VKS_DEPENDENCY_UNAVAILABLE", "模型凭证引用未配置", 503)
    identity = (p.scheme, p.hostname, ip, port, hashlib.sha256((key or "").encode()).digest())
    conn = MODEL_CONNECTIONS.take(
        identity,
        lambda: (
            PinnedTLSConnection(ip, port, p.hostname, timeout)
            if p.scheme == "https"
            else http.client.HTTPConnection(ip, port, timeout=timeout)
        ),
        timeout,
    )
    usage = response = None
    try:
        stream.attach(conn)
        host = (f"[{p.hostname}]" if ":" in p.hostname else p.hostname) + (f":{port}" if port not in (80, 443) else "")
        headers = {
            "Host": host,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "VKS-Model-Gateway/1.0",
        }
        if key:
            headers["Authorization"] = "Bearer " + key
        payload = {**body, "stream": True}
        if config["protocol"] == "chat_completions":
            payload["stream_options"] = {"include_usage": True}
        conn.request("POST", p.path or "/", body=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
        stream.attach(conn)
        response = conn.getresponse()
        if response.status in (429, 500, 502, 503, 504):
            gateway.failure("VKS_DEPENDENCY_UNAVAILABLE", "模型服务暂时不可用，请稍后重试", 503, True)
        if response.status != 200:
            gateway.failure("VKS_INVALID_INPUT", "模型服务拒绝流式请求或发生重定向")
        if "text/event-stream" not in response.getheader("Content-Type", "").lower():
            raw = response.read(LIMIT + 1)
            stream.check()
            if len(raw) > LIMIT:
                gateway.failure("VKS_LIMIT_EXCEEDED", "模型响应超过体积限制", 413)
            return json.loads(raw)
        text, finish, size, data = "", None, 0, []
        while True:
            stream.check()
            line = response.readline(LIMIT + 1)
            if not line:
                gateway.failure("VKS_DEPENDENCY_UNAVAILABLE", "连接提前结束，流式回答未完成，请重试", 503)
            size += len(line)
            if size > LIMIT:
                gateway.failure("VKS_LIMIT_EXCEEDED", "模型响应超过体积限制", 413)
            line = line.decode("utf-8-sig").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line or not data:
                continue
            raw, data = "\n".join(data), []
            if raw == "[DONE]":
                if config["protocol"] == "chat_completions" and finish == "stop" and usage:
                    return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": usage}
                gateway.failure("VKS_INVALID_INPUT", "模型未返回完整回答及用量")
            event = json.loads(raw)
            if event.get("usage") and (not event.get("choices") or event["choices"][0].get("finish_reason")):
                usage = event["usage"]
            kind = event.get("type")
            if kind in {
                "response.completed",
                "response.failed",
                "response.incomplete",
                "response.cancelled",
            } and event.get("response", {}).get("usage"):
                usage = event["response"]["usage"]
            if event.get("error") or kind in {"error", "response.failed", "response.incomplete", "response.cancelled"}:
                gateway.failure("VKS_DEPENDENCY_UNAVAILABLE", "模型未能完成本次回答，请重试", 503)
            delta = event.get("delta") if kind == "response.output_text.delta" else None
            choices = event.get("choices", [])
            if choices:
                choice = choices[0]
                delta = choice.get("delta", {}).get("content")
                finish = choice.get("finish_reason") or finish
                if finish and finish != "stop":
                    gateway.failure("VKS_INVALID_INPUT", "模型生成未完成，回答可能达到输出限制")
            if isinstance(delta, str):
                text += delta
                stream.preview(text)
            if kind == "response.completed":
                result = event.get("response", {})
                if result.get("status") != "completed":
                    gateway.failure("VKS_INVALID_INPUT", "模型生成未完成")
                if not result.get("output") and text:
                    result = {
                        **result,
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                    }
                return result
            if config["protocol"] == "chat_completions" and finish == "stop" and usage and event.get("usage"):
                return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": usage}
    except Exception as exc:
        if stream.cancelled.is_set():
            error = DomainError("VKS_CANCELLED", "本次回答已停止", 499)
        elif isinstance(exc, DomainError):
            error = exc
        elif isinstance(exc, TimeoutError):
            error = DomainError("VKS_DEPENDENCY_UNAVAILABLE", "模型流式响应超时，回答未完成，可重试", 503)
        else:
            error = DomainError("VKS_DEPENDENCY_UNAVAILABLE", "流式连接异常，回答未完成，可重试", 503)
        if usage:
            it, ot = (
                usage.get("input_tokens", usage.get("prompt_tokens")),
                usage.get("output_tokens", usage.get("completion_tokens")),
            )
            if type(it) is int and type(ot) is int and min(it, ot) >= 0 and max(it, ot) <= 1000000000:
                error._vks_accounted_usage = (it, ot)
        if error is exc:
            raise
        raise error from exc
    finally:
        if response is not None:
            response.close()
        conn.close()  # A terminal SSE event may leave unread bytes; never pool it.
