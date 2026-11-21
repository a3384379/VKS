"""Instance-local model credential storage. Values never enter response DTOs."""

import json
import os
from threading import RLock

from pydantic import SecretStr

from packages.private_files import read_private_file, write_private_file
from services.browser_runner.network import origin

from .models import uid
from .service import DomainError

ENV_NAME = "VKS_MODEL_API_KEY"
MAX_STORE_BYTES = 1024 * 1024


def credential_path(service):
    return service.data_dir / "model-credentials.json"


STORE_LOCK = RLock()


def _read_store(service):
    path = credential_path(service)
    if path.is_symlink():
        raise DomainError("VKS_INVALID_INPUT", "凭据文件不能是符号链接", 422)
    try:
        data = json.loads(read_private_file(path, MAX_STORE_BYTES).decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        raise DomainError("VKS_INVALID_INPUT", "本机模型凭据文件无法读取，请重新配置", 503) from None


def _write_store(service, data):
    path = credential_path(service)
    if path.is_symlink():
        raise DomainError("VKS_INVALID_INPUT", "凭据文件不能是符号链接", 422)
    raw = json.dumps(data).encode("utf-8")
    if len(raw) > MAX_STORE_BYTES:
        raise DomainError("VKS_LIMIT_EXCEEDED", "本机模型凭据文件已达存储上限", 409)
    try:
        write_private_file(path, raw)
    except OSError:
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "无法保存本机模型凭据", 503) from None


def _valid_key(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and all(33 <= ord(c) <= 126 for c in value)


def _key_var(ref):
    return "VKS_MODEL_CONNECTION_KEY_" + ref.removeprefix("model-connection-").replace("-", "").upper()


def key_for_ref(ref, endpoint):
    if ref == "model-default":
        return os.getenv(ENV_NAME, "")
    if not isinstance(ref, str) or not ref.startswith("model-connection-"):
        return ""
    name = _key_var(ref)
    if os.getenv(name + "_ENDPOINT") != endpoint.rstrip("/"):
        return ""
    return os.getenv(name, "")


def load_credential(service):
    data = _read_store(service)
    value = data.get(ENV_NAME)
    if value is not None:
        if not _valid_key(value):
            raise DomainError("VKS_INVALID_INPUT", "本机模型凭据无效", 503)
        os.environ[ENV_NAME] = value
    profiles = data.get("connections", {})
    origins = data.get("allowed_origins", [])
    if not isinstance(profiles, dict) or not isinstance(origins, list) or any(not isinstance(x, str) for x in origins):
        raise DomainError("VKS_INVALID_INPUT", "本机模型连接配置无效", 503)
    for ref, record in profiles.items():
        if (
            not isinstance(record, dict)
            or not ref.startswith("model-connection-")
            or not _valid_key(record.get("key"))
            or not isinstance(record.get("endpoint"), str)
        ):
            continue
        name = _key_var(ref)
        os.environ[name] = record["key"]
        os.environ[name + "_ENDPOINT"] = record["endpoint"]
    origins = data.get("allowed_origins", [])
    if origins:
        current = {v.strip() for v in os.getenv("VKS_MODEL_ALLOWED_ORIGINS", "").split(",") if v.strip()}
        os.environ["VKS_MODEL_ALLOWED_ORIGINS"] = ",".join(sorted(current | set(origins)))


def store_connection_key(service, endpoint, key):
    ref = "model-connection-" + uid()
    with STORE_LOCK:
        data = _read_store(service)
        records = data.get("connections", {})
        if len(records) >= 256:
            raise DomainError("VKS_LIMIT_EXCEEDED", "本机连接密钥已达上限", 409)
        data["connections"] = {**records, ref: {"endpoint": endpoint, "key": key}}
        _write_store(service, data)
    name = _key_var(ref)
    os.environ[name] = key
    os.environ[name + "_ENDPOINT"] = endpoint
    return ref


def register_origin(service, endpoint):
    target = origin(endpoint)
    with STORE_LOCK:
        data = _read_store(service)
        data["allowed_origins"] = sorted(set(data.get("allowed_origins", [])) | {target})
        _write_store(service, data)
    current = {x.strip() for x in os.getenv("VKS_MODEL_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    os.environ["VKS_MODEL_ALLOWED_ORIGINS"] = ",".join(sorted(current | {target}))


def require_admin(service, actor, pid):
    with service.db() as session:
        service.require(session, actor, pid, "project:manage")
    if not actor.human or not service.session_data(actor).get("user", {}).get("instance_admin"):
        raise DomainError("VKS_FORBIDDEN", "模型密钥由本机实例管理员配置", 403)


def credential_status(service, actor, pid):
    require_admin(service, actor, pid)
    return {
        "configured": bool(os.getenv(ENV_NAME)),
        "env_name": ENV_NAME,
        "storage_path": str(credential_path(service)),
        "storage_kind": "local_file",
    }


def save_credential(service, actor, pid, secret: SecretStr, confirmed: bool):
    require_admin(service, actor, pid)
    if not confirmed:
        raise DomainError("VKS_INVALID_INPUT", "请确认页面显示的本机保存位置", 422)
    value = secret.get_secret_value().strip()
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise DomainError("VKS_INVALID_INPUT", "请输入有效的 API Key", 422)
    with STORE_LOCK:
        data = _read_store(service)
        data[ENV_NAME] = value
        _write_store(service, data)
    os.environ[ENV_NAME] = value
    with service.db() as session:
        service.audit(session, actor, pid, "model.credential.save", pid)
    return credential_status(service, actor, pid)
