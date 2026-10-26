"""OAuth resource-server validation for registered external authorization servers.

Trust is configured by the instance owner. JWT headers never choose a remote
key URL; only registered public JWKS, issuer, audience and RS256 are accepted.
Project grants are independent of token scopes and checked on every request.
"""

from __future__ import annotations
import hashlib
import json
import os
import secrets
from urllib.parse import urlsplit
import jwt
from jwt import PyJWK
from fastapi import Depends, Request, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, Integer, JSON, Boolean, ForeignKey, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Identified, Project, Credential, uid


class OAuthIssuer(Identified, Base):
    __tablename__ = "oauth_issuer"
    issuer: Mapped[str] = mapped_column(String(2048), unique=True)
    audience: Mapped[str] = mapped_column(String(2048))
    jwks: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_token_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class OAuthPrincipal(Identified, Base):
    __tablename__ = "oauth_principal"
    __table_args__ = (UniqueConstraint("issuer_id", "subject", "client_id"),)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("oauth_issuer.id"))
    subject: Mapped[str] = mapped_column(String(255))
    client_id: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OAuthGrant(Identified, Base):
    __tablename__ = "client_project_grant"
    __table_args__ = (UniqueConstraint("principal_id", "project_id"),)
    principal_id: Mapped[str] = mapped_column(ForeignKey("oauth_principal.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    scopes: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(36))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


def denied():
    from .service import DomainError

    return DomainError("VKS_UNAUTHENTICATED", "访问令牌无效或未获授权", 401)


def validate_jwks(value):
    from .service import DomainError

    if (
        not isinstance(value, dict)
        or set(value) != {"keys"}
        or not isinstance(value["keys"], list)
        or not 1 <= len(value["keys"]) <= 10
    ):
        raise DomainError("VKS_INVALID_INPUT", "需提供1–10个公开RSA签名密钥")
    seen = set()
    for item in value["keys"]:
        if (
            not isinstance(item, dict)
            or item.get("kty") != "RSA"
            or any(k in item for k in ("d", "p", "q", "dp", "dq", "qi", "oth", "k", "jku", "x5u"))
            or item.get("alg", "RS256") != "RS256"
            or item.get("use", "sig") != "sig"
        ):
            raise DomainError("VKS_INVALID_INPUT", "只允许公开RS256签名密钥")
        kid = item.get("kid")
        if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in seen:
            raise DomainError("VKS_INVALID_INPUT", "kid必须唯一且非空")
        try:
            key = PyJWK.from_dict(item, algorithm="RS256").key
        except Exception:
            raise DomainError("VKS_INVALID_INPUT", "公钥格式无效") from None
        if key.key_size < 2048:
            raise DomainError("VKS_INVALID_INPUT", "RSA公钥至少2048位")
        seen.add(kid)
    return value


def authenticate(service, token):
    from .service import Actor, MACHINE_SCOPES

    if not isinstance(token, str) or len(token) > 16384:
        raise denied()
    try:
        header = jwt.get_unverified_header(token)
        raw = jwt.decode(token, options={"verify_signature": False})
        if header.get("alg") != "RS256" or "jku" in header or "x5u" in header:
            raise denied()
        with service.db() as s:
            issuer = s.scalar(
                select(OAuthIssuer).where(OAuthIssuer.issuer == raw.get("iss"), OAuthIssuer.active == True)
            )
            if not issuer:
                raise denied()
            matches = [k for k in issuer.jwks["keys"] if k.get("kid") == header.get("kid")]
            if len(matches) != 1:
                raise denied()
            key = PyJWK.from_dict(matches[0], algorithm="RS256").key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=issuer.issuer,
                audience=issuer.audience,
                options={"require": ["iss", "aud", "sub", "exp", "iat"], "verify_aud": True},
                leeway=0,
            )
            if (
                not isinstance(claims["exp"], (int, float))
                or not isinstance(claims["iat"], (int, float))
                or not 0 < claims["exp"] - claims["iat"] <= issuer.max_token_seconds
            ):
                raise denied()
            subject = claims["sub"]
            client = claims.get("client_id") or claims.get("azp")
            if (
                not isinstance(subject, str)
                or not 1 <= len(subject) <= 255
                or not isinstance(client, str)
                or not 1 <= len(client) <= 255
            ):
                raise denied()
            if claims.get("client_id") and claims.get("azp") and claims["client_id"] != claims["azp"]:
                raise denied()
            principal = s.scalar(
                select(OAuthPrincipal).where(
                    OAuthPrincipal.issuer_id == issuer.id,
                    OAuthPrincipal.subject == subject,
                    OAuthPrincipal.client_id == client,
                    OAuthPrincipal.active == True,
                )
            )
            if not principal:
                raise denied()
            raw_scopes = claims.get("scope", "")
            if not isinstance(raw_scopes, str):
                raise denied()
            scopes = set(raw_scopes.split()) & MACHINE_SCOPES
            grant_scopes = set()
            for grant in s.scalars(
                select(OAuthGrant).where(OAuthGrant.principal_id == principal.id, OAuthGrant.active == True)
            ):
                grant_scopes |= _granted(service, s, grant)
            scopes &= grant_scopes
            if "knowledge:read" not in scopes:
                raise denied()
            return Actor(principal.id, frozenset(scopes), False, None, "oauth:" + principal.id)
    except Exception as exc:
        from .service import DomainError

        if isinstance(exc, DomainError):
            raise
        raise denied() from None


def _granted(service, s, grant):
    from .identity import UserIdentity, member_permissions
    from .models import Member

    identity = s.get(UserIdentity, grant.created_by)
    if not identity or identity.status != "active":
        return set()
    return set(grant.scopes) & member_permissions(service, s, s.get(Member, (grant.project_id, grant.created_by)))


def authorize_project(service, s, actor, pid, scope):
    from .service import DomainError

    principal = s.get(OAuthPrincipal, actor.id)
    issuer = s.get(OAuthIssuer, principal.issuer_id) if principal else None
    grant = s.scalar(
        select(OAuthGrant).where(
            OAuthGrant.principal_id == actor.id, OAuthGrant.project_id == pid, OAuthGrant.active == True
        )
    )
    query = select(Project).where(Project.id == pid)
    if not s.info.get("vks_read_only"):
        query = query.with_for_update()
    project = s.scalar(query)
    if not principal or not principal.active or not issuer or not issuer.active or not grant or not project:
        raise DomainError("VKS_NOT_FOUND", "未找到对象", 404)
    scopes = _granted(service, s, grant)
    if scope not in scopes or scope not in actor.scopes:
        raise DomainError("VKS_FORBIDDEN", f"需要 {scope} 权限", 403)
    return project


def project_ids(service, s, actor):
    ids = []
    for grant in s.scalars(select(OAuthGrant).where(OAuthGrant.principal_id == actor.id, OAuthGrant.active == True)):
        if "knowledge:read" in _granted(service, s, grant):
            ids.append(grant.project_id)
    return ids


def resource_metadata(service):
    resource = os.getenv("VKS_MCP_RESOURCE", "http://127.0.0.1:8788/mcp")
    with service.db() as s:
        issuers = list(s.scalars(select(OAuthIssuer.issuer).where(OAuthIssuer.active == True)))
    from .service import MACHINE_SCOPES

    return {
        "resource": resource,
        "authorization_servers": issuers,
        "scopes_supported": sorted(MACHINE_SCOPES),
        "bearer_methods_supported": ["header"],
    }


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssuerInput(Input):
    issuer: str = Field(min_length=8, max_length=2048)
    audience: str = Field(min_length=8, max_length=2048)
    jwks: dict
    max_token_seconds: int = Field(default=3600, ge=60, le=86400)


class IssuerUpdate(Input):
    active: bool
    jwks: dict | None = None


class GrantInput(Input):
    issuer_id: str
    subject: str = Field(min_length=1, max_length=255)
    client_id: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(min_length=1, max_length=20)


class GrantUpdate(Input):
    active: bool


def install_oauth(app, service, actor, envelope, API="/api/v1"):
    from .identity import require_instance_owner
    from .service import DomainError, MACHINE_SCOPES

    @app.get(API + "/oauth/resource-metadata")
    def metadata(request: Request):
        return resource_metadata(service)

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def well_known(request: Request):
        return resource_metadata(service)

    @app.get(API + "/oauth/issuers")
    def issuers(request: Request, a=Depends(actor)):
        with service.db() as s:
            require_instance_owner(service, s, a)
            return envelope(
                request,
                [
                    {
                        "id": i.id,
                        "issuer": i.issuer,
                        "audience": i.audience,
                        "active": i.active,
                        "max_token_seconds": i.max_token_seconds,
                        "row_version": i.row_version,
                        "kids": [k["kid"] for k in i.jwks["keys"]],
                    }
                    for i in s.scalars(select(OAuthIssuer))
                ],
            )

    @app.post(API + "/oauth/issuers", status_code=201)
    def add_issuer(payload: IssuerInput, request: Request, a=Depends(actor)):
        for locator in (payload.issuer, payload.audience):
            url = urlsplit(locator)
            if (
                (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")))
                or url.username
                or url.password
                or url.fragment
            ):
                raise DomainError("VKS_INVALID_INPUT", "授权服务和资源地址需HTTPS；本机回环开发除外")
        if payload.audience != os.getenv("VKS_MCP_RESOURCE", "http://127.0.0.1:8788/mcp"):
            raise DomainError("VKS_INVALID_INPUT", "audience必须是本实例登记的MCP资源地址")
        public = validate_jwks(payload.jwks)
        with service.db() as s:
            require_instance_owner(service, s, a)
            i = OAuthIssuer(
                id=uid(),
                issuer=payload.issuer,
                audience=payload.audience,
                jwks=public,
                max_token_seconds=payload.max_token_seconds,
                active=True,
                row_version=1,
            )
            s.add(i)
            s.flush()
            service.audit(s, a, None, "oauth.issuer.create", i.id)
            return envelope(request, {"id": i.id, "issuer": i.issuer, "row_version": i.row_version})

    @app.patch(API + "/oauth/issuers/{iid}")
    def update_issuer(iid: str, payload: IssuerUpdate, request: Request, a=Depends(actor), if_match: str = Header(...)):
        with service.db() as s:
            require_instance_owner(service, s, a)
            i = s.scalar(select(OAuthIssuer).where(OAuthIssuer.id == iid).with_for_update())
            if not i:
                raise DomainError("VKS_NOT_FOUND", "未找到授权服务", 404)
            service._version(i.row_version, if_match)
            if payload.jwks is not None:
                i.jwks = validate_jwks(payload.jwks)
            i.active = payload.active
            i.row_version += 1
            service.audit(s, a, None, "oauth.issuer.update", i.id)
            return envelope(request, {"id": i.id, "active": i.active, "row_version": i.row_version})

    @app.get(API + "/projects/{pid}/oauth-grants")
    def grants(pid: str, request: Request, a=Depends(actor)):
        with service.db() as s:
            service.require(s, a, pid, "project:manage")
            rows = []
            for grant in s.scalars(select(OAuthGrant).where(OAuthGrant.project_id == pid)):
                principal = s.get(OAuthPrincipal, grant.principal_id)
                rows.append(
                    {
                        "id": grant.id,
                        "principal_id": principal.id,
                        "issuer_id": principal.issuer_id,
                        "subject": principal.subject,
                        "client_id": principal.client_id,
                        "scopes": grant.scopes,
                        "active": grant.active,
                        "row_version": grant.row_version,
                    }
                )
            return envelope(request, rows)

    @app.post(API + "/projects/{pid}/oauth-grants", status_code=201)
    def add_grant(pid: str, payload: GrantInput, request: Request, a=Depends(actor)):
        if set(payload.scopes) - MACHINE_SCOPES or "knowledge:read" not in payload.scopes:
            raise DomainError("VKS_INVALID_INPUT", "OAuth客户端不能获得管理员/审核/发布权限")
        with service.db() as s:
            service.require(s, a, pid, "project:manage")
            if not a.human:
                raise DomainError("VKS_FORBIDDEN", "需要人员管理员", 403)
            for scope in payload.scopes:
                service.require(s, a, pid, scope)
            issuer = s.get(OAuthIssuer, payload.issuer_id)
            if not issuer or not issuer.active:
                raise DomainError("VKS_NOT_FOUND", "未找到授权服务", 404)
            principal = s.scalar(
                select(OAuthPrincipal).where(
                    OAuthPrincipal.issuer_id == issuer.id,
                    OAuthPrincipal.subject == payload.subject,
                    OAuthPrincipal.client_id == payload.client_id,
                )
            )
            if not principal:
                principal = OAuthPrincipal(
                    id=uid(), issuer_id=issuer.id, subject=payload.subject, client_id=payload.client_id, active=True
                )
                s.add(principal)
                s.flush()
            grant = OAuthGrant(
                id=uid(),
                principal_id=principal.id,
                project_id=pid,
                scopes=payload.scopes,
                created_by=a.id,
                active=True,
                row_version=1,
            )
            s.add(grant)
            s.flush()
            service.audit(s, a, pid, "oauth.grant.create", grant.id)
            return envelope(request, {"id": grant.id, "principal_id": principal.id, "row_version": 1})

    @app.patch(API + "/projects/{pid}/oauth-grants/{gid}")
    def update_grant(
        pid: str, gid: str, payload: GrantUpdate, request: Request, a=Depends(actor), if_match: str = Header(...)
    ):
        with service.db() as s:
            service.require(s, a, pid, "project:manage")
            g = s.scalar(select(OAuthGrant).where(OAuthGrant.id == gid, OAuthGrant.project_id == pid).with_for_update())
            if not g:
                raise DomainError("VKS_NOT_FOUND", "未找到授权", 404)
            service._version(g.row_version, if_match)
            g.active = payload.active
            g.row_version += 1
            service.audit(s, a, pid, "oauth.grant.update", gid)
            return envelope(request, {"id": g.id, "active": g.active, "row_version": g.row_version})


def actor_for_job(service, s, actor_id, pid, authorization=None):
    from .service import Actor

    principal = s.get(OAuthPrincipal, actor_id)
    issuer = s.get(OAuthIssuer, principal.issuer_id) if principal else None
    grant = s.scalar(
        select(OAuthGrant).where(
            OAuthGrant.principal_id == actor_id, OAuthGrant.project_id == pid, OAuthGrant.active == True
        )
    )
    if not principal or not principal.active or not issuer or not issuer.active or not grant or not authorization:
        raise denied()
    scopes = _granted(service, s, grant) & set(authorization["scopes"])
    return Actor(actor_id, frozenset(scopes), False, None, "oauth:" + actor_id)
