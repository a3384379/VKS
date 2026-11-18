from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, quote
from uuid import uuid4
import json
import os
import secrets
from fastapi import FastAPI, Request, Depends, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from sqlalchemy import text
from .service import Service, DomainError, Actor
from .version import VERSION
from .schemas import *

API = "/api/v1"
MAX_UPLOAD = 128 * 1024 * 1024


def create_app(data_dir=None, database_url=None, enable_worker=True):
    service = Service(data_dir, database_url)

    @asynccontextmanager
    async def lifespan(app):
        if enable_worker:
            service.start_worker()
        yield
        service.stop_worker()

    app = FastAPI(title="知境 VKS · Project Knowledge API", version=VERSION, lifespan=lifespan)
    app.state.service = service

    def envelope(request, data, **meta):
        return {"data": data, "meta": {"request_id": getattr(request.state, "request_id", str(uuid4())), **meta}}

    def error(request, e):
        return JSONResponse(
            {
                "error": {"code": e.code, "message": e.message, "retryable": e.retryable, "details": e.details},
                "meta": {"request_id": getattr(request.state, "request_id", str(uuid4()))},
            },
            status_code=e.status,
        )

    @app.middleware("http")
    async def guard(request, call_next):
        request.state.request_id = str(uuid4())
        allowed = os.getenv("VKS_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
        host = request.url.hostname or ""
        if host not in allowed:
            return error(request, DomainError("VKS_FORBIDDEN", "Host 未登记", 403))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc != request.headers.get("host") or parsed.scheme not in ("http", "https"):
                    return error(request, DomainError("VKS_FORBIDDEN", "跨来源写入被拒绝", 403))
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > MAX_UPLOAD):
                return error(request, DomainError("VKS_LIMIT_EXCEEDED", "请求超过 128 MiB 上限", 413))
        try:
            r = await call_next(request)
        except DomainError as e:
            r = error(request, e)
        r.headers["X-Request-ID"] = request.state.request_id
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Referrer-Policy"] = "no-referrer"
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.exception_handler(DomainError)
    async def domain_error(request, e):
        return error(request, e)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, e):
        return error(
            request,
            DomainError(
                "VKS_INVALID_INPUT",
                "请求字段不符合契约",
                422,
                {"fields": [{"location": list(x["loc"]), "reason": x["type"]} for x in e.errors()]},
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, e):
        return error(
            request,
            DomainError(
                "VKS_NOT_FOUND" if e.status_code == 404 else "VKS_INVALID_INPUT",
                "未找到入口" if e.status_code == 404 else str(e.detail),
                e.status_code,
            ),
        )

    def actor(request: Request):
        auth = request.headers.get("authorization", "")
        if auth:
            if not auth.startswith("Bearer "):
                raise DomainError("VKS_UNAUTHENTICATED", "需要 Bearer 访问令牌", 401)
            a = service.authenticate(auth[7:], "token")
        else:
            a = service.authenticate(request.cookies.get("vks_session"), "session")
            if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), a.csrf_token
            ):
                raise DomainError("VKS_FORBIDDEN", "CSRF 校验失败，请重新登录", 403)
        return a

    @app.get("/api/v3/document-templates/handbook/download", response_class=HTMLResponse)
    def download_handbook_template(a: Actor = Depends(actor)):
        from .handbook_authoring import template_html

        return HTMLResponse(template_html(), headers={
            "Content-Disposition": 'attachment; filename="vks-handbook-template.html"',
        })

    @app.get("/api/v3/document-templates/handbook/assistant", response_class=HTMLResponse)
    def document_assistant(a: Actor = Depends(actor)):
        from .document_assistant import assistant_html

        return HTMLResponse(assistant_html(), headers={"Cache-Control": "private, no-store"})

    @app.post("/api/v2/projects/{pid}/snapshots", status_code=201)
    def save_snapshot(
        pid: str,
        payload: SnapshotInput,
        request: Request,
        a: Actor = Depends(actor),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, service.save_snapshot(a, pid, payload.model_dump(), idempotency_key))

    @app.get("/live")
    @app.get(API + "/live")
    def live():
        return {"status": "ok"}

    @app.get("/ready")
    @app.get(API + "/ready")
    def ready():
        with service.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "storage": "local-private",
            "dispatcher": os.getenv("VKS_TASK_MODE", "local"),
            "capabilities": {"capture": True, "platform_llm": True},
        }

    @app.get(API + "/auth/session")
    def get_session(request: Request):
        try:
            a = actor(request)
        except DomainError:
            return envelope(
                request, {"authenticated": False, "bootstrap_required": True, "auth_mode": "local-bootstrap"}
            )
        return envelope(request, service.session_data(a))

    @app.post(API + "/auth/session")
    def login(payload: Login, request: Request):
        token, a = service.login(payload.bootstrap_token)
        r = JSONResponse(envelope(request, service.session_data(a)))
        r.set_cookie(
            "vks_session",
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=43200,
            path="/",
        )
        return r

    @app.delete(API + "/auth/session")
    def logout(request: Request, a: Actor = Depends(actor)):
        service.logout(a)
        r = JSONResponse(envelope(request, {"authenticated": False}))
        r.delete_cookie("vks_session", path="/")
        return r

    @app.get(API + "/projects")
    def list_projects(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, max_length=128),
        a: Actor = Depends(actor),
    ):
        rows, next_cursor = service.list_projects(a, cursor, limit, q)
        return envelope(request, rows, next_cursor=next_cursor, limit=limit)

    @app.post(API + "/projects", status_code=201)
    def create_project(
        payload: ProjectInput, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, service.create_project(a, payload.model_dump(), idempotency_key))

    @app.get(API + "/projects/{pid}")
    def project(pid: str, request: Request, a: Actor = Depends(actor)):
        d = service.get_project(a, pid)
        r = JSONResponse(envelope(request, d))
        r.headers["ETag"] = f'"{d["row_version"]}"'
        return r

    @app.patch(API + "/projects/{pid}")
    def patch_project(
        pid: str, payload: ProjectPatch, request: Request, a: Actor = Depends(actor), if_match: str = Header(...)
    ):
        return envelope(request, service.patch_project(a, pid, payload.model_dump(exclude_none=True), if_match))

    @app.get(API + "/projects/{pid}/members")
    def members(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.members(a, pid))

    @app.put(API + "/projects/{pid}/members")
    def member(
        pid: str, payload: MemberInput, request: Request, a: Actor = Depends(actor), if_match: str = Header(...)
    ):
        return envelope(request, service.set_member(a, pid, payload.model_dump(), if_match))

    @app.get(API + "/projects/{pid}/workspace")
    def workspace(
        pid: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        return envelope(request, service.workspace(a, pid, analysis_set_id, release_id))

    @app.get(API + "/projects/{pid}/context")
    def project_context(pid: str, release_id: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.project_context(a, pid, release_id))

    @app.post(API + "/projects/{pid}/upload-sessions", status_code=201)
    def upload_session(pid: str, payload: UploadInput, request: Request, a: Actor = Depends(actor)):
        d = service.create_upload(a, pid, payload.model_dump())
        d["upload_url"] = str(request.base_url).rstrip("/") + d["upload_url"]
        return envelope(request, d)

    @app.put(API + "/projects/{pid}/uploads/{upload_id}")
    async def upload(pid: str, upload_id: str, request: Request, a: Actor = Depends(actor)):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_UPLOAD:
                raise DomainError("VKS_LIMIT_EXCEEDED", "上传超过 128 MiB 上限", 413)
        return envelope(request, service.put_upload(a, pid, upload_id, bytes(raw)))

    @app.post(API + "/projects/{pid}/imports/preflight")
    def preflight(pid: str, payload: ImportInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.preflight(a, pid, payload.model_dump(exclude_none=True)))

    @app.post(API + "/projects/{pid}/imports", status_code=202)
    def imports(
        pid: str, payload: ImportInput, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, service.import_bundle(a, pid, payload.model_dump(exclude_none=True), idempotency_key))

    @app.post(API + "/projects/{pid}/imports/sample", status_code=202)
    @app.post(API + "/projects/{pid}/demo-import", status_code=202)
    def demo_import(pid: str, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)):
        return envelope(request, service.demo_import(a, pid, idempotency_key))

    @app.get(API + "/projects/{pid}/imports/{jid}/report")
    def import_report(pid: str, jid: str, request: Request, a: Actor = Depends(actor)):
        j = service.get_job(a, jid, pid)
        result = j.get("result", {})
        return envelope(
            request,
            {
                "import_id": jid,
                "accepted_count": result.get("accepted_count", 0),
                "rejected_count": len(result.get("issues", [])),
                "completeness": j["completeness"],
                "snapshot_ids": result.get("snapshot_ids", []),
                "skipped": result.get("issues", []),
            },
        )

    @app.post(API + "/projects/{pid}/analysis-sets", status_code=201)
    def analysis_set(
        pid: str,
        payload: AnalysisInput,
        request: Request,
        a: Actor = Depends(actor),
        idempotency_key: str = Header(...),
    ):
        return envelope(
            request, service.create_analysis(a, pid, payload.model_dump(exclude_none=True), idempotency_key)
        )

    @app.post(API + "/analysis-sets/{aid}/jobs", status_code=202)
    def analysis_job(
        aid: str, payload: JobInput, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(
            request, service.create_analysis_job(a, aid, payload.model_dump(exclude_none=True), idempotency_key)
        )

    @app.get(API + "/jobs/{jid}")
    def job(jid: str, request: Request, project_id: str | None = None, a: Actor = Depends(actor)):
        return envelope(request, service.get_job(a, jid, project_id))

    @app.post(API + "/jobs/{jid}/cancel")
    def cancel(
        jid: str,
        request: Request,
        payload: dict | None = None,
        project_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        return envelope(request, service.cancel_job(a, jid, (payload or {}).get("reason", "用户取消"), project_id))

    @app.post(API + "/jobs/{jid}/retry", status_code=202)
    def retry(jid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.retry_job(a, jid))

    @app.get(API + "/projects/{pid}/pages")
    def pages(
        pid: str,
        request: Request,
        release_id: str | None = None,
        analysis_set_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        w = service.workspace(a, pid, analysis_set_id, release_id)
        fields = ("entity_id", "entity_revision_id", "type", "name", "content_sha256", "schema_version")
        return envelope(request, [{k: e[k] for k in fields} for e in w["pages"]])

    @app.get(API + "/actions/{eid}/context")
    def action_context(
        eid: str,
        request: Request,
        release_id: str | None = None,
        analysis_set_id: str | None = None,
        depth: int = Query(2, ge=1, le=3),
        max_nodes: int = Query(50, ge=1, le=200),
        project_id: str | None = None,
        relation_policy: str = Query("valid", pattern="^(valid|outdated|all)$"),
        a: Actor = Depends(actor),
    ):
        pid = service.find_entity_project(a, eid)
        if project_id and project_id != pid:
            raise DomainError("VKS_NOT_FOUND", "未找到对象", 404)
        return envelope(
            request, service.action_context(a, pid, eid, analysis_set_id, release_id, depth, max_nodes, relation_policy)
        )

    @app.get(API + "/entities/{eid}/relations")
    def graph(
        eid: str,
        request: Request,
        release_id: str | None = None,
        analysis_set_id: str | None = None,
        depth: int = Query(2, ge=1, le=3),
        max_nodes: int = Query(50, ge=1, le=200),
        relation_types: str | None = None,
        project_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        pid = service.find_entity_project(a, eid)
        if project_id and project_id != pid:
            raise DomainError("VKS_NOT_FOUND", "未找到对象", 404)
        return envelope(
            request,
            service.graph(
                a,
                pid,
                eid,
                analysis_set_id,
                release_id,
                depth,
                max_nodes,
                relation_types.split(",") if relation_types else None,
            ),
        )

    @app.get(API + "/projects/{pid}/evidence")
    def evidence(
        pid: str,
        request: Request,
        evidence_ids: str,
        max_chars: int = Query(8000, ge=100, le=30000),
        a: Actor = Depends(actor),
    ):
        ids = evidence_ids.split(",")
        if not 1 <= len(ids) <= 20:
            raise DomainError("VKS_INVALID_INPUT", "每次读取 1–20 条证据")
        return envelope(request, service.get_evidence(a, pid, ids, max_chars))

    @app.get(API + "/evidence/{eid}")
    def evidence_one(eid: str, request: Request, project_id: str | None = None, a: Actor = Depends(actor)):
        pid = service.evidence_project(a, eid)
        if project_id and project_id != pid:
            raise DomainError("VKS_NOT_FOUND", "未找到证据", 404)
        return envelope(request, service.get_evidence(a, pid, [eid])[0])

    @app.get(API + "/documents/{eid}/revisions/{rid}")
    def document(eid: str, rid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.get_document(a, eid, rid))

    @app.put(API + "/documents/{eid}/draft")
    def draft(eid: str, payload: DraftInput, request: Request, a: Actor = Depends(actor), if_match: str = Header(...)):
        return envelope(request, service.save_document(a, eid, payload.model_dump(), if_match))

    @app.get(API + "/analysis-sets/{aid}/link-candidates")
    def candidates(aid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.candidates(a, aid))

    @app.post(API + "/projects/{pid}/link-proposals")
    def propose(
        pid: str,
        payload: ProposalInput,
        request: Request,
        a: Actor = Depends(actor),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, service.propose_links(a, pid, payload.model_dump(), idempotency_key))

    @app.get(API + "/projects/{pid}/releases")
    def releases(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.releases(a, pid))

    @app.get(API + "/projects/{pid}/releases/{rid}/diff")
    def release_diff(
        pid: str,
        rid: str,
        request: Request,
        to_analysis_set_id: str | None = None,
        from_release_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        if not to_analysis_set_id:
            raise DomainError("VKS_INVALID_INPUT", "需要 to_analysis_set_id")
        return envelope(request, service.diff(a, pid, from_release_id or rid, to_analysis_set_id))

    @app.post(API + "/projects/{pid}/search")
    def search(pid: str, payload: SearchInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.search(a, pid, payload.model_dump(exclude_none=True)))

    @app.post(API + "/projects/{pid}/questions")
    def question(pid: str, payload: QuestionInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.question(a, pid, payload.model_dump(exclude_none=True)))

    @app.post(API + "/projects/{pid}/exports", status_code=202)
    def export(
        pid: str, payload: ExportInput, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, service.export(a, pid, payload.model_dump(), idempotency_key))

    @app.get(API + "/projects/{pid}/exports/{jid}/download")
    def download_export(pid: str, jid: str, request: Request, a: Actor = Depends(actor)):
        raw, name = service.download_export(a, pid, jid)
        return Response(
            raw,
            media_type="text/html"
            if name.endswith(".html")
            else "application/pdf"
            if name.endswith(".pdf")
            else "application/zip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )

    @app.get(API + "/projects/{pid}/artifacts/{aid}/download")
    def download_artifact(pid: str, aid: str, request: Request, a: Actor = Depends(actor)):
        raw, name = service.artifact_bytes(a, pid, aid)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.get(API + "/projects/{pid}/audit-events")
    def audit(
        pid: str,
        request: Request,
        actor_id: str | None = None,
        action: str | None = None,
        resource_id: str | None = None,
        outcome: str | None = Query(default=None, pattern="^(succeeded|failed|denied)$"),
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        a: Actor = Depends(actor),
    ):
        return envelope(
            request,
            service.audit_events(
                a,
                pid,
                dict(
                    actor_id=actor_id,
                    action=action,
                    resource_id=resource_id,
                    outcome=outcome,
                    since=since,
                    until=until,
                    cursor=cursor,
                    limit=limit,
                ),
            ),
        )

    @app.get(API + "/projects/{pid}/tokens")
    def tokens(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.tokens(a, pid))

    @app.post(API + "/projects/{pid}/tokens", status_code=201)
    def token_create(pid: str, payload: TokenInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.create_token(a, pid, payload.model_dump()))

    @app.delete(API + "/projects/{pid}/tokens/{tid}")
    def token_revoke(pid: str, tid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, service.revoke_token(a, pid, tid))

    @app.get(API + "/mcp/catalog")
    def catalog(request: Request, a: Actor = Depends(actor)):
        path = Path(__file__).resolve().parents[3] / "packages/contracts/mcp_tools.json"
        return envelope(request, json.loads(path.read_text()))

    from .identity import install_identity
    from .scenarios import install_scenarios

    install_identity(app, service, actor, envelope, API)
    install_scenarios(app, service, actor, envelope, API)
    from .knowledge import install_knowledge

    install_knowledge(app, service, actor, envelope, API)
    from .oauth import install_oauth

    install_oauth(app, service, actor, envelope, API)
    from .ai_gateway import install_ai
    from .documents import install_documents
    from .versions import install_versions

    install_ai(app, service, actor, envelope, API)
    install_documents(app, service, actor, envelope, API)
    install_versions(app, service, actor, envelope, API)
    from .relationships import install_relationships

    install_relationships(app, service, actor, envelope, API)
    from .modules import install_modules
    from .attachments import install_attachments

    install_modules(app, service, actor, envelope, API)
    install_attachments(app, service, actor, API)
    from .hotspots import install_hotspots

    install_hotspots(app, service, actor, envelope, API)
    from .prototype import install_prototype

    install_prototype(app, service, actor, envelope)
    from .interactive import install_interactive

    install_interactive(app, service, actor, envelope)
    from .materials import install_materials

    install_materials(app, service, actor, envelope)
    from .bulk_association import install_bulk_association
    from .document_presentation import install_document_presentation

    install_bulk_association(app, service, actor, envelope)
    from .reassociation_batches import install_reassociation_batches

    install_reassociation_batches(app, service, actor, envelope)
    install_document_presentation(app, service, actor, envelope)
    from .prototype_scan import install_prototype_scan

    install_prototype_scan(app, service, actor, envelope)
    from .project_profile import install_project_profile

    install_project_profile(app, service, actor, envelope, API)
    from .journeys import install_journeys
    from .workbench_v2 import install_workbench

    install_journeys(app, service, actor, envelope)
    install_workbench(app, service, actor, envelope)
    from .onboarding_report import install as install_onboarding_report

    install_onboarding_report(app, service, actor, envelope)
    from .source_alignment import install_source_alignment

    install_source_alignment(app, service, actor, envelope)
    from .source_sync import install_source_sync

    install_source_sync(app, service, actor, envelope)
    from .trajectory_comparison import install_trajectory_comparison

    install_trajectory_comparison(app, service, actor, envelope)
    from .document_diff import install_document_diff

    install_document_diff(app, service, actor, envelope)
    from .knowledge_tasks import install_tasks

    install_tasks(app, service, actor, envelope)
    from .capture_proposals import install_capture_proposals

    install_capture_proposals(app, service, actor, envelope)
    return app


app = create_app()
