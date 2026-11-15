"""Local team identities and project grants shared by Web and MCP services.

This is local password authentication, not an OAuth/SSO implementation. Passwords
and session/token plaintext never enter identity DTOs or audit metadata.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Integer, JSON, String, select, text, update
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Credential, Identified, Member, Project, now, uid

SENSITIVE_PERMISSIONS = {"source:read", "trace:read", "capture:run"}
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1
_DUMMY_SALT = b"vks-login-dummy!"
_DUMMY_KEY = hashlib.scrypt(b"not-a-user-password", salt=_DUMMY_SALT, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)


class UserIdentity(Identified, Base):
    __tablename__ = "user_identity"
    username: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": row_version}


class MemberGrant(Base):
    __tablename__ = "project_member_grant"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_identity.id"), primary_key=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class MachineOwner(Base):
    __tablename__ = "machine_credential_owner"
    credential_id: Mapped[str] = mapped_column(ForeignKey("credential.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_identity.id"), index=True)


class LoginThrottle(Base):
    __tablename__ = "identity_login_throttle"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


def _error(code, message, status=422, details=None):
    from .service import DomainError

    return DomainError(code, message, status, details)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _future(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _password_valid(password):
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise _error("VKS_INVALID_INPUT", "密码需为 12–1024 个字符")


def hash_password(password):
    _password_valid(password)
    return _encode_password(password)


def _encode_password(password):
    # The requested initial owner/owner credential is provisioned only when
    # creating the instance owner. User creation/reset keeps its own policy.
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def verify_password(password, stored):
    # A missing/disabled account still pays the same KDF cost before a generic
    # authentication failure. KDF parameters are fixed, never accepted from input.
    salt, expected = _DUMMY_SALT, _DUMMY_KEY
    valid_format = False
    if stored:
        try:
            algorithm, n, r, p, salt64, key64 = stored.split("$")
            if (algorithm, n, r, p) == ("scrypt", "16384", "8", "1"):
                salt = base64.b64decode(salt64, validate=True)
                expected = base64.b64decode(key64, validate=True)
                valid_format = len(salt) == 16 and len(expected) == 64
        except (ValueError, TypeError):
            pass
    bounded = password if isinstance(password, str) and len(password) <= PASSWORD_MAX_LENGTH else ""
    actual = hashlib.scrypt(bounded.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return secrets.compare_digest(actual, expected) and valid_format


def ensure_owner(service, session):
    owner = session.get(UserIdentity, service.owner_id)
    if owner:
        return owner
    if service.engine.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(service.owner_id.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    owner = session.get(UserIdentity, service.owner_id)
    if not owner:
        owner = UserIdentity(
            id=service.owner_id,
            username="owner",
            display_name="本地管理员",
            password_hash=_encode_password("owner"),
            status="active",
            row_version=1,
        )
        session.add(owner)
        session.flush()
        # Before team identities existed, only the persisted bootstrap owner
        # could issue tokens. Bind those historical credentials once so later
        # owner password resets and project revocations also invalidate them.
        legacy_ids = session.scalars(
            select(Credential.id).where(
                Credential.kind == "token", ~Credential.id.in_(select(MachineOwner.credential_id))
            )
        )
        for credential_id in legacy_ids:
            session.add(MachineOwner(credential_id=credential_id, user_id=owner.id))
        session.flush()
    return owner


def member_permissions(service, session, member):
    from .service import ALL_SCOPES, ROLE_SCOPES

    if not member or member.status != "active":
        return set()
    if member.role == "admin":
        return set(ALL_SCOPES)
    permissions = set(ROLE_SCOPES.get(member.role, set())) - SENSITIVE_PERMISSIONS
    grant = session.get(MemberGrant, (member.project_id, member.user_id))
    if grant:
        permissions |= set(grant.permissions) & SENSITIVE_PERMISSIONS
    return permissions


def human_scopes(service, session, identity):
    from .service import ALL_SCOPES, READ

    if identity.id == service.owner_id:
        return set(ALL_SCOPES)
    scopes = {READ}
    for member in session.scalars(select(Member).where(Member.user_id == identity.id, Member.status == "active")):
        scopes |= member_permissions(service, session, member)
    return scopes


def _active_identity(service, session, identity_id):
    ensure_owner(service, session)
    identity = session.get(UserIdentity, identity_id)
    if not identity or identity.status != "active":
        raise _error("VKS_UNAUTHENTICATED", "会话或令牌已失效", 401)
    return identity


def _issue_session(service, session, identity):
    from .service import Actor

    secret, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
    scopes = human_scopes(service, session, identity)
    credential = Credential(
        id=uid(),
        token_hash=_digest(secret),
        kind="session",
        actor_id=identity.id,
        name=identity.display_name,
        scopes=sorted(scopes),
        csrf_token=csrf,
        expires_at=_future(43200),
    )
    session.add(credential)
    service.audit(session, identity.id, None, "session.create", credential.id)
    return secret, Actor(identity.id, frozenset(scopes), True, None, credential.id, csrf)


def bootstrap_login(service, token):
    if not isinstance(token, str) or not secrets.compare_digest(token, service.bootstrap_token):
        raise _error("VKS_UNAUTHENTICATED", "本地访问密钥不正确", 401)
    with service.db() as session:
        return _issue_session(service, session, ensure_owner(service, session))


def password_login(service, username, password):
    username = username.strip().casefold()
    error = None
    result = None
    with service.db() as session:
        ensure_owner(service, session)
        key = _digest("local-user:" + username)
        if service.engine.dialect.name == "postgresql":
            lock_key = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        throttle = session.get(LoginThrottle, key)
        identity = session.scalar(select(UserIdentity).where(UserIdentity.username == username))
        correct = verify_password(password, identity.password_hash if identity else None)
        blocked = throttle and throttle.blocked_until and throttle.blocked_until > now()
        if blocked:
            error = _error("VKS_RATE_LIMITED", "登录失败次数较多，请稍后重试", 429)
        elif not correct or not identity or identity.status != "active":
            if not throttle:
                throttle = LoginThrottle(key=key, failures=0, updated_at=now())
                session.add(throttle)
            if throttle.blocked_until and throttle.blocked_until <= now():
                throttle.failures = 0
            throttle.failures += 1
            throttle.updated_at = now()
            if throttle.failures >= 5:
                throttle.blocked_until = _future(60)
            # The audit has no username, password, hash or project name.
            service.audit(session, "00000000-0000-4000-8000-000000000000", None, "session.denied", uid(), "denied")
            error = _error("VKS_UNAUTHENTICATED", "用户名或密码不正确", 401)
        else:
            if throttle:
                session.delete(throttle)
            result = _issue_session(service, session, identity)
    if error:
        raise error
    return result


def authenticate(service, secret, kind=None):
    from .reading import ReadingService

    if not secret:
        raise _error("VKS_UNAUTHENTICATED", "请先登录或提供项目访问令牌", 401)
    # Credential checks are reads. Waiting for the mutation lock here made
    # every GET queue behind indexing/association writes despite read-only APIs.
    with ReadingService(service).db() as session:
        if session.get(UserIdentity, service.owner_id):
            return _authenticate_credential(service, session, secret, kind)
    # A pre-team database may still need its one-time owner/token migration.
    with service.db() as session:
        ensure_owner(service, session)
    with ReadingService(service).db() as session:
        return _authenticate_credential(service, session, secret, kind)


def _authenticate_credential(service, session, secret, kind):
    from .service import Actor

    credential = session.scalar(select(Credential).where(Credential.token_hash == _digest(secret)))
    if not credential or credential.revoked or credential.expires_at <= now() or (kind and credential.kind != kind):
        raise _error("VKS_UNAUTHENTICATED", "会话或令牌已失效", 401)
    if credential.kind == "session":
        identity = _active_identity(service, session, credential.actor_id)
        scopes = human_scopes(service, session, identity)
    else:
        owner = session.get(MachineOwner, credential.id)
        # All pre-team tokens were issued by the persisted bootstrap owner.
        owner_id = owner.user_id if owner else service.owner_id
        _active_identity(service, session, owner_id)
        member = session.get(Member, (credential.project_id, owner_id))
        grants = member_permissions(service, session, member)
        if not grants:
            raise _error("VKS_UNAUTHENTICATED", "会话或令牌已失效", 401)
        scopes = set(credential.scopes) & grants
    return Actor(
        credential.actor_id,
        frozenset(scopes),
        credential.kind == "session",
        credential.project_id,
        credential.id,
        credential.csrf_token,
    )


def authorize_project(service, session, actor, project_id, scope):
    if actor.credential_id and actor.credential_id.startswith("oauth:"):
        from .oauth import authorize_project as oauth_authorize

        return oauth_authorize(service, session, actor, project_id, scope)
    readonly = bool(session.info.get("vks_read_only"))
    cache_key = (actor.id, actor.credential_id, actor.human, project_id, actor.scopes)
    cached = session.info.get("vks_auth_grants") if readonly else None
    if cached and cached[0] is session.get_transaction() and cache_key in cached[1]:
        project, grants = cached[1][cache_key]
        if scope not in actor.scopes or scope not in grants:
            raise _error("VKS_FORBIDDEN", f"需要 {scope} 权限", 403)
        return project
    query = select(Project).where(Project.id == project_id)
    if not readonly:
        query = query.with_for_update()
    project = session.scalar(query)
    if not project:
        raise _error("VKS_NOT_FOUND", "未找到对象", 404)
    if actor.human:
        _active_identity(service, session, actor.id)
        member = session.get(Member, (project_id, actor.id))
        grants = member_permissions(service, session, member)
    else:
        if actor.project_id != project_id:
            raise _error("VKS_NOT_FOUND", "未找到对象", 404)
        credential = session.get(Credential, actor.credential_id) if actor.credential_id else None
        if not credential or credential.revoked or credential.expires_at <= now():
            raise _error("VKS_UNAUTHENTICATED", "会话或令牌已失效", 401)
        owner = session.get(MachineOwner, credential.id)
        owner_id = owner.user_id if owner else service.owner_id
        _active_identity(service, session, owner_id)
        grants = member_permissions(service, session, session.get(Member, (project_id, owner_id)))
        grants &= set(credential.scopes)
    if not grants:
        raise _error("VKS_NOT_FOUND", "未找到对象", 404)
    if readonly:
        transaction = session.get_transaction()
        if not cached or cached[0] is not transaction:
            cached = (transaction, {})
            session.info["vks_auth_grants"] = cached
        cached[1][cache_key] = (project, frozenset(grants))
    if scope not in actor.scopes or scope not in grants:
        raise _error("VKS_FORBIDDEN", f"需要 {scope} 权限", 403)
    return project


def user_dto(service, identity):
    return {
        "id": identity.id,
        "username": identity.username,
        "display_name": identity.display_name,
        "status": identity.status,
        "row_version": identity.row_version,
        "instance_admin": identity.id == service.owner_id,
        "password_configured": bool(identity.password_hash),
    }


def session_data(service, actor):
    with service.db() as session:
        if actor.human:
            identity = _active_identity(service, session, actor.id)
            profile = user_dto(service, identity)
            permissions = {
                m.project_id: sorted(member_permissions(service, session, m))
                for m in session.scalars(select(Member).where(Member.user_id == actor.id, Member.status == "active"))
            }
        else:
            profile, permissions = {"display_name": "MCP 客户端", "instance_admin": False}, {}
    return {
        "authenticated": True,
        "actor_id": actor.id,
        "name": profile["display_name"],
        "user": profile,
        "human": actor.human,
        "scopes": sorted(actor.scopes),
        "csrf_token": actor.csrf_token,
        "auth_mode": "oauth-resource-server"
        if actor.credential_id and actor.credential_id.startswith("oauth:")
        else "local-team",
        "project_permissions": permissions,
        "capabilities": {
            "live_capture": True,
            "platform_llm": True,
            "remote_connectors": False,
            "client_collectors": True,
        },
    }


def require_instance_owner(service, session, actor):
    if not actor.human or actor.id != service.owner_id:
        raise _error("VKS_FORBIDDEN", "需要实例管理员身份", 403)
    return _active_identity(service, session, actor.id)


def create_user(service, actor, payload):
    username = payload["username"].strip().casefold()
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,79}", username):
        raise _error("VKS_INVALID_INPUT", "用户名仅允许小写字母、数字、点、下划线和短横线")
    encoded = hash_password(payload["password"])
    with service.db() as session:
        require_instance_owner(service, session, actor)
        if session.scalar(select(UserIdentity).where(UserIdentity.username == username)):
            raise _error("VKS_STATE_CONFLICT", "用户名已存在", 409)
        identity = UserIdentity(
            id=uid(),
            username=username,
            display_name=payload["display_name"].strip(),
            password_hash=encoded,
            status="active",
            row_version=1,
        )
        session.add(identity)
        service.audit(session, actor, None, "identity.create", identity.id)
        return user_dto(service, identity)


def list_users(service, actor):
    with service.db() as session:
        require_instance_owner(service, session, actor)
        return [
            user_dto(service, user) for user in session.scalars(select(UserIdentity).order_by(UserIdentity.username))
        ]


def _revoke_user_credentials(session, user_id, project_id=None):
    owned = select(MachineOwner.credential_id).where(MachineOwner.user_id == user_id)
    conditions = [Credential.actor_id == user_id] if project_id is None else []
    token_conditions = [Credential.id.in_(owned)]
    if project_id:
        token_conditions.append(Credential.project_id == project_id)
    session.execute(update(Credential).where(*token_conditions).values(revoked=True))
    if conditions:
        session.execute(update(Credential).where(*conditions).values(revoked=True))


def _prevent_last_admin_disable(session, user_id):
    for member in session.scalars(
        select(Member).where(Member.user_id == user_id, Member.role == "admin", Member.status == "active")
    ):
        # Lock project first, so simultaneous removals/identity disables serialize.
        session.scalar(select(Project).where(Project.id == member.project_id).with_for_update())
        other = session.scalar(
            select(Member).where(
                Member.project_id == member.project_id,
                Member.role == "admin",
                Member.status == "active",
                Member.user_id != user_id,
            )
        )
        if not other:
            raise _error("VKS_STATE_CONFLICT", "该用户是项目最后一位管理员，请先安排其他管理员", 409)


def update_user(service, actor, user_id, payload, version):
    with service.db() as session:
        require_instance_owner(service, session, actor)
        identity = session.scalar(select(UserIdentity).where(UserIdentity.id == user_id).with_for_update())
        if not identity:
            raise _error("VKS_NOT_FOUND", "未找到用户", 404)
        service._version(identity.row_version, version)
        if payload.get("status") == "disabled":
            if identity.id == service.owner_id:
                raise _error("VKS_STATE_CONFLICT", "不能停用实例管理员", 409)
            _prevent_last_admin_disable(session, user_id)
            _revoke_user_credentials(session, user_id)
            for member in session.scalars(select(Member).where(Member.user_id == user_id)):
                member.status = "disabled"
        if "display_name" in payload:
            identity.display_name = payload["display_name"].strip()
        if "status" in payload:
            identity.status = payload["status"]
        identity.row_version += 1
        service.audit(session, actor, None, "identity.update", user_id)
        return user_dto(service, identity)


def reset_password(service, actor, user_id, password):
    encoded = hash_password(password)
    with service.db() as session:
        require_instance_owner(service, session, actor)
        identity = session.scalar(select(UserIdentity).where(UserIdentity.id == user_id).with_for_update())
        if not identity:
            raise _error("VKS_NOT_FOUND", "未找到用户", 404)
        identity.password_hash = encoded
        identity.row_version += 1
        _revoke_user_credentials(session, user_id)
        service.audit(session, actor, None, "identity.password_reset", user_id)
        return {"user_id": user_id, "sessions_revoked": True}


def change_password(service, actor, current_password, new_password):
    _password_valid(new_password)
    with service.db() as session:
        if not actor.human:
            raise _error("VKS_FORBIDDEN", "需要人员会话", 403)
        identity = _active_identity(service, session, actor.id)
        if not verify_password(current_password, identity.password_hash):
            raise _error("VKS_UNAUTHENTICATED", "当前密码不正确", 401)
        identity.password_hash = hash_password(new_password)
        identity.row_version += 1
        _revoke_user_credentials(session, actor.id)
        service.audit(session, actor, None, "identity.password_change", actor.id)
    return {"sessions_revoked": True}


def _member_dtos(service, session, project_id):
    result = []
    for member in session.scalars(select(Member).where(Member.project_id == project_id)):
        identity = session.get(UserIdentity, member.user_id)
        result.append(
            {
                "user_id": member.user_id,
                "role": member.role,
                "status": member.status,
                "username": identity.username if identity else "",
                "display_name": identity.display_name if identity else "",
                "permissions": sorted(member_permissions(service, session, member) & SENSITIVE_PERMISSIONS),
            }
        )
    return result


def list_members(service, actor, project_id):
    with service.db() as session:
        service.require(session, actor, project_id, "project:manage")
        return _member_dtos(service, session, project_id)


def set_member(service, actor, project_id, payload, version):
    with service.db() as session:
        project = service.require(session, actor, project_id, "project:manage")
        service._version(project.row_version, version)
        caller = session.get(Member, (project_id, actor.id))
        target = _active_identity(service, session, payload["user_id"])
        member = session.get(Member, (project_id, target.id))
        if caller.role != "admin" and (payload["role"] == "admin" or (member and member.role == "admin")):
            raise _error("VKS_FORBIDDEN", "只有项目管理员可以管理管理员角色", 403)
        if member and member.role == "admin" and (payload["role"] != "admin" or payload["status"] != "active"):
            admins = list(
                session.scalars(
                    select(Member).where(
                        Member.project_id == project_id, Member.role == "admin", Member.status == "active"
                    )
                )
            )
            if len(admins) <= 1:
                raise _error("VKS_STATE_CONFLICT", "不能移除最后一位管理员", 409)
        if not member:
            member = Member(project_id=project_id, user_id=target.id, role=payload["role"], status=payload["status"])
            session.add(member)
        else:
            changed = member.role != payload["role"] or member.status != payload["status"]
            member.role, member.status = payload["role"], payload["status"]
            if changed:
                _revoke_user_credentials(session, target.id, project_id)
                if payload["status"] == "disabled":
                    session.execute(
                        update(Credential)
                        .where(Credential.actor_id == target.id, Credential.kind == "session")
                        .values(revoked=True)
                    )
        if payload["role"] == "viewer":
            grant = session.get(MemberGrant, (project_id, target.id))
            if grant and "capture:run" in grant.permissions:
                grant.permissions = [p for p in grant.permissions if p != "capture:run"]
        project.row_version += 1
        service.audit(session, actor, project_id, "member.update", target.id)
        session.flush()
        return _member_dtos(service, session, project_id)


def list_member_permissions(service, actor, project_id):
    with service.db() as session:
        service.require(session, actor, project_id, "project:manage")
        project = session.get(Project, project_id)
        return {"row_version": project.row_version, "members": _member_dtos(service, session, project_id)}


def set_member_permissions(service, actor, project_id, payload, version):
    permissions = set(payload["permissions"])
    if permissions - SENSITIVE_PERMISSIONS:
        raise _error("VKS_INVALID_INPUT", "只接受独立源码、Trace 与录制授权")
    with service.db() as session:
        project = service.require(session, actor, project_id, "project:manage")
        caller = session.get(Member, (project_id, actor.id))
        if caller.role != "admin":
            raise _error("VKS_FORBIDDEN", "只有项目管理员可以配置敏感权限", 403)
        service._version(project.row_version, version)
        member = session.get(Member, (project_id, payload["user_id"]))
        if not member or member.status != "active":
            raise _error("VKS_NOT_FOUND", "未找到有效项目成员", 404)
        if member.role == "viewer" and "capture:run" in permissions:
            raise _error("VKS_FORBIDDEN", "阅读者不能获准执行录制", 403)
        grant = session.get(MemberGrant, (project_id, member.user_id))
        if not grant:
            grant = MemberGrant(project_id=project_id, user_id=member.user_id, permissions=sorted(permissions))
            session.add(grant)
        else:
            grant.permissions = sorted(permissions)
        _revoke_user_credentials(session, member.user_id, project_id)
        project.row_version += 1
        service.audit(session, actor, project_id, "member.permissions", member.user_id)
        return {"user_id": member.user_id, "permissions": sorted(permissions), "row_version": project.row_version}


def create_token(service, actor, project_id, payload):
    from .service import MACHINE_SCOPES, READ

    with service.db() as session:
        service.require(session, actor, project_id, "project:manage")
        if not actor.human:
            raise _error("VKS_FORBIDDEN", "需要人员管理员会话", 403)
        grants = member_permissions(service, session, session.get(Member, (project_id, actor.id)))
        scopes = set(payload["scopes"])
        if scopes - MACHINE_SCOPES or scopes - grants:
            raise _error("VKS_FORBIDDEN", "令牌不能获得管理员或超出签发人权限的 scope", 403)
        if READ not in scopes:
            raise _error("VKS_INVALID_INPUT", "令牌必须包含 knowledge:read")
        secret = "vks_" + secrets.token_urlsafe(36)
        credential = Credential(
            id=uid(),
            token_hash=_digest(secret),
            kind="token",
            actor_id=uid(),
            project_id=project_id,
            name=payload["name"],
            scopes=sorted(scopes),
            csrf_token="",
            expires_at=_future(payload.get("expires_in_days", 30) * 86400),
        )
        session.add(credential)
        session.flush()
        session.add(MachineOwner(credential_id=credential.id, user_id=actor.id))
        service.audit(session, actor, project_id, "token.create", credential.id)
        return {**service.token_dto(credential), "token": secret}


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordLogin(StrictInput):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class CreateUser(StrictInput):
    username: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,79}$")
    display_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=12, max_length=1024)


class UpdateUser(StrictInput):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class PasswordReset(StrictInput):
    new_password: str = Field(min_length=12, max_length=1024)


class PasswordChange(PasswordReset):
    current_password: str = Field(min_length=1, max_length=1024)


class ProfileUpdate(StrictInput):
    display_name: str = Field(min_length=1, max_length=160)


class MemberPermissions(StrictInput):
    user_id: str
    permissions: list[str] = Field(default_factory=list, max_length=3)


def install_identity(app, service, actor, envelope, API="/api/v1"):
    @app.post(API + "/auth/login")
    def login(payload: PasswordLogin, request: Request):
        token, identity_actor = password_login(service, payload.username, payload.password)
        response = JSONResponse(envelope(request, service.session_data(identity_actor)))
        response.set_cookie(
            "vks_session",
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=43200,
            path="/",
        )
        return response

    @app.get(API + "/users")
    def users(request: Request, identity_actor=Depends(actor)):
        return envelope(request, list_users(service, identity_actor))

    @app.post(API + "/users", status_code=201)
    def add_user(payload: CreateUser, request: Request, identity_actor=Depends(actor)):
        return envelope(request, create_user(service, identity_actor, payload.model_dump()))

    @app.patch(API + "/users/{user_id}")
    def edit_user(
        user_id: str, payload: UpdateUser, request: Request, identity_actor=Depends(actor), if_match: str = Header(...)
    ):
        return envelope(
            request, update_user(service, identity_actor, user_id, payload.model_dump(exclude_none=True), if_match)
        )

    @app.post(API + "/users/{user_id}/password-reset")
    def password_reset(user_id: str, payload: PasswordReset, request: Request, identity_actor=Depends(actor)):
        return envelope(request, reset_password(service, identity_actor, user_id, payload.new_password))

    @app.get(API + "/auth/profile")
    def profile(request: Request, identity_actor=Depends(actor)):
        if not identity_actor.human:
            raise _error("VKS_FORBIDDEN", "需要人员会话", 403)
        with service.db() as session:
            return envelope(request, user_dto(service, _active_identity(service, session, identity_actor.id)))

    @app.patch(API + "/auth/profile")
    def edit_profile(
        payload: ProfileUpdate, request: Request, identity_actor=Depends(actor), if_match: str = Header(...)
    ):
        if not identity_actor.human:
            raise _error("VKS_FORBIDDEN", "需要人员会话", 403)
        with service.db() as session:
            identity = _active_identity(service, session, identity_actor.id)
            service._version(identity.row_version, if_match)
            identity.display_name = payload.display_name.strip()
            identity.row_version += 1
            service.audit(session, identity_actor, None, "identity.profile", identity.id)
            return envelope(request, user_dto(service, identity))

    @app.post(API + "/auth/password")
    def own_password(payload: PasswordChange, request: Request, identity_actor=Depends(actor)):
        result = change_password(service, identity_actor, payload.current_password, payload.new_password)
        response = JSONResponse(envelope(request, result))
        response.delete_cookie("vks_session", path="/")
        return response

    @app.get(API + "/projects/{project_id}/member-permissions")
    def permissions(project_id: str, request: Request, identity_actor=Depends(actor)):
        return envelope(request, list_member_permissions(service, identity_actor, project_id))

    @app.put(API + "/projects/{project_id}/member-permissions")
    def set_permissions(
        project_id: str,
        payload: MemberPermissions,
        request: Request,
        identity_actor=Depends(actor),
        if_match: str = Header(...),
    ):
        return envelope(
            request, set_member_permissions(service, identity_actor, project_id, payload.model_dump(), if_match)
        )
