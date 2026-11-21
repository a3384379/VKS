"""Non-secret connection profiles and endpoint validation."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(value: str, label: str) -> str:
    error = f"{label} 必须是 HTTPS 地址或同机 loopback HTTP，且不含凭据、查询或片段"
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    if (
        not host
        or (port is not None and port < 1)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not is_loopback(host))
    ):
        raise ValueError(error)
    return value.rstrip("/")


def default_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/VKS/connector"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "VKS/connector"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vks/connector"


def connection_path(root: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        raise ValueError("连接名称必须由小写英文字母、数字或连字符组成")
    return root.resolve() / (name + ".json")


@dataclass(frozen=True)
class Connection:
    api_base_url: str = "http://127.0.0.1:8014"
    studio_base_url: str = "http://127.0.0.1:5193"
    runtime_mode: str = "same-machine"
    runtime_url: str | None = "http://127.0.0.1:5195"
    mcp_url: str | None = None
    default_project_id: str | None = None
    release_selector: str = "latest_saved"
    version: int = 1

    def __post_init__(self):
        for name in ("api_base_url", "studio_base_url", "runtime_url", "mcp_url"):
            value = getattr(self, name)
            if value is None and name in {"runtime_url", "mcp_url"}:
                continue
            object.__setattr__(self, name, validate_url(value, name))
        if self.runtime_mode not in {"same-machine", "documents-only"}:
            raise ValueError("runtime_mode 必须是 same-machine 或 documents-only")
        if self.runtime_mode == "documents-only" and self.runtime_url is not None:
            raise ValueError("仅文档模式不接受 Runtime 地址")
        if self.runtime_mode == "same-machine" and (
            self.runtime_url is None
            or not all(
                is_loopback(urlsplit(url).hostname)
                for url in (self.api_base_url, self.studio_base_url, self.runtime_url)
            )
        ):
            raise ValueError("同机模式要求 API、Studio 和 Runtime 都是 loopback 地址")
        if self.default_project_id is not None:
            try:
                object.__setattr__(self, "default_project_id", str(UUID(self.default_project_id)))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("default_project_id 必须是项目 UUID") from None
        if self.release_selector != "latest_saved" or type(self.version) is not int or self.version != 1:
            raise ValueError("不支持该连接配置版本或版本选择策略")


def load_connection(path: Path) -> Connection:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError
        return Connection(**json.loads(raw))
    except FileNotFoundError:
        raise ValueError("未找到连接配置。请先运行 vks-connect configure") from None
    except (TypeError, ValueError):
        raise ValueError("连接配置无效。请使用 configure 生成 UTF-8 配置") from None


def save_connection(path: Path, connection: Connection):
    save_private_json(path, asdict(connection))


def save_private_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
