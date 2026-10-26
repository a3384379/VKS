"""Instance-owned test authentication references. Never accepts a client file path."""

from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from .network import BUSINESS_HEADERS, origin


class AuthRegistryError(ValueError):
    pass


def fail():
    raise AuthRegistryError("实例测试认证引用不可用、已更新或不符合私有文件策略")


def private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 2 * 1024 * 1024:
                fail()
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                fail()
            raw = handle.read(2 * 1024 * 1024 + 1)
        value = json.loads(raw)
        if not isinstance(value, dict):
            fail()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        fail()


def safe_value(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail()
    return value


@dataclass(repr=False)
class AuthMaterial:
    auth_ref: str
    revision: str
    origin: str
    binding_hmac: str
    headers: dict = field(repr=False)
    storage_state: dict = field(repr=False)
    redactions: list[str] = field(repr=False)
    allowed_project_keys: list[str] | None = field(default=None, repr=False)

    @property
    def binding(self):
        return {"auth_ref": self.auth_ref, "auth_revision": self.revision, "auth_binding_hmac": self.binding_hmac}


def _resolve_auth(auth_ref, base_origin, binding_key, project_key=None):
    if auth_ref is None:
        return None
    if not isinstance(auth_ref, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,95}", auth_ref):
        fail()
    registry_path = os.getenv("VKS_RUNNER_SECRET_REGISTRY")
    if not registry_path or len(binding_key) < 32:
        fail()
    registry = private_json(registry_path)
    if registry.get("schema_version") != "1" or not isinstance(registry.get("entries"), dict):
        fail()
    entry = registry["entries"].get(auth_ref)
    if not isinstance(entry, dict) or entry.get("test_only") is not True:
        fail()
    if set(entry) - {"origin", "revision", "test_only", "headers", "headers_env", "storage_state_path", "project_keys"}:
        fail()
    try:
        registered_origin = origin(entry["origin"])
        if registered_origin != origin(base_origin):
            fail()
    except (ValueError, KeyError, TypeError):
        fail()
    project_keys = entry.get("project_keys")
    if project_keys is not None:
        if (
            not isinstance(project_keys, list)
            or not project_keys
            or len(project_keys) > 100
            or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,95}", key) for key in project_keys)
            or (project_key is not None and project_key not in project_keys)
        ):
            fail()
    revision = entry.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", revision):
        fail()
    headers = {}
    if not isinstance(entry.get("headers", {}), dict) or not isinstance(entry.get("headers_env", {}), dict):
        fail()
    for name, value in entry.get("headers", {}).items():
        if name.lower() not in {"authorization", "x-api-key"} | BUSINESS_HEADERS:
            fail()
        headers[name] = safe_value(value)
    for name, variable in entry.get("headers_env", {}).items():
        if name.lower() not in {"authorization", "x-api-key"} | BUSINESS_HEADERS or not re.fullmatch(
            r"VKS_RUNNER_AUTH_[A-Z0-9_]{1,100}", str(variable)
        ):
            fail()
        headers[name] = safe_value(os.getenv(variable))
    state = {"cookies": [], "origins": []}
    if entry.get("storage_state_path"):
        # Only an instance administrator's registry can name this local file.
        path = Path(entry["storage_state_path"])
        if not path.is_absolute():
            path = Path(registry_path).parent / path
        state = private_json(path)
    if (
        set(state) - {"cookies", "origins"}
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins"), list)
    ):
        fail()
    if len(state["cookies"]) > 100 or len(state["origins"]) > 1:
        fail()
    redactions = list(headers.values())
    host = urlsplit(base_origin).hostname
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict) or set(cookie) - {
            "name",
            "value",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        }:
            fail()
        if (
            cookie.get("domain") != host
            or not isinstance(cookie.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cookie["name"])
        ):
            fail()
        if not isinstance(cookie.get("path"), str) or not cookie["path"].startswith("/"):
            fail()
        if urlsplit(base_origin).scheme == "https" and cookie.get("secure") is not True:
            fail()
        redactions.append(safe_value(cookie.get("value")))
    for stored_origin in state["origins"]:
        if not isinstance(stored_origin, dict) or set(stored_origin) != {"origin", "localStorage"}:
            fail()
        if origin(stored_origin["origin"]) != registered_origin or not isinstance(stored_origin["localStorage"], list):
            fail()
        if len(stored_origin["localStorage"]) > 100:
            fail()
        for item in stored_origin["localStorage"]:
            if not isinstance(item, dict) or set(item) != {"name", "value"} or not isinstance(item["name"], str):
                fail()
            redactions.append(safe_value(item["value"]))
    if not headers and not state["cookies"] and not state["origins"]:
        fail()
    redactions += [x.split(" ", 1)[1] for x in headers.values() if x.lower().startswith("bearer ")]
    if sum(len(value) for value in redactions) > 128 * 1024 or len(redactions) > 200:
        fail()
    material = {
        "ref": auth_ref,
        "revision": revision,
        "origin": registered_origin,
        "headers": headers,
        "storage_state": state,
        "project_keys": project_keys,
    }
    message = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    fingerprint = hmac.new(binding_key.encode(), message, hashlib.sha256).hexdigest()
    return AuthMaterial(
        auth_ref,
        revision,
        registered_origin,
        fingerprint,
        headers,
        state,
        sorted(set(redactions), key=len, reverse=True),
        project_keys,
    )


def resolve_auth(auth_ref, base_origin, binding_key, *, project_key=None):
    try:
        return _resolve_auth(auth_ref, base_origin, binding_key, project_key)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
        fail()


def rotated_cookies(lines, url):
    """Validate each Set-Cookie separately, reducing scope to the exact auth host."""
    from http.cookies import SimpleCookie
    from email.utils import parsedate_to_datetime
    import time

    parsed = urlsplit(url)
    output, secrets = [], []
    if len(lines) > 100:
        raise AuthRegistryError("认证响应Cookie数量超过限制")
    for line in lines:
        if not isinstance(line, str) or len(line) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in line):
            continue
        jar = SimpleCookie()
        try:
            jar.load(line)
        except Exception:
            continue
        for name, item in jar.items():
            value = item.value
            if value:
                secrets.append(value)
            domain, path = item["domain"].lstrip(".").lower(), item["path"]
            if not path:
                path = parsed.path.rsplit("/", 1)[0] or "/"
            secure = bool(item["secure"])
            same_site = item["samesite"].capitalize() if item["samesite"] else "Lax"
            if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    or domain and domain != parsed.hostname or not path.startswith("/")
                    or secure != (parsed.scheme == "https")
                    or same_site not in ("Strict", "Lax", "None")
                    or same_site == "None" and not secure
                    or name.startswith("__Secure-") and not secure
                    or name.startswith("__Host-") and (not secure or domain or path != "/")):
                continue
            cookie = dict(name=name, value=value, domain=parsed.hostname, path=path,
                          secure=secure, httpOnly=bool(item["httponly"]), sameSite=same_site)
            try:
                if item["max-age"]:
                    cookie["expires"] = max(1, min(time.time() + int(item["max-age"]), time.time() + 365 * 86400))
                elif item["expires"]:
                    cookie["expires"] = max(1, parsedate_to_datetime(item["expires"]).timestamp())
            except (ValueError, TypeError, OverflowError):
                continue
            output.append(cookie)
    return output, secrets
