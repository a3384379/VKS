"""Wire the local ChatGPT connection into explicitly selected Studio requests."""

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from . import connector_source  # Register the shared connector source in editable checkouts.
from .service import DomainError
from vks_connect.chatgpt_plan import PlanError, PlanSession
from vks_connect.config import is_loopback

CURRENT_PLAN = ContextVar("vks_chatgpt_plan", default=None)


@lru_cache(maxsize=2)
def runtime(root):
    return PlanSession(Path(root) if root else None)


def local_session(service, request, actor):
    return (
        os.getenv("VKS_CHATGPT_LOCAL") == "1"
        and sys.platform in {"darwin", "win32"} and service.personal_mode
        and actor.human and bool(actor.csrf_token)
        and request.client and is_loopback(request.client.host)
        and service.studio_base_url and is_loopback(urlsplit(service.studio_base_url).hostname or "")
        and request.headers.get("origin") == service.studio_base_url.rstrip("/")
    )


def selection_key(service, actor, project):
    from .service import sha

    return "studio:" + sha(f"{service.data_dir.resolve()}\n{actor.id}\n{project}")


def domain_error(error):
    # Upstream OAuth errors must not invalidate the independent VKS session.
    return DomainError(error.code, str(error), 409 if error.status == 401 else error.status)


@contextmanager
def studio_plan(service, request, actor, project):
    if request.headers.get("x-vks-model-access") != "chatgpt-plan":
        yield
        return
    if not local_session(service, request, actor):
        raise DomainError("VKS_FORBIDDEN", "ChatGPT 订阅仅供本机 Studio 用户使用", 403)
    with service.db() as db:
        service.require(db, actor, project, "knowledge:read")
    session = runtime(os.getenv("VKS_CONNECT_CONFIG_DIR"))
    key = selection_key(service, actor, project)
    selection = session.setting(key)
    status = session.status()
    if not selection.get("model") or not status["connected"] or selection.get("profile_id") != status["profile_id"]:
        raise DomainError("VKS_CHATGPT_RECONNECT", "请在 AI 设置选择并启用当前 ChatGPT 连接", 409)
    reset = CURRENT_PLAN.set((actor.id, project, selection, key))
    try:
        yield
    finally:
        CURRENT_PLAN.reset(reset)


def current_selection(actor, project):
    context = CURRENT_PLAN.get()
    if not context or context[:2] != (actor.id, project):
        return None
    selection, key = context[2:]
    session = runtime(os.getenv("VKS_CONNECT_CONFIG_DIR"))
    status = session.status()
    if session.setting(key) != selection or not status["connected"] or status["profile_id"] != selection["profile_id"]:
        raise DomainError("VKS_CHATGPT_RECONNECT", "ChatGPT 连接或项目设置已变化，请重新发起", 409)
    return selection


@lru_cache(maxsize=4)
def saved_vector_index(path, object_key, release):
    from .readonly_export import decode_envelope, digest, section_inputs, LibraryVectors, PROFILE, MODEL, DIMENSIONS
    from vks_connect.library import MAX_FILE_BYTES

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        return None
    with source.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES or digest(raw) != object_key:
        return None
    _, payload = decode_envelope(raw)
    index = payload.get("local_rag", {})
    if payload["snapshot"]["id"] != release or (
        index.get("profile"), index.get("model"), index.get("dimensions")
    ) != (PROFILE, MODEL, DIMENSIONS):
        return None
    return LibraryVectors.read(index, section_inputs(payload))


def rerank_saved_chapters(service, actor, project, release, query, chapters, lexical):
    """Query an existing authorized snapshot index or preserve the keyword results."""
    import re

    from .readonly_export import ROOT, local_export_objects
    from vks_connect.local_embeddings import LocalModelError, encoder

    if not release or not query.strip():
        return lexical
    allowed = {chapter["revision_id"]: chapter for chapter in chapters}
    with service.db() as db:
        exports = local_export_objects(db, project, actor.id, release, exact=True)
    for _, object_key in exports:
        if not isinstance(object_key, str) or not re.fullmatch("[a-f0-9]{64}", object_key):
            continue
        path = service.data_dir / "objects" / object_key
        try:
            index = saved_vector_index(str(path), object_key, release)
            if index is None:
                continue
            model = Path(os.getenv("VKS_LOCAL_EMBEDDING_MODEL_DIR") or ROOT / "output/vks-connect/local-model")
            semantic = index.rank(encoder(model.resolve()).query(query), 16, eligible=allowed)
        except (OSError, ValueError, KeyError, TypeError, LocalModelError):
            continue
        if not semantic:
            continue
        score = {}
        selected = {row["revision_id"]: row for row in lexical}
        for hits in ([row["revision_id"] for row in lexical], semantic):
            for rank, identity in enumerate(hits, 1):
                score[identity] = score.get(identity, 0.0) + 1.0 / (60 + rank)
        ranked = sorted(score, key=lambda identity: (-score[identity], identity))[:12]
        return [dict(selected.get(identity) or allowed[identity]) for identity in ranked]
    return lexical


def install_chatgpt_plan(app, service, actor, envelope):
    @app.post("/api/v3/projects/{pid}/chatgpt-plan")
    def manage(pid: str, form: PlanInput, request: Request, user=Depends(actor)):
        if not local_session(service, request, user):
            return envelope(request, {"available": False})
        with service.db() as s:
            service.require(s, user, pid, "project:manage")
        session = runtime(os.getenv("VKS_CONNECT_CONFIG_DIR"))
        key = selection_key(service, user, pid)
        owner = selection_key(service, user, "")
        try:
            # The person who connects the local Studio owns its subscriptions
            # across projects. Other Studio users cannot select that account.
            if form.action == "connect":
                assigned = session.claim_studio(owner)
                if not assigned:
                    raise DomainError("VKS_FORBIDDEN", "本机 Studio 已由另一个用户绑定 ChatGPT", 403)
                session.begin()
            owns = session.setting("studio-owner").get("owner") == owner
            if form.action in {"models", "enable", "disconnect"} and not owns:
                raise DomainError("VKS_FORBIDDEN", "请先连接本机 ChatGPT", 403)
            if form.action == "disconnect":
                session.disconnect()
            if form.action == "disable":
                session.setting(key, {})
            status = session.status() if owns else {
                "connected": False, "account": "", "profile_id": "", "pending": False, "message": "",
            }
            models = (
                session.models(status["profile_id"])
                if form.action in {"models", "enable"} and status["connected"] else []
            )
            if form.action == "enable":
                if not form.allow_external or form.model not in {m["id"] for m in models}:
                    raise DomainError("VKS_INVALID_INPUT", "请选择可用模型并允许发送本项目相关片段", 422)
                session.setting(key, {"profile_id": status["profile_id"], "model": form.model})
        except PlanError as exc:
            raise domain_error(exc) from exc
        selection = session.setting(key)
        active = owns and status["connected"] and selection.get("profile_id") == status["profile_id"]
        return envelope(request, {
            "available": True, "connected": status["connected"], "account": status["account"],
            "pending": status["pending"], "message": status["message"],
            "enabled": bool(active), "model": selection.get("model", "") if active else "", "models": models,
        })


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["status", "connect", "models", "enable", "disable", "disconnect"]
    model: str = Field(default="", max_length=160)
    allow_external: bool = False
