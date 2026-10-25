from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock, Event, Thread
from uuid import uuid4
import hashlib
import io
import json
import os
import secrets
import time
import zipfile
from sqlalchemy import create_engine, select, update, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from .models import *
from . import identity as team_identity

READ = "knowledge:read"
ALL_SCOPES = {
    READ,
    "project:manage",
    "source:read",
    "trace:read",
    "source:import",
    "analysis:run",
    "links:propose",
    "links:review",
    "release:publish",
    "capture:run",
    "knowledge:export",
    "audit:read",
    "knowledge:edit",
}
MACHINE_SCOPES = ALL_SCOPES - {"project:manage", "links:review", "release:publish"}
ROLE_SCOPES = {
    "viewer": {READ, "knowledge:export"},
    "editor": {READ, "knowledge:edit", "source:import", "analysis:run", "links:propose", "knowledge:export"},
    "reviewer": {
        READ,
        "knowledge:edit",
        "source:import",
        "analysis:run",
        "links:propose",
        "links:review",
        "release:publish",
        "knowledge:export",
    },
    "maintainer": ALL_SCOPES,
    "admin": ALL_SCOPES,
}
SOURCE_TYPES = {"symbol", "sql", "table"}
JOB_SCOPES = {
    "import": "source:import",
    "index": "analysis:run",
    "generate_documents": "analysis:run",
    "suggest_links": "analysis:run",
    "detect_drift": "analysis:run",
    "capture": "capture:run",
    "release_precheck": "links:propose",
    "export": "knowledge:export",
}
RELATION_TYPES = {
    "contains",
    "renders",
    "handler_of",
    "may_call",
    "observed_during",
    "confirmed_trigger",
    "implements",
    "maps_to_sql",
    "reads_table",
    "writes_table",
    "documents",
    "depends_on",
    "supersedes",
}


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def future(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class DomainError(Exception):
    def __init__(self, code, message, status=422, details=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable


@dataclass(frozen=True)
class Actor:
    id: str
    scopes: frozenset[str]
    human: bool = False
    project_id: str | None = None
    credential_id: str | None = None
    csrf_token: str = ""


class Service:
    def __init__(self, data_dir=None, database_url=None):
        from . import (
            knowledge,
            scenarios,
            jobs,
            oauth,
            ai_gateway,
            documents,
            versions,
            relationships,
            modules,
            project_profile,
        )

        self.data_dir = Path(data_dir or os.getenv("VKS_DATA_DIR", ".vks")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        (self.data_dir / "objects").mkdir(exist_ok=True)
        from .connection import database_url as configured_database_url

        url = database_url or configured_database_url(f"sqlite:///{self.data_dir}/vks.db")
        args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        opts = (
            {"poolclass": StaticPool}
            if url in ("sqlite://", "sqlite:///:memory:")
            else {
                "pool_size": max(5, int(os.getenv("VKS_DB_POOL_SIZE", "20"))),
                "max_overflow": 10,
            }
        )
        self.engine = create_engine(url, connect_args=args, **opts)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def sqlite_config(conn, record):
                conn.execute("PRAGMA foreign_keys=ON")
                conn.create_function(
                    "vks_casefold",
                    1,
                    lambda value: value.casefold() if isinstance(value, str) else "",
                    deterministic=True,
                )
                conn.create_function(
                    "vks_json_casefold",
                    1,
                    lambda value: canonical(json.loads(value)).casefold() if value is not None else "",
                    deterministic=True,
                )

        if url.startswith("sqlite"):
            with self.engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                Base.metadata.create_all(connection)
                connection.commit()
        elif os.getenv("VKS_AUTO_MIGRATE") == "1":
            Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = RLock()
        self.stop = Event()
        self.thread = None
        self.bootstrap_token = self._private_setting(
            "bootstrap-token", lambda: os.getenv("VKS_BOOTSTRAP_TOKEN") or secrets.token_urlsafe(32)
        )
        self.owner_id = self._private_setting("owner-id", uid)

    def _private_setting(self, name, generate):
        target = self.data_dir / name
        if not target.exists():
            temporary = self.data_dir / (".init-" + secrets.token_hex(12))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(generate())
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        return target.read_text().strip()

    @contextmanager
    def db(self):
        with self.lock:
            try:
                with self.Session.begin() as s:
                    if self.engine.dialect.name == "sqlite":
                        s.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    yield s
            except StaleDataError as e:
                raise DomainError("VKS_VERSION_CONFLICT", "当前内容已更新，请重新加载", 412) from e
            except IntegrityError as e:
                raise DomainError("VKS_STATE_CONFLICT", "对象唯一性或引用约束冲突，请重新加载", 409) from e

    def start_worker(self):
        from .jobs import dispatch_mode

        if dispatch_mode() == "external" or (self.thread and self.thread.is_alive()):
            return
        self.stop.clear()

        def loop():
            while not self.stop.wait(0.2):
                try:
                    self.run_pending()
                except Exception:
                    continue

        self.thread = Thread(target=loop, daemon=True, name="vks-durable-dispatcher")
        self.thread.start()

    def stop_worker(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def audit(self, s, actor, pid, action, rid, outcome="succeeded", metadata=None):
        s.add(
            Audit(
                project_id=pid,
                actor_id=actor.id if isinstance(actor, Actor) else actor,
                action=action,
                resource_id=rid,
                outcome=outcome,
                metadata_json=metadata or {},
            )
        )

    def login(self, token):
        return team_identity.bootstrap_login(self, token)

    def authenticate(self, secret, kind=None):
        if kind in (None, "token") and isinstance(secret, str) and secret.count(".") == 2:
            from .oauth import authenticate

            return authenticate(self, secret)
        return team_identity.authenticate(self, secret, kind)

    def session_data(self, actor):
        return team_identity.session_data(self, actor)

    def logout(self, actor):
        with self.db() as s:
            c = s.get(Credential, actor.credential_id)
            if c:
                c.revoked = True

    def require(self, s, actor, pid, scope=READ):
        return team_identity.authorize_project(self, s, actor, pid, scope)

    def _can(self, s, actor, pid, scope):
        transaction = s.get_transaction()
        if s.info.get("vks_permission_transaction") is not transaction:
            s.info["vks_permissions"] = {}
            s.info["vks_permission_transaction"] = transaction
        cache = s.info.setdefault("vks_permissions", {})
        key = (actor.id, actor.human, actor.project_id, actor.credential_id, actor.scopes, pid, scope)
        if key not in cache:
            try:
                self.require(s, actor, pid, scope)
                cache[key] = True
            except DomainError:
                cache[key] = False
        return cache[key]

    def _version(self, value, expected):
        if expected is None:
            raise DomainError("VKS_VERSION_CONFLICT", "需要 If-Match 版本", 412, {"current_version": value})
        if str(expected).strip('"') != str(value):
            raise DomainError(
                "VKS_VERSION_CONFLICT", "当前内容已更新，请重新加载后合并", 412, {"current_version": value}
            )

    def idem(self, s, actor, pid, operation, key, payload, fn):
        if not key or len(key) > 128:
            raise DomainError("VKS_INVALID_INPUT", "需要 1–128 字符的 Idempotency-Key")
        if self.engine.dialect.name == "postgresql":
            lock_key = int.from_bytes(
                hashlib.sha256(f"{pid}:{actor.id}:{operation}:{key}".encode()).digest()[:8], "big", signed=True
            )
            s.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
        h = sha(canonical(payload))
        row = s.scalar(
            select(Idempotency).where(
                Idempotency.project_id == pid,
                Idempotency.actor_id == actor.id,
                Idempotency.operation == operation,
                Idempotency.key == key,
            )
        )
        if row:
            if row.payload_hash != h:
                raise DomainError("VKS_IDEMPOTENCY_CONFLICT", "相同幂等键对应不同内容", 409)
            return row.response_json
        result = fn()
        s.flush()
        s.add(
            Idempotency(
                project_id=pid, actor_id=actor.id, operation=operation, key=key, payload_hash=h, response_json=result
            )
        )
        return result

    def project_dto(self, p):
        d = {
            "id": p.id,
            "key": p.key,
            "name": p.name,
            "description": p.description,
            "status": p.status,
            "row_version": p.row_version,
        }
        if p.current_release_id:
            d["current_release_id"] = p.current_release_id
        return d

    def list_projects(self, actor, cursor=None, limit=20):
        with self.db() as s:
            if READ not in actor.scopes:
                raise DomainError("VKS_FORBIDDEN", "需要 knowledge:read 权限", 403)
            if actor.human:
                q = (
                    select(Project)
                    .join(Member, Member.project_id == Project.id)
                    .where(Member.user_id == actor.id, Member.status == "active")
                )
            elif actor.credential_id and actor.credential_id.startswith("oauth:"):
                from .oauth import project_ids

                q = select(Project).where(Project.id.in_(project_ids(self, s, actor)))
            else:
                q = select(Project).where(Project.id == actor.project_id)
            rows = s.scalars(q.order_by(Project.created_at, Project.id)).all()
            if cursor:
                ids = [p.id for p in rows]
                if cursor not in ids:
                    raise DomainError("VKS_INVALID_INPUT", "分页游标无效")
                rows = rows[ids.index(cursor) + 1 :]
            return [self.project_dto(p) for p in rows[:limit]], rows[limit - 1].id if len(rows) > limit else None

    def create_project(self, actor, payload, key):
        if not actor.human or actor.id != self.owner_id or "project:manage" not in actor.scopes:
            raise DomainError("VKS_FORBIDDEN", "需要实例管理员身份", 403)
        with self.db() as s:

            def create():
                if s.scalar(select(Project).where(Project.key == payload["key"])):
                    raise DomainError("VKS_STATE_CONFLICT", "项目 key 已存在", 409)
                p = Project(
                    id=uid(),
                    key=payload["key"],
                    name=payload["name"],
                    description=payload.get("description", ""),
                    owner_id=actor.id,
                    row_version=1,
                    status="active",
                )
                s.add(p)
                s.flush()
                s.add(Member(project_id=p.id, user_id=actor.id, role="admin"))
                self.audit(s, actor, p.id, "project.create", p.id)
                return self.project_dto(p)

            return self.idem(s, actor, "instance", "project.create", key, payload, create)

    def get_project(self, actor, pid):
        with self.db() as s:
            return self.project_dto(self.require(s, actor, pid))

    def patch_project(self, actor, pid, payload, version):
        with self.db() as s:
            p = self.require(s, actor, pid, "project:manage")
            self._version(p.row_version, version)
            if payload.get("current_release_id"):
                r = s.get(Release, payload["current_release_id"])
                if not r or r.project_id != pid:
                    raise DomainError("VKS_NOT_FOUND", "未找到发布版", 404)
            for k, v in payload.items():
                if v is not None:
                    setattr(p, k, v)
            p.row_version += 1
            self.audit(s, actor, pid, "project.update", pid)
            return self.project_dto(p)

    def analysis_dto(self, a):
        return {
            "id": a.id,
            "project_id": a.project_id,
            "key": a.key,
            "name": a.name,
            "state": a.state,
            "sources": a.sources,
            "ruleset_version": a.ruleset_version,
            "selection_version": a.selection_version,
            "runtime_alignment": a.runtime_alignment,
            "created_at": a.created_at,
        }

    def _visible_sources(self, s, actor, pid, sources):
        visible = []
        for source in sources:
            rows = list(
                s.scalars(
                    select(Artifact).where(
                        Artifact.project_id == pid, Artifact.snapshot_id == source.get("source_snapshot_id")
                    )
                )
            )
            if not rows:
                continue
            if any(
                (
                    art.kind == "source"
                    or art.sensitivity == "confidential"
                    or art.metadata_json.get("requires_source_read")
                )
                and not self._can(s, actor, pid, "source:read")
                for art in rows
            ):
                continue
            if any(
                (art.kind in ("trace", "capture") or art.metadata_json.get("requires_trace_read"))
                and not self._can(s, actor, pid, "trace:read")
                for art in rows
            ):
                continue
            visible.append(source)
        return visible

    def analysis_public(self, s, actor, a):
        return {**self.analysis_dto(a), "sources": self._visible_sources(s, actor, a.project_id, a.sources)}

    def entity_dto(self, s, r, extended=False):
        e = s.get(Entity, r.entity_id)
        d = {
            "entity_id": e.id,
            "entity_revision_id": r.id,
            "type": e.type,
            "name": r.name,
            "content_sha256": r.content_sha256,
            "semantic_content_sha256": r.semantic_content_sha256,
            "schema_version": "1.0",
        }
        if extended:
            d.update(
                id=e.id,
                revision_id=r.id,
                display_name=r.name,
                stable_key=e.stable_key,
                content=r.content,
                metadata=r.metadata_json,
                locator=r.locator,
                artifact_id=r.artifact_id,
                row_version=r.metadata_json.get("row_version", 1),
            )
        return d

    def relation_dto(self, r):
        return {
            "id": r.id,
            "analysis_set_id": r.analysis_set_id,
            "from_revision_id": r.from_revision_id,
            "to_revision_id": r.to_revision_id,
            "relation_type": r.relation_type,
            "review_status": r.review_status,
            "validity_status": r.validity_status,
            "causal_basis": r.causal_basis,
            "evidence_ids": r.evidence_ids,
            "reason": r.reason,
            "row_version": r.row_version,
        }

    def evidence_dto(self, e, max_chars=8000):
        return {
            "id": e.id,
            "evidence_kind": e.evidence_kind,
            "artifact_id": e.artifact_id,
            "content_sha256": e.content_sha256,
            "locator": {key: value for key, value in e.locator.items() if not key.startswith("_")},
            "excerpt": e.excerpt[:max_chars],
            "extractor_version": e.locator.get("_extractor_version")
            or (
                "vks-controlled-runner-1"
                if e.evidence_kind == "observed" and e.locator.get("source_kind") == "runtime_capture"
                else "vks-explicit-1"
            ),
            "captured_at": e.created_at,
            "redaction_version": e.locator.get("_redaction_version")
            or (
                "vks-runner-redact-1"
                if e.evidence_kind == "observed" and e.locator.get("source_kind") == "runtime_capture"
                else "import-policy-v1"
            ),
        }

    def job_dto(self, j):
        d = {
            "id": j.id,
            "project_id": j.project_id,
            "kind": j.kind,
            "state": j.state,
            "progress": j.progress,
            "completeness": j.completeness,
            "stage": j.stage,
            "attempt": j.attempt,
            "created_at": j.created_at,
            "created_by": j.created_by,
            "updated_at": j.updated_at,
            "issue_count": j.issue_count,
            "result_refs": j.result_json.get("result_refs", []),
        }
        if j.result_json:
            d["result"] = j.result_json
        if j.error_json:
            d["error"] = j.error_json
        return d

    def release_dto(self, r):
        return {
            "id": r.id,
            "project_id": r.project_id,
            "analysis_set_id": r.analysis_set_id,
            "release_no": r.release_no,
            "manifest_sha256": r.manifest_sha256,
            "published_at": r.created_at,
            "published_by": r.published_by,
        }

    def _selected(self, s, actor, pid, analysis_set_id=None, release_id=None):
        p = self.require(s, actor, pid)
        if analysis_set_id and release_id:
            raise DomainError("VKS_INVALID_INPUT", "release_id 与 analysis_set_id 只能选择一个")
        if release_id:
            r = s.get(Release, release_id)
            if not r or r.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到发布版", 404)
            return s.get(Analysis, r.analysis_set_id), r
        if analysis_set_id:
            a = s.get(Analysis, analysis_set_id)
            if not a or a.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            release = s.scalar(select(Release).where(Release.analysis_set_id == a.id))
            if not release:
                self.require(s, actor, pid, "knowledge:edit")
            return a, release
        if self._can(s, actor, pid, "knowledge:edit"):
            a = s.scalar(select(Analysis).where(Analysis.project_id == pid).order_by(Analysis.created_at.desc()))
            return a, s.scalar(select(Release).where(Release.analysis_set_id == a.id)) if a else None
        if p.current_release_id:
            r = s.get(Release, p.current_release_id)
            return s.get(Analysis, r.analysis_set_id), r
        return None, None

    def _revisions(self, s, aid):
        return list(
            s.scalars(
                select(Revision)
                .join(Selection, Selection.entity_revision_id == Revision.id)
                .where(Selection.analysis_set_id == aid)
            )
        )

    def _relations(self, s, a, release=None):
        if release:
            return release.manifest_json["relations"]
        return [self.relation_dto(r) for r in s.scalars(select(Relation).where(Relation.analysis_set_id == a.id)).all()]

    def _visible_revision(self, s, actor, pid, r):
        art = s.get(Artifact, r.artifact_id) if r.artifact_id else None
        source_restricted = s.get(Entity, r.entity_id).type in SOURCE_TYPES or (
            art
            and (
                art.kind == "source"
                or art.sensitivity == "confidential"
                or art.metadata_json.get("requires_source_read")
            )
        )
        trace_restricted = art and (art.kind in ("trace", "capture") or art.metadata_json.get("requires_trace_read"))
        return (not source_restricted or self._can(s, actor, pid, "source:read")) and (
            not trace_restricted or self._can(s, actor, pid, "trace:read")
        )

    def _visible_evidence(self, s, actor, pid, e):
        a = s.get(Artifact, e.artifact_id)
        return (
            (
                (a.kind != "source" and not a.metadata_json.get("requires_source_read"))
                or self._can(s, actor, pid, "source:read")
            )
            and (
                (a.kind not in ("trace", "capture") and not a.metadata_json.get("requires_trace_read"))
                or self._can(s, actor, pid, "trace:read")
            )
            and (a.sensitivity != "confidential" or self._can(s, actor, pid, "source:read"))
        )

    def workspace(self, actor, pid, analysis_set_id=None, release_id=None):
        from .reading import ReadingService
        from sqlalchemy.orm import load_only

        reading = ReadingService(self)
        with reading.db() as s:
            p = self.require(s, actor, pid)
            a, release = reading.baseline(s, actor, pid, analysis_set_id, release_id)
            can_edit = self._can(s, actor, pid, "knowledge:edit")
            analyses = (
                list(s.scalars(select(Analysis).where(Analysis.project_id == pid).order_by(Analysis.created_at.desc())))
                if can_edit
                else []
            )
            releases = list(
                s.scalars(
                    select(Release)
                    .options(
                        load_only(
                            Release.id,
                            Release.project_id,
                            Release.analysis_set_id,
                            Release.release_no,
                            Release.manifest_sha256,
                            Release.created_at,
                            Release.published_by,
                        )
                    )
                    .where(Release.project_id == pid)
                    .order_by(Release.created_at.desc())
                )
            )
            choices, index = reading.selections(s, a, release)
            permitted = reading.visible_ids(s, actor, pid, choices)
            revs = reading.revision_rows(s, permitted.values())
            entities = [self.entity_dto(s, r, True) for r in revs]
            visible = {r.id for r in revs}
            rels = (
                [
                    r
                    for r in reading.edge_rows(s, a.id, index, selected=visible)
                    if r["from_revision_id"] in visible and r["to_revision_id"] in visible
                ]
                if a
                else []
            )
            evidence_rows = list(s.scalars(select(Evidence).where(Evidence.analysis_set_id == a.id))) if a else []
            reading.preload(s, evidence=evidence_rows)
            evs = [e for e in evidence_rows if self._visible_evidence(s, actor, pid, e)]
            allowed_e = {e.id for e in evs}
            for r in rels:
                r["evidence_ids"] = [eid for eid in r["evidence_ids"] if eid in allowed_e]
            rels = [r for r in rels if r["evidence_ids"]]
            names = {e["entity_revision_id"]: e["name"] for e in entities}
            rels = [dict(r, from_name=names[r["from_revision_id"]], to_name=names[r["to_revision_id"]]) for r in rels]
            jobs = (
                [
                    reading.job_dto(s, actor, j)
                    for j in s.scalars(
                        select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc()).limit(30)
                    )
                ]
                if can_edit
                else []
            )
            audit = (
                [
                    self.audit_dto(e)
                    for e in s.scalars(
                        select(Audit).where(Audit.project_id == pid).order_by(Audit.created_at.desc()).limit(30)
                    )
                ]
                if self._can(s, actor, pid, "audit:read")
                else []
            )
            actions = [e for e in entities if e["type"] == "action" and e["metadata"].get("status") != "removed"]
            confirmed = {
                r["from_revision_id"]
                for r in rels
                if r["review_status"] == "confirmed" and r["validity_status"] == "valid"
            }
            from .runtime import status

            runtime = status(self, s, pid)
            return {
                "runtime_status": runtime,
                "project": self.project_dto(p),
                "analysis_sets": [self.analysis_public(s, actor, x) for x in analyses],
                "selected_analysis_set": self.analysis_public(s, actor, a) if a else None,
                "selected_release": self.release_dto(release) if release else None,
                "releases": [self.release_dto(r) for r in releases],
                "entities": entities,
                "relations": rels,
                "evidence": [self.evidence_dto(e) for e in evs],
                "documents": [e for e in entities if e["type"] == "document"],
                "pages": [e for e in entities if e["type"] == "page"],
                "actions": actions,
                "scenarios": [e for e in entities if e["type"] == "scenario"],
                "sources": self._visible_sources(s, actor, pid, a.sources) if a else [],
                "jobs": jobs,
                "audit_events": audit,
                "capabilities": [
                    "bundle_import",
                    "explicit_bindings",
                    "markdown",
                    "openapi_index",
                    "safe_explanation",
                    "human_review",
                    "immutable_releases",
                    "scoped_tokens",
                    "mcp_stdio",
                    "mcp_http_oauth",
                    "java_spring_mybatis_ast",
                    "immutable_scenarios",
                    "controlled_browser_runner",
                    "source_snapshots",
                    "client_readonly_collectors",
                    "document_workflow",
                    "version_proposals",
                    "optional_model_gateway",
                ],
                "unknowns": [
                    "尚未接入真实业务环境；运行与源码版本未核对",
                    "静态候选和单次采集不能证明全部业务行为；发布前需人员审核",
                ],
                "stats": {
                    "sources": len(self._visible_sources(s, actor, pid, a.sources)) if a else 0,
                    "scenarios": len([e for e in entities if e["type"] == "scenario"]),
                    "actions": len(actions),
                    "linked_actions": len({e["entity_revision_id"] for e in actions} & confirmed),
                    "confirmed_relations": len([r for r in rels if r["review_status"] == "confirmed"]),
                    "candidate_relations": len([r for r in rels if r["review_status"] == "candidate"]),
                    "broken_relations": len([r for r in rels if r["validity_status"] == "broken"]),
                    "documents": len([e for e in entities if e["type"] == "document"]),
                },
            }

    def create_upload(self, actor, pid, payload):
        with self.db() as s:
            self.require(s, actor, pid, "source:import")
            u = Upload(
                id=uid(),
                artifact_id=uid(),
                project_id=pid,
                created_by=actor.id,
                expires_at=future(3600),
                complete=False,
                **payload,
            )
            s.add(u)
            return {
                "upload_session_id": u.id,
                "artifact_id": u.artifact_id,
                "upload_url": f"/api/v1/projects/{pid}/uploads/{u.id}",
                "expires_at": u.expires_at,
            }

    def _put_object(self, data):
        h = sha(data)
        p = self.data_dir / "objects" / h
        if not p.exists():
            tmp = p.with_name(h + "." + secrets.token_hex(8))
            tmp.write_bytes(data)
            tmp.chmod(0o600)
            os.replace(tmp, p)
        return h

    def put_upload(self, actor, pid, upload_id, data):
        with self.db() as s:
            self.require(s, actor, pid, "source:import")
            u = s.get(Upload, upload_id)
            if not u or u.project_id != pid or u.created_by != actor.id:
                raise DomainError("VKS_NOT_FOUND", "未找到上传会话", 404)
            if u.expires_at < now():
                raise DomainError("VKS_STATE_CONFLICT", "上传会话已过期", 409)
            if len(data) != u.size_bytes or sha(data) != u.sha256:
                raise DomainError("VKS_INVALID_INPUT", "上传内容的大小或 SHA-256 不匹配")
            self._put_object(data)
            u.complete = True
            self.audit(s, actor, pid, "upload.complete", u.artifact_id)
            return {"artifact_id": u.artifact_id, "complete": True}

    def _upload_data(self, s, actor, pid, artifact_id):
        u = s.scalar(
            select(Upload).where(Upload.artifact_id == artifact_id, Upload.project_id == pid, Upload.complete == True)
        )
        if not u or u.created_by != actor.id:
            raise DomainError("VKS_NOT_FOUND", "未找到已完成的上传", 404)
        data = (self.data_dir / "objects" / u.sha256).read_bytes()
        if len(data) != u.size_bytes or sha(data) != u.sha256:
            raise DomainError("VKS_INVALID_INPUT", "上传对象完整性检查失败")
        return data, u

    def _parse(self, data, project_key):
        from packages.analyzers.vks_ingest import parse_bundle, BundleError

        try:
            return parse_bundle(data, project_key)
        except BundleError as e:
            raise DomainError(
                "VKS_INVALID_INPUT", e.message, 422, {"reason_code": e.code, **getattr(e, "details", {})}
            ) from e

    def preflight(self, actor, pid, payload):
        if payload["kind"] != "bundle":
            from .knowledge import ImportService

            return ImportService(self).preview(actor, pid, payload)
        with self.db() as s:
            p = self.require(s, actor, pid, "source:import")
            if payload["kind"] != "bundle" or len(payload["artifact_ids"]) != 1:
                raise DomainError("VKS_INVALID_INPUT", "此轮预检支持单个 .vksbundle.zip 包")
            data, u = self._upload_data(s, actor, pid, payload["artifact_ids"][0])
            key = p.key
        bundle = self._parse(data, key)
        return {
            "valid": True,
            "project_key": bundle.manifest["project_key"],
            "baseline_key": bundle.manifest["baseline_key"],
            "file_count": len(bundle.artifacts),
            "size_bytes": sum(x["size_bytes"] for x in bundle.artifacts),
            "entity_count": len(bundle.entities),
            "relation_count": len(bundle.relations),
            "source_count": len(bundle.sources),
            "issues": bundle.issues,
            "allow_platform_llm": bool(bundle.manifest.get("allow_platform_llm", False)),
            "capabilities": ["readonly_import", "explicit_bindings", "java_spring_mybatis_ast"],
            "requires_review": True,
        }

    def _new_job(self, s, actor, pid, kind, payload):
        j = Job(
            id=uid(),
            project_id=pid,
            kind=kind,
            state="queued",
            created_by=actor.id,
            input_json=payload,
            attempt=0,
            stage="VALIDATE",
            progress=0,
            completeness="none",
        )
        s.add(j)
        s.flush()
        s.add(Outbox(job_id=j.id, dispatched=False))
        self.audit(s, actor, pid, "job.create", j.id, metadata={"kind": kind})
        return j

    def import_bundle(self, actor, pid, payload, key):
        if payload["kind"] != "bundle":
            from .knowledge import ImportService

            return ImportService(self).enqueue(actor, pid, payload, key)
        with self.db() as s:
            self.require(s, actor, pid, "source:import")
            if payload["kind"] != "bundle":
                raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "此轮请将 Markdown / 原型整理为显式绑定包导入", 503)
            if len(payload["artifact_ids"]) != 1:
                raise DomainError("VKS_INVALID_INPUT", "bundle 只允许一个上传对象")
            self._upload_data(s, actor, pid, payload["artifact_ids"][0])
            return self.idem(
                s,
                actor,
                pid,
                "import",
                key,
                payload,
                lambda: self.job_dto(self._new_job(s, actor, pid, "import", payload)),
            )

    def demo_import(self, actor, pid, key):
        with self.db() as s:
            p = self.require(s, actor, pid, "source:import")
            project_key = p.key
        path = Path(__file__).resolve().parents[3] / "fixtures" / "charge-demo.vksbundle.zip"
        if not path.exists():
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "合成样例包尚未生成", 503)
        data = path.read_bytes()
        # Re-key only the bundled synthetic fixture; ordinary uploads always remain byte-preserved.
        if project_key != "charge-demo":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                files = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
            m = json.loads(files["manifest.json"])
            m["project_key"] = project_key
            for ref in ("uiManifest", "bindings"):
                if ref in m:
                    rp = m[ref]["path"]
                    obj = json.loads(files[rp])
                    obj["projectKey"] = project_key
                    files[rp] = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
                    m[ref]["sha256"] = sha(files[rp])
                    for a in m["artifacts"]:
                        if a["path"] == rp:
                            a["sha256"] = sha(files[rp])
                            a["size_bytes"] = len(files[rp])
            for source in m["sources"]:
                records = [
                    {k: a[k] for k in ("path", "sha256", "size_bytes")}
                    for a in m["artifacts"]
                    if a["source_id"] == source["source_id"]
                ]
                records.sort(key=lambda x: x["path"])
                source["content_sha256"] = sha(canonical(records))
            files["manifest.json"] = json.dumps(m, ensure_ascii=False).encode()
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for name, content in files.items():
                    z.writestr(name, content)
            data = out.getvalue()
        # Upload and enqueue through the same handles and parser as user-provided material.
        payload = {
            "filename": "charge-demo.vksbundle.zip",
            "size_bytes": len(data),
            "sha256": sha(data),
            "media_type": "application/zip",
        }
        with self.db() as s:
            cached = s.scalar(
                select(Idempotency).where(
                    Idempotency.project_id == pid,
                    Idempotency.actor_id == actor.id,
                    Idempotency.operation == "demo_import",
                    Idempotency.key == key,
                )
            )
            if cached:
                return cached.response_json
        upload = self.create_upload(actor, pid, payload)
        self.put_upload(actor, pid, upload["upload_session_id"], data)
        job = self.import_bundle(actor, pid, {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]}, "demo:" + key)
        with self.db() as s:
            s.add(
                Idempotency(
                    project_id=pid,
                    actor_id=actor.id,
                    operation="demo_import",
                    key=key,
                    payload_hash=sha("{}"),
                    response_json=job,
                )
            )
        return job

    def run_pending(self):
        from .jobs import run_pending

        return run_pending(self)

    def process_job(self, jid):
        from .jobs import execute_job

        return execute_job(self, jid)

    def _commit_bundle(self, s, pid, owner, bundle, analysis_id=None):
        from .knowledge import persist_bundle

        return persist_bundle(self, s, pid, owner, bundle, analysis_id)

    def get_job(self, actor, jid, pid=None):
        from .reading import ReadingService

        reading = ReadingService(self)
        with reading.db() as s:
            j = s.get(Job, jid)
            if not j or (pid and j.project_id != pid):
                raise DomainError("VKS_NOT_FOUND", "未找到任务", 404)
            self.require(s, actor, j.project_id)
            if not self._can(s, actor, j.project_id, "knowledge:edit") and j.created_by != actor.id:
                raise DomainError("VKS_NOT_FOUND", "未找到任务", 404)
            dto = reading.job_dto(s, actor, j)
            if j.kind == "detect_drift" and "result" in dto:
                from .versions import VersionService

                dto["result"] = VersionService(self).public_report(actor, j.project_id, dto["result"])
            return dto

    def cancel_job(self, actor, jid, reason="用户取消", pid=None):
        with self.db() as s:
            j = s.get(Job, jid)
            if not j or (pid and j.project_id != pid):
                raise DomainError("VKS_NOT_FOUND", "未找到任务", 404)
            self.require(s, actor, j.project_id)
            s.refresh(j, with_for_update=True)
            if not self._can(s, actor, j.project_id, "project:manage"):
                if j.created_by != actor.id:
                    raise DomainError("VKS_FORBIDDEN", "只能取消自己发起的任务", 403)
                self.require(s, actor, j.project_id, JOB_SCOPES[j.kind])
            if j.state in ("queued", "retry_wait"):
                j.state = "cancelled"
                j.stage = "CANCELLED"
                j.cancel_requested = True
            elif j.state == "running":
                j.state = "cancelling"
                j.cancel_requested = True
            self.audit(s, actor, j.project_id, "job.cancel", jid, metadata={"reason": reason[:500]})
            return self.job_dto(j)

    def retry_job(self, actor, jid):
        with self.db() as s:
            j = s.get(Job, jid)
            if not j:
                raise DomainError("VKS_NOT_FOUND", "未找到任务", 404)
            self.require(s, actor, j.project_id, JOB_SCOPES[j.kind])
            s.refresh(j, with_for_update=True)
            if j.created_by != actor.id and not self._can(s, actor, j.project_id, "project:manage"):
                raise DomainError("VKS_FORBIDDEN", "只能重试自己发起的任务", 403)
            if j.state not in ("cancelled", "failed"):
                raise DomainError("VKS_STATE_CONFLICT", "只有失败或取消的任务可重试", 409)
            if j.kind == "capture":
                raise DomainError("VKS_APPROVAL_REQUIRED", "录制重试需要重新授权", 403)
            j.state = "queued"
            j.cancel_requested = False
            j.error_json = {}
            j.progress = 0
            j.stage = "VALIDATE"
            j.updated_at = now()
            self.audit(s, actor, j.project_id, "job.retry", jid)
            return self.job_dto(j)

    def review_relation(self, actor, rid, payload, version):
        with self.db() as s:
            r = s.get(Relation, rid)
            if not r:
                raise DomainError("VKS_NOT_FOUND", "未找到关系", 404)
            self.require(s, actor, r.project_id, "links:review")
            s.refresh(r)
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "普通客户端令牌不能审核关系", 403)
            self._version(r.row_version, version)
            a = s.get(Analysis, r.analysis_set_id)
            if a.state == "frozen":
                raise DomainError("VKS_STATE_CONFLICT", "已发布基线不可修改，请创建新草稿", 409)
            if set(payload["evidence_ids"]) - set(r.evidence_ids):
                raise DomainError("VKS_INVALID_EVIDENCE", "审核证据必须属于当前关系")
            self._verify_relation(s, r)
            if not all(
                self._visible_revision(s, actor, r.project_id, s.get(Revision, rid))
                for rid in (r.from_revision_id, r.to_revision_id)
            ) or not all(
                self._visible_evidence(s, actor, r.project_id, s.get(Evidence, eid)) for eid in payload["evidence_ids"]
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到可审核的证据", 404)
            rev = Review(
                id=uid(),
                project_id=r.project_id,
                subject_id=r.id,
                actor_id=actor.id,
                decision=payload["decision"],
                reason=payload["reason"],
                base_version=r.row_version,
                evidence_ids=payload["evidence_ids"],
            )
            s.add(rev)
            r.review_status = {"confirm": "confirmed", "reject": "rejected", "reopen": "candidate"}[payload["decision"]]
            r.row_version += 1
            r.manual_lock = r.review_status == "confirmed"
            a.selection_version += 1
            self.audit(
                s,
                actor,
                r.project_id,
                "relation.review",
                r.id,
                metadata={"decision": payload["decision"], "base_version": rev.base_version},
            )
            return {"review_decision_id": rev.id, "relation": self.relation_dto(r)}

    def _verify_relation(self, s, r):
        selected = set(
            s.scalars(select(Selection.entity_revision_id).where(Selection.analysis_set_id == r.analysis_set_id))
        )
        if r.from_revision_id not in selected or r.to_revision_id not in selected:
            raise DomainError("VKS_INVALID_EVIDENCE", "关系引用不属于当前基线")
        for eid in r.evidence_ids:
            ev = s.get(Evidence, eid)
            if not ev or ev.project_id != r.project_id or ev.analysis_set_id != r.analysis_set_id:
                raise DomainError("VKS_INVALID_EVIDENCE", "证据不属于当前项目和基线")
        if not r.evidence_ids:
            raise DomainError("VKS_INVALID_EVIDENCE", "关系缺少证据")
        fr = s.get(Revision, r.from_revision_id)
        to = s.get(Revision, r.to_revision_id)
        f = s.get(Entity, fr.entity_id)
        t = s.get(Entity, to.entity_id)
        a, b = f.type, t.type
        legal = {
            "documents": a != "section" and b == "section",
            "renders": a in ("page", "symbol") and b in ("action", "page"),
            "handler_of": a == "symbol" and b in ("action", "api"),
            "may_call": a in ("action", "symbol", "api") and b in ("api", "symbol"),
            "observed_during": a in ("action", "api") and b in ("action", "api"),
            "confirmed_trigger": a == "action" and b == "api",
            "implements": a in ("api", "symbol") and b in ("symbol", "api"),
            "maps_to_sql": a == "symbol" and b == "sql",
            "reads_table": a in ("sql", "symbol") and b == "table",
            "writes_table": a in ("sql", "symbol") and b == "table",
            "contains": a in ("document", "page", "scenario", "symbol")
            and (
                a != b
                or (
                    a == "symbol"
                    and fr.metadata_json.get("kind") in ("class", "interface")
                    and to.metadata_json.get("kind") in ("method", "constructor")
                    and f.id != t.id
                )
            ),
            "depends_on": f.id != t.id,
            "supersedes": a == b and f.id != t.id,
        }
        if not legal.get(r.relation_type, False):
            raise DomainError("VKS_INVALID_EVIDENCE", "关系端点类型不符合约束")

    def candidates(self, actor, aid):
        with self.db() as s:
            a = s.get(Analysis, aid)
            if not a:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            self.require(s, actor, a.project_id, "knowledge:edit")
            rows = []
            for r in s.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
                if not all(
                    self._visible_revision(s, actor, a.project_id, s.get(Revision, rid))
                    for rid in (r.from_revision_id, r.to_revision_id)
                ):
                    continue
                ids = [
                    eid
                    for eid in r.evidence_ids
                    if self._visible_evidence(s, actor, a.project_id, s.get(Evidence, eid))
                ]
                if ids:
                    rows.append(dict(self.relation_dto(r), evidence_ids=ids))
            return rows

    def propose_links(self, actor, pid, payload, key):
        with self.db() as s:
            self.require(s, actor, pid, "links:propose")
            a = s.get(Analysis, payload["analysis_set_id"])
            if not a or a.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            if a.state == "frozen":
                raise DomainError("VKS_STATE_CONFLICT", "已发布集合不可修改", 409)

            def create():
                rows = []
                for c in payload["candidates"]:
                    if c["relation_type"] not in RELATION_TYPES:
                        raise DomainError("VKS_INVALID_INPUT", "关系类型无效")
                    if c["causal_basis"] == "correlated_runtime" or c["relation_type"] == "confirmed_trigger":
                        raise DomainError("VKS_INVALID_EVIDENCE", "此轮没有经过服务器核验的运行因果证据")
                    r = s.scalar(
                        select(Relation).where(
                            Relation.analysis_set_id == a.id,
                            Relation.from_revision_id == c["from_revision_id"],
                            Relation.to_revision_id == c["to_revision_id"],
                            Relation.relation_type == c["relation_type"],
                        )
                    )
                    if not r:
                        r = Relation(
                            id=uid(),
                            project_id=pid,
                            analysis_set_id=a.id,
                            review_status="candidate",
                            validity_status="valid",
                            row_version=1,
                            **c,
                        )
                        self._verify_relation(s, r)
                        s.add(r)
                    if not all(
                        self._visible_revision(s, actor, pid, s.get(Revision, rid))
                        for rid in (r.from_revision_id, r.to_revision_id)
                    ):
                        raise DomainError("VKS_NOT_FOUND", "未找到可访问的关系端点", 404)
                    if not all(self._visible_evidence(s, actor, pid, s.get(Evidence, eid)) for eid in r.evidence_ids):
                        raise DomainError("VKS_NOT_FOUND", "未找到可访问的关系证据", 404)
                    rows.append(self.relation_dto(r))
                a.selection_version += 1
                self.audit(s, actor, pid, "links.propose", a.id)
                return {"review_batch_id": uid(), "candidates": rows}

            return self.idem(s, actor, pid, "propose_links", key, payload, create)

    def _manifest(self, s, a):
        selections = [
            {"entity_id": r.entity_id, "entity_revision_id": r.id, "content_sha256": r.content_sha256}
            for r in self._revisions(s, a.id)
        ]
        selections.sort(key=lambda x: x["entity_id"])
        relations = [
            self.relation_dto(r)
            for r in s.scalars(
                select(Relation).where(Relation.analysis_set_id == a.id, Relation.review_status != "rejected")
            )
        ]
        relations.sort(key=lambda x: x["id"])
        return {
            "schema_version": "1.0",
            "analysis_set_id": a.id,
            "project_id": a.project_id,
            "selection_version": a.selection_version,
            "sources": a.sources,
            "selections": selections,
            "relations": relations,
            "runtime_alignment": a.runtime_alignment,
            "ruleset_version": a.ruleset_version,
        }

    def request_release(self, actor, pid, payload, key):
        with self.db() as s:
            self.require(s, actor, pid, "links:propose")
            a = s.get(Analysis, payload["analysis_set_id"])
            if not a or a.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            if a.state == "frozen":
                raise DomainError("VKS_STATE_CONFLICT", "该基线已发布", 409)

            def create():
                manifest = self._manifest(s, a)
                from .documents import DocumentService
                from .versions import VersionService
                from .scenarios import ScenarioService

                blockers = (
                    DocumentService.assert_publishable(s, a)
                    + VersionService.assert_publishable(s, a)
                    + ScenarioService(self).assert_publishable(s, a)
                )
                if not all(self._visible_revision(s, actor, pid, r) for r in self._revisions(s, a.id)):
                    raise DomainError("VKS_FORBIDDEN", "需要读取全部待发布材料的权限", 403)
                if not manifest["selections"]:
                    blockers.append("分析集合没有知识实体")
                for r in s.scalars(
                    select(Relation).where(Relation.analysis_set_id == a.id, Relation.review_status != "rejected")
                ):
                    if r.review_status != "confirmed":
                        blockers.append(f"关系 {r.id} 尚未人工审核")
                    if r.validity_status != "valid":
                        blockers.append(f"关系 {r.id} 的证据需要复核")
                    try:
                        self._verify_relation(s, r)
                    except DomainError:
                        blockers.append(f"关系 {r.id} 证据不完整")
                j = self._new_job(s, actor, pid, "release_precheck", {"analysis_set_id": a.id})
                j.state = "succeeded"
                j.stage = "COMPLETE"
                j.progress = 100
                j.completeness = "complete"
                j.result_json = {"blockers": blockers, "manifest_sha256": sha(canonical(manifest))}
                c = Candidate(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=a.id,
                    manifest_sha256=sha(canonical(manifest)),
                    manifest_json=manifest,
                    blockers=blockers,
                    job_id=j.id,
                    created_by=actor.id,
                )
                s.add(c)
                s.flush()
                return {
                    "candidate_id": c.id,
                    "precheck_job": self.job_dto(j),
                    "review_url": f"/?project={pid}&page=versions&candidate={c.id}",
                    "published": False,
                    "manifest_sha256": c.manifest_sha256,
                    "blockers": blockers,
                }

            return self.idem(s, actor, pid, "request_release", key, payload, create)

    def confirm_release(self, actor, cid):
        with self.db() as s:
            c = s.get(Candidate, cid)
            if not c:
                raise DomainError("VKS_NOT_FOUND", "未找到发布预检", 404)
            self.require(s, actor, c.project_id, "release:publish")
            s.refresh(c)
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "需要人员会话确认发布", 403)
            a = s.get(Analysis, c.analysis_set_id)
            if c.blockers:
                raise DomainError("VKS_STATE_CONFLICT", "请先处理发布阻断项", 409, {"blockers": c.blockers})
            if sha(canonical(self._manifest(s, a))) != c.manifest_sha256:
                raise DomainError("VKS_VERSION_CONFLICT", "预检后素材发生变化，请重新预检", 412)
            confirmation = uid()
            c.confirmation_hash = sha(confirmation)
            c.confirmation_actor = actor.id
            c.confirmation_expires = future(300)
            self.audit(s, actor, c.project_id, "release.confirmation", c.id)
            return {
                "human_confirmation_ref": confirmation,
                "expected_manifest_sha256": c.manifest_sha256,
                "expires_at": c.confirmation_expires,
            }

    def publish(self, actor, cid, payload, key):
        with self.db() as s:
            c = s.get(Candidate, cid)
            if not c:
                raise DomainError("VKS_NOT_FOUND", "未找到发布预检", 404)
            p = self.require(s, actor, c.project_id, "release:publish")
            s.refresh(c)
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "需要人员会话发布", 403)

            def do_publish():
                a = s.get(Analysis, c.analysis_set_id)
                if a.state == "frozen":
                    raise DomainError("VKS_STATE_CONFLICT", "该基线已发布", 409)
                from .documents import DocumentService
                from .versions import VersionService
                from .scenarios import ScenarioService

                blockers = (
                    c.blockers
                    + DocumentService.assert_publishable(s, a)
                    + VersionService.assert_publishable(s, a)
                    + ScenarioService(self).assert_publishable(s, a)
                )
                if blockers:
                    raise DomainError("VKS_STATE_CONFLICT", "存在发布阻断项", 409, {"blockers": blockers})
                if (
                    payload["expected_manifest_sha256"] != c.manifest_sha256
                    or sha(canonical(self._manifest(s, a))) != c.manifest_sha256
                ):
                    raise DomainError("VKS_VERSION_CONFLICT", "预检内容已变化，请重新审核", 412)
                if (
                    c.confirmation_actor != actor.id
                    or not c.confirmation_hash
                    or c.confirmation_expires < now()
                    or not secrets.compare_digest(c.confirmation_hash, sha(payload["human_confirmation_ref"]))
                ):
                    raise DomainError("VKS_APPROVAL_REQUIRED", "人员确认票据无效或过期", 403)
                r = Release(
                    id=uid(),
                    project_id=p.id,
                    analysis_set_id=a.id,
                    release_no=a.name,
                    manifest_sha256=c.manifest_sha256,
                    manifest_json=c.manifest_json,
                    published_by=actor.id,
                    change_note=payload["change_note"],
                )
                s.add(r)
                s.flush()
                a.state = "frozen"
                p.current_release_id = r.id
                p.row_version += 1
                c.confirmation_hash = None
                self.audit(s, actor, p.id, "release.publish", r.id)
                return self.release_dto(r)

            return self.idem(s, actor, p.id, "publish:" + cid, key, payload, do_publish)

    def releases(self, actor, pid):
        with self.db() as s:
            self.require(s, actor, pid)
            return [
                self.release_dto(r)
                for r in s.scalars(select(Release).where(Release.project_id == pid).order_by(Release.created_at.desc()))
            ]

    def evidence_project(self, actor, eid):
        with self.db() as s:
            e = s.get(Evidence, eid)
            if not e:
                raise DomainError("VKS_NOT_FOUND", "未找到证据", 404)
            self.require(s, actor, e.project_id)
            return e.project_id

    def get_evidence(self, actor, pid, ids, max_chars=8000):
        with self.db() as s:
            self.require(s, actor, pid)
            rows = []
            for eid in ids:
                e = s.get(Evidence, eid)
                if not e or e.project_id != pid or not self._visible_evidence(s, actor, pid, e):
                    raise DomainError("VKS_NOT_FOUND", "未找到证据", 404)
                if not self._can(s, actor, pid, "knowledge:edit") and not s.scalar(
                    select(Release).where(Release.analysis_set_id == e.analysis_set_id)
                ):
                    raise DomainError("VKS_NOT_FOUND", "未找到证据", 404)
                rows.append(self.evidence_dto(e, max_chars))
                self.audit(s, actor, pid, "evidence.read", e.id)
            return rows

    def graph(
        self,
        actor,
        pid,
        eid,
        analysis_set_id=None,
        release_id=None,
        depth=2,
        max_nodes=50,
        relation_types=None,
        extended=False,
    ):
        from .reading import ReadingService

        return ReadingService(self).graph(
            actor, pid, eid, analysis_set_id, release_id, depth, max_nodes, relation_types, extended
        )

    def action_context(self, actor, pid, eid, analysis_set_id=None, release_id=None, depth=2, max_nodes=50):
        from .reading import ReadingService

        return ReadingService(self).action_context(actor, pid, eid, analysis_set_id, release_id, depth, max_nodes)

    def find_entity_project(self, actor, eid):
        with self.db() as s:
            e = s.get(Entity, eid)
            if not e:
                r = s.get(Revision, eid)
                e = s.get(Entity, r.entity_id) if r else None
            if not e:
                raise DomainError("VKS_NOT_FOUND", "未找到对象", 404)
            self.require(s, actor, e.project_id)
            return e.project_id

    def search(self, actor, pid, payload):
        from .reading import ReadingService

        return ReadingService(self).search(actor, pid, payload)

    def question(self, actor, pid, payload):
        if payload.get("use_model"):
            from .ai_gateway import AiService

            return AiService(self).run(
                actor,
                pid,
                payload["question"],
                analysis_set_id=payload.get("analysis_set_id"),
                release_id=payload.get("release_id"),
                action_id=payload.get("action_id"),
            )
        self.get_project(actor, pid)
        results = self.search(
            actor,
            pid,
            {
                "query": payload["question"],
                "release_id": payload.get("release_id"),
                "analysis_set_id": payload.get("analysis_set_id"),
                "limit": 5,
            },
        )
        refs = sorted({eid for r in results for eid in r["evidence_ids"]})
        answer = (
            "\n\n".join(f"{r['entity']['name']}：{r['excerpt']}" for r in results)
            if results
            else "当前基线没有与问题精确匹配的材料，请补充相关 Markdown、API 或明确的操作上下文。"
        )
        return {
            "answer": answer,
            "evidence_ids": refs,
            "unknowns": ["未配置平台模型，本次为已有知识检索结果；没有生成业务推断"],
            "model_id": "none:deterministic-retrieval",
        }

    def get_document(self, actor, eid, rid):
        from .documents import DocumentService

        return DocumentService(self).read_revision(actor, eid, rid)

    def save_document(self, actor, eid, payload, version):
        from .documents import DocumentService

        return DocumentService(self).save_draft(actor, eid, payload, version)

    def create_analysis(self, actor, pid, payload, key):
        with self.db() as s:
            self.require(s, actor, pid, "analysis:run")

            def create():
                name = payload["name"]
                a = Analysis(
                    id=uid(),
                    project_id=pid,
                    key="draft-" + uid(),
                    name=name,
                    state="draft",
                    sources=payload.get("sources", []),
                    ruleset_version=payload.get("ruleset_version", "explicit-binding-v1"),
                    selection_version=1,
                    created_by=actor.id,
                )
                for source in a.sources:
                    if not any(
                        source.get("source_snapshot_id") == existing.get("source_snapshot_id")
                        for prev in s.scalars(select(Analysis).where(Analysis.project_id == pid))
                        for existing in prev.sources
                    ):
                        raise DomainError("VKS_INVALID_INPUT", "来源快照不属于此项目")
                s.add(a)
                self.audit(s, actor, pid, "analysis.create", a.id)
                return self.analysis_dto(a)

            return self.idem(s, actor, pid, "analysis.create", key, payload, create)

    def create_analysis_job(self, actor, aid, payload, key):
        with self.db() as s:
            a = s.get(Analysis, aid)
            if not a:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            self.require(s, actor, a.project_id, "analysis:run")
            if a.state == "frozen" and payload["kind"] != "detect_drift":
                raise DomainError("VKS_STATE_CONFLICT", "已发布基线不可分析写入", 409)
            if payload["kind"] in ("index", "suggest_links"):
                self.require(s, actor, a.project_id, "source:read")
            if payload["kind"] == "generate_documents":
                from .ai_gateway import ModelPolicy

                self.require(s, actor, a.project_id, "knowledge:edit")
                policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == a.project_id))
                if not policy or not policy.settings.get("enabled"):
                    raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "平台模型未配置或未启用", 503)
            payload = dict(payload, analysis_set_id=aid)
            return self.idem(
                s,
                actor,
                a.project_id,
                "analysis.job",
                key,
                payload,
                lambda: self.job_dto(self._new_job(s, actor, a.project_id, payload["kind"], payload)),
            )

    def _diff(self, s, pid, from_release_id, to_analysis_set_id):
        from .versions import VersionService

        return VersionService(self).compare(s, pid, from_release_id, to_analysis_set_id)

    def diff(self, actor, pid, from_id, to_id):
        from .versions import VersionService

        with self.db() as s:
            self.require(s, actor, pid)
            a, _ = self._selected(s, actor, pid, to_id, None)
            result = self._diff(s, pid, from_id, a.id)
        return VersionService(self).public_report(actor, pid, result)

    def _filter_diff(self, s, actor, pid, result):
        changes = []
        for change in result.get("changes", []):
            revisions = [s.get(Revision, change[k]) for k in ("old_revision_id", "new_revision_id") if k in change]
            if revisions and all(r and self._visible_revision(s, actor, pid, r) for r in revisions):
                changes.append(change)
        relations = []
        for relation in result.get("impacted_relations", []):
            endpoints = [s.get(Revision, relation[k]) for k in ("from_revision_id", "to_revision_id")]
            if not all(r and self._visible_revision(s, actor, pid, r) for r in endpoints):
                continue
            evidence_ids = [
                eid
                for eid in relation["evidence_ids"]
                if (ev := s.get(Evidence, eid)) and self._visible_evidence(s, actor, pid, ev)
            ]
            if evidence_ids:
                relations.append(dict(relation, evidence_ids=evidence_ids))
        return dict(result, changes=changes, impacted_relations=relations)

    def project_context(self, actor, pid, release_id):
        from .modules import ModuleService

        w = self.workspace(actor, pid, release_id=release_id)
        return {
            "project": w["project"],
            "release": w["selected_release"],
            "sources": w["sources"],
            "modules": ModuleService(self).context(actor, pid, release_id),
            "capabilities": w["capabilities"],
            "unknowns": w["unknowns"],
        }

    def create_token(self, actor, pid, payload):
        return team_identity.create_token(self, actor, pid, payload)

    def token_dto(self, c):
        return {
            "id": c.id,
            "name": c.name,
            "scopes": c.scopes,
            "project_id": c.project_id,
            "expires_at": c.expires_at,
            "revoked": c.revoked,
            "created_at": c.created_at,
        }

    def tokens(self, actor, pid):
        with self.db() as s:
            self.require(s, actor, pid, "project:manage")
            return [
                self.token_dto(c)
                for c in s.scalars(
                    select(Credential)
                    .where(Credential.project_id == pid, Credential.kind == "token")
                    .order_by(Credential.created_at.desc())
                )
            ]

    def revoke_token(self, actor, pid, tid):
        with self.db() as s:
            self.require(s, actor, pid, "project:manage")
            c = s.get(Credential, tid)
            if not c or c.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到令牌", 404)
            c.revoked = True
            self.audit(s, actor, pid, "token.revoke", tid)
            return self.token_dto(c)

    def audit_dto(self, e):
        return {
            "id": e.id,
            "actor_id": e.actor_id,
            "action": e.action,
            "resource_id": e.resource_id,
            "outcome": e.outcome,
            "created_at": e.created_at,
        }

    def audit_events(self, actor, pid, filters=None):
        from datetime import datetime, timezone

        filters = filters or {}
        with self.db() as s:
            self.require(s, actor, pid, "audit:read")
            query = select(Audit).where(Audit.project_id == pid)
            for key in ("actor_id", "action", "resource_id", "outcome"):
                if filters.get(key):
                    query = query.where(getattr(Audit, key) == filters[key])
            for key, op in (("since", "after"), ("until", "before")):
                if filters.get(key):
                    try:
                        value = datetime.fromisoformat(str(filters[key]).replace("Z", "+00:00"))
                        if value.tzinfo is None:
                            raise ValueError()
                        value = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    except (ValueError, TypeError):
                        raise DomainError("VKS_INVALID_INPUT", "审计时间需要带时区的ISO8601格式") from None
                    query = query.where(Audit.created_at >= value if op == "after" else Audit.created_at <= value)
            if filters.get("cursor"):
                last = s.get(Audit, filters["cursor"])
                if not last or last.project_id != pid:
                    raise DomainError("VKS_INVALID_INPUT", "审计分页游标无效")
                query = query.where(
                    (Audit.created_at < last.created_at)
                    | ((Audit.created_at == last.created_at) & (Audit.id < last.id))
                )
            limit = filters.get("limit", 100)
            if not isinstance(limit, int) or not 1 <= limit <= 500:
                raise DomainError("VKS_INVALID_INPUT", "审计单页限制为1–500")
            return [
                self.audit_dto(e)
                for e in s.scalars(query.order_by(Audit.created_at.desc(), Audit.id.desc()).limit(limit))
            ]

    def members(self, actor, pid):
        return team_identity.list_members(self, actor, pid)

    def set_member(self, actor, pid, payload, version):
        return team_identity.set_member(self, actor, pid, payload, version)

    def export(self, actor, pid, payload, key):
        with self.db() as s:
            self.require(s, actor, pid, "knowledge:export")
            r = s.get(Release, payload["release_id"])
            if not r or r.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到发布版", 404)
            if payload["format"] not in ("markdown_bundle", "pdf", "vksbundle"):
                raise DomainError("VKS_INVALID_INPUT", "不支持的导出格式")
            if payload.get("include_source"):
                self.require(s, actor, pid, "source:read")
            return self.idem(
                s,
                actor,
                pid,
                "export",
                key,
                payload,
                lambda: self.job_dto(
                    self._new_job(
                        s,
                        actor,
                        pid,
                        "export",
                        dict(
                            payload,
                            allowed_source=payload.get("include_source", False),
                            allowed_confidential=self._can(s, actor, pid, "source:read"),
                            allowed_trace=self._can(s, actor, pid, "trace:read"),
                        ),
                    )
                ),
            )

    def _perform_export(self, s, j):
        from .exports import perform_export

        return perform_export(self, s, j)

    def download_export(self, actor, pid, jid):
        with self.db() as s:
            self.require(s, actor, pid, "knowledge:export")
            j = s.get(Job, jid)
            if not j or j.project_id != pid or j.kind != "export" or j.state != "succeeded" or j.created_by != actor.id:
                raise DomainError("VKS_NOT_FOUND", "未找到导出结果", 404)
            if j.input_json.get("allowed_source") or j.input_json.get("allowed_confidential"):
                self.require(s, actor, pid, "source:read")
            if j.input_json.get("allowed_trace"):
                self.require(s, actor, pid, "trace:read")
            self.audit(s, actor, pid, "export.download", jid)
            return (self.data_dir / "objects" / j.result_json["object_key"]).read_bytes(), j.result_json["filename"]

    def artifact_bytes(self, actor, pid, aid):
        with self.db() as s:
            self.require(s, actor, pid)
            a = s.get(Artifact, aid)
            if not a or a.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到文件", 404)
            if a.kind == "source" or a.sensitivity == "confidential" or a.metadata_json.get("requires_source_read"):
                self.require(s, actor, pid, "source:read")
            if a.kind in ("trace", "capture") or a.metadata_json.get("requires_trace_read"):
                self.require(s, actor, pid, "trace:read")
            if not self._can(s, actor, pid, "knowledge:edit"):
                aids = {
                    s.get(Revision, x["entity_revision_id"]).artifact_id
                    for r in s.scalars(select(Release).where(Release.project_id == pid))
                    for x in r.manifest_json["selections"]
                }
                if aid not in aids:
                    raise DomainError("VKS_NOT_FOUND", "未找到文件", 404)
            self.audit(s, actor, pid, "artifact.download", aid)
            return (self.data_dir / "objects" / a.object_key).read_bytes(), Path(a.relative_path).name

    def scenario(self, actor, pid, sid, rid):
        with self.db() as s:
            self.require(s, actor, pid)
            r = s.get(Revision, rid)
            e = s.get(Entity, sid)
            if not r or not e or r.entity_id != sid or e.project_id != pid or e.type != "scenario":
                raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            if not self._can(s, actor, pid, "knowledge:edit"):
                if not any(
                    any(x["entity_revision_id"] == rid for x in rel.manifest_json["selections"])
                    for rel in s.scalars(select(Release).where(Release.project_id == pid))
                ):
                    raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            return {
                "scenario_id": sid,
                "scenario_revision_id": r.id,
                "entity_revision_id": r.id,
                "revision_no": 1,
                "content_sha256": r.content_sha256,
                "created_at": r.created_at,
                "dsl": r.metadata_json.get("dsl", {}),
            }
