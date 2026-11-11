"""Instance-local model credential storage. Values never enter response DTOs."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import SecretStr

from .service import DomainError

ENV_NAME = "VKS_MODEL_API_KEY"


def credential_path(service):
    return service.data_dir / "model-credentials.json"


def load_credential(service):
    path = credential_path(service)
    if not path.exists():
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        value = json.loads(raw).get(ENV_NAME)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
        ):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise DomainError("VKS_INVALID_INPUT", "本机模型凭据文件无法读取，请重新配置", 503) from None
    os.environ[ENV_NAME] = value


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
    path = credential_path(service)
    if path.is_symlink():
        raise DomainError("VKS_INVALID_INPUT", "凭据文件不能是符号链接", 422)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".model-key-", dir=service.data_dir, delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump({ENV_NAME: value}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "无法保存本机模型凭据", 503) from None
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    os.environ[ENV_NAME] = value
    with service.db() as session:
        service.audit(session, actor, pid, "model.credential.save", pid)
    return credential_status(service, actor, pid)
