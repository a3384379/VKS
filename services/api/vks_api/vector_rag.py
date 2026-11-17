"""Rebuildable local vector database and permission-scoped hybrid RAG.

SQLite stores real normalized float32 embeddings, immutable chunk identities and
query vectors. Native matrix scoring shortlists candidates, then the original
math.sumprod scoring fixes exact order and ties. Knowledge remains in VKS.
"""

import json
import math
import os
import sqlite3
import sys
import threading
import time
import weakref
from array import array
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager, nullcontext
from contextvars import copy_context
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, Request
from pydantic import Field, model_validator
from sqlalchemy import select

from . import ai_gateway
from .ai_gateway import ModelNotSent, failure
from .model_connections import ModelConnection
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref
from .service import Actor, DomainError, canonical, sha

_BUILD_LEASE_SECONDS = 30
_BUILD_HEARTBEAT_SECONDS = 5
_BUILD_POLL_SECONDS = 0.1


class RagSettings(Input):
    enabled: bool = True
    connection_id: str | None = Field(default=None, max_length=96)
    model_id: str = Field(default="text-embedding-3-small", min_length=1, max_length=160)
    dimensions: int = Field(default=512, ge=64, le=3072)
    candidate_limit: int = Field(default=24, ge=6, le=64)
    per_document_limit: int = Field(default=8, ge=1, le=24)
    daily_token_limit: int = Field(default=10000000, ge=10000, le=100000000, deprecated=True)

    @model_validator(mode="after")
    def supported_dimensions(self):
        if self.model_id == "text-embedding-3-small" and self.dimensions > 1536:
            raise ValueError("text-embedding-3-small 的维度不能超过 1536")
        return self


class DocumentIndexInput(Input):
    analysis_set_id: Ref | None = None
    release_id: Ref | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        if bool(self.analysis_set_id) == bool(self.release_id):
            raise ValueError("请选择准确的草稿或快照")
        return self


def normalize(vector, dimensions):
    if (
        not isinstance(vector, list)
        or len(vector) != dimensions
        or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
    ):
        failure("VKS_INVALID_INPUT", "嵌入服务返回了无效维度或数值")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0:
        failure("VKS_INVALID_INPUT", "嵌入向量没有有效长度")
    return array("f", (x / norm for x in vector)).tobytes()


def vector(blob):
    result = array("f")
    result.frombytes(blob)
    return result


class VectorStore:
    def __init__(self, data_dir):
        self.path = data_dir / "rag-vectors.sqlite"
        self.lock = threading.RLock()
        if self.path.is_symlink():
            failure("VKS_INVALID_INPUT", "向量库路径不能是符号链接")
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config(project TEXT PRIMARY KEY, settings TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS status(project TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks(project TEXT, space TEXT, id TEXT, revision TEXT, start INTEGER, content TEXT, embedding BLOB, PRIMARY KEY(project,space,id));
                CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(project,space,revision);
                CREATE TABLE IF NOT EXISTS queries(project TEXT, space TEXT, id TEXT, embedding BLOB, PRIMARY KEY(project,space,id));
                CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,project TEXT,day TEXT,model TEXT,reserved INTEGER,used INTEGER,state TEXT);
                CREATE TABLE IF NOT EXISTS index_build(project TEXT, space TEXT, token TEXT NOT NULL, lease_until REAL NOT NULL, PRIMARY KEY(project,space));
                CREATE TABLE IF NOT EXISTS chunk_epoch(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
                INSERT OR IGNORE INTO chunk_epoch VALUES(1,0);
                CREATE TRIGGER IF NOT EXISTS chunks_insert_epoch AFTER INSERT ON chunks BEGIN UPDATE chunk_epoch SET value=value+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS chunks_update_epoch AFTER UPDATE ON chunks BEGIN UPDATE chunk_epoch SET value=value+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS chunks_delete_epoch AFTER DELETE ON chunks BEGIN UPDATE chunk_epoch SET value=value+1 WHERE id=1; END;
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        # Connection.__exit__ commits/rolls back but does not close the SQLite
        # connection. Explicit closing keeps frequent lookups from retaining FDs.
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            yield db

    def settings(self, pid):
        from .inference_pipeline import READ_SESSION, WRITE_SESSION

        current = WRITE_SESSION.get() or READ_SESSION.get()
        session = current[1] if current and current[0].data_dir / "rag-vectors.sqlite" == self.path else None
        scoped = session is not None and (
            session.info.get("vks_read_batch") or session.info.get("vks_completion_batch")
        )
        cached = session.info.setdefault("vks_vector_settings", {}) if scoped else {}
        if pid in cached:
            return cached[pid].model_copy(deep=True)
        with self.db() as db:
            row = db.execute("SELECT settings FROM config WHERE project=?", (pid,)).fetchone()
        result = RagSettings.model_validate_json(row[0]) if row else RagSettings(enabled=False)
        cached[pid] = result
        return result.model_copy(deep=True)

    def batch_reads(self):
        from .inference_pipeline import READ_SESSION, WRITE_SESSION

        current = READ_SESSION.get() or WRITE_SESSION.get()
        if current and current[0].data_dir / "rag-vectors.sqlite" == self.path:
            session = current[1]
            if session.info.get("vks_read_batch") or session.info.get("vks_completion_batch"):
                return session.info.setdefault("vks_vector_reads", {})
        return {}

    def query_vector(self, pid, space, key):
        cached = self.batch_reads()
        identity = ("query", pid, space, key)
        if identity not in cached:
            with self.db() as db:
                row = db.execute(
                    "SELECT embedding FROM queries WHERE project=? AND space=? AND id=?", (pid, space, key)
                ).fetchone()
            cached[identity] = row[0] if row else None
        return cached[identity]

    def query_vectors(self, pid, space, keys):
        cached = self.batch_reads()
        missing = [key for key in keys if ("query", pid, space, key) not in cached]
        if missing:
            with self.db() as db:
                rows = db.execute(
                    "SELECT id,embedding FROM queries WHERE project=? AND space=? "
                    "AND id IN (SELECT value FROM json_each(?))",
                    (pid, space, json.dumps(missing)),
                ).fetchall()
            cached.update({("query", pid, space, key): None for key in missing})
            cached.update({("query", pid, space, key): value for key, value in rows})

    def state(self, pid, data=None):
        with self.db() as db:
            if data is not None:
                db.execute("INSERT OR REPLACE INTO status VALUES (?,?)", (pid, canonical(data)))
            row = db.execute("SELECT data FROM status WHERE project=?", (pid,)).fetchone()
            result = json.loads(row[0]) if row else {"state": "empty", "completed": 0, "total": 0, "error": ""}
            result.pop("_index_writer", None)
            return result

    def index_state(self, pid, data, token, *, space=None):
        """A late failed owner cannot replace a newer builder's ready status."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if space is not None:
                self.index_owner(db, pid, space, token)
            else:
                row = db.execute("SELECT data FROM status WHERE project=?", (pid,)).fetchone()
                if not row or json.loads(row[0]).get("_index_writer") != token:
                    return
            db.execute("INSERT OR REPLACE INTO status VALUES (?,?)", (pid, canonical({**data, "_index_writer": token})))

    @staticmethod
    def index_owner(db, pid, space, token):
        row = db.execute(
            "SELECT token,lease_until FROM index_build WHERE project=? AND space=?", (pid, space)
        ).fetchone()
        if not row or row[0] != token or row[1] <= time.time():
            failure("VKS_CANCELLED", "文档索引执行资格已失效，请重试", 409)

    def reserve(self, pid, settings, bound):
        from .models import uid

        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        key = uid()
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?)", (key, pid, day, settings.model_id, bound, 0, "reserved")
            )
        return key

    def settle(self, key, used, state):
        with self.db() as db:
            row = db.execute("SELECT reserved FROM usage WHERE id=? AND state=?", (key, "reserved")).fetchone()
            if row:
                db.execute(
                    "UPDATE usage SET reserved=0,used=?,state=? WHERE id=?",
                    (row[0] if used is None else used, state, key),
                )

    def count(self, pid, space, revisions):
        with self.db() as db:
            return db.execute(
                "SELECT COUNT(*),COUNT(DISTINCT revision) FROM chunks WHERE project=? AND space=? AND embedding IS NOT NULL AND revision IN (SELECT value FROM json_each(?))",
                (pid, space, json.dumps(list(revisions))),
            ).fetchone()

    def has_vectors(self, pid, space, revisions):
        # Inference needs existence, not two complete COUNT aggregations over
        # every visible chunk. Keep count() for the actual status statistics.
        revisions = tuple(sorted(revisions))
        cached = self.batch_reads()
        key = ("has", pid, space, revisions)
        if key in cached:
            return cached[key]
        with self.db() as db:
            result = (
                db.execute(
                    "SELECT 1 FROM chunks WHERE project=? AND space=? AND embedding IS NOT NULL "
                    "AND revision IN (SELECT value FROM json_each(?)) LIMIT 1",
                    (pid, space, json.dumps(list(revisions))),
                ).fetchone()
                is not None
            )
        cached[key] = result
        return result

    @contextmanager
    def index_build(self, pid, space, validate):
        """Lease cold-index ownership without holding a transaction over I/O.

        The small token-fenced record also coordinates separate Service/Celery
        processes sharing this rebuildable vector store. A dead owner expires;
        a delayed old response must still prove its token before committing.
        """
        from .models import uid

        token, stopped, lost = uid(), threading.Event(), threading.Event()
        while True:
            validate()
            current = time.time()
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO index_build VALUES (?,?,?,?) ON CONFLICT(project,space) DO UPDATE SET "
                    "token=excluded.token,lease_until=excluded.lease_until WHERE index_build.lease_until<=?",
                    (pid, space, token, current + _BUILD_LEASE_SECONDS, current),
                )
                row = db.execute("SELECT token FROM index_build WHERE project=? AND space=?", (pid, space)).fetchone()
            if row and row[0] == token:
                break
            stopped.wait(_BUILD_POLL_SECONDS)

        def held():
            with self.db() as db:
                self.index_owner(db, pid, space, token)
            if lost.is_set():
                failure("VKS_CANCELLED", "文档索引执行资格已失效，请重试", 409)

        def renew():
            while not stopped.wait(_BUILD_HEARTBEAT_SECONDS):
                try:
                    with self.db() as db:
                        current = time.time()
                        changed = db.execute(
                            "UPDATE index_build SET lease_until=? WHERE project=? AND space=? AND token=? "
                            "AND lease_until>?",
                            (current + _BUILD_LEASE_SECONDS, pid, space, token, current),
                        ).rowcount
                    if not changed:
                        lost.set()
                        return
                except sqlite3.Error:
                    lost.set()
                    return

        thread = threading.Thread(target=renew, name="vks-vector-index-lease", daemon=True)
        thread.start()
        try:
            held()
            validate()
            yield held, token
        finally:
            stopped.set()
            thread.join(timeout=1)
            try:
                with self.db() as db:
                    db.execute("DELETE FROM index_build WHERE project=? AND space=? AND token=?", (pid, space, token))
            except sqlite3.Error:
                # A failed cleanup cannot make a new owner unsafe: this token
                # expires, and the next owner always rechecks current scope.
                pass


def split(chapters):
    for row in chapters:
        text = row["content"]
        for start in range(0, max(1, len(text)), 780):
            content = text[start : start + 900]
            full = row["document_name"][:160] + "\n" + row["name"][:160] + "\n" + content
            yield {
                "id": sha(canonical([row["revision_id"], start, full])),
                "revision": row["revision_id"],
                "start": start,
                "content": content,
                "input": full,
            }
            if start + 900 >= len(text):
                break


class VectorRag:
    def __init__(self, service, assistant):
        self.service, self.assistant = service, assistant
        self.store = VectorStore(service.data_dir)
        # Each page job creates its own assistant. Share only synchronization
        # facts on the Service, never authorization, source data or results.
        with service.lock:
            shared = getattr(service, "_rag_index_sync", None)
            if shared is None:
                shared = (threading.RLock(), weakref.WeakValueDictionary())
                service._rag_index_sync = shared
            from .vector_search import ExactVectorSearch

            self.vector_search = service.__dict__.setdefault("_rag_vector_search", ExactVectorSearch())
        self.lock, self.export_locks = shared
        self.running = {}
        self.query_locks = weakref.WeakValueDictionary()

    @contextmanager
    def _index_build_lock(self, pid, space, validate):
        with self.lock:
            lock = self.export_locks.setdefault((pid, space), threading.Lock())
        while not lock.acquire(timeout=_BUILD_POLL_SECONDS):
            validate()
        try:
            validate()
            with self.store.index_build(pid, space, validate) as ownership:
                yield ownership
        finally:
            lock.release()

    def document_scope(
        self,
        actor,
        pid,
        *,
        analysis_set_id=None,
        release_id=None,
        section_revision_ids=None,
        permission="knowledge:export",
    ):
        """Exact readable document corpus, independent of any prototype package.

        This is deliberately resolved again around network calls. A caller's
        supplied revision IDs restrict the scope; they never grant permission or
        select historical/unmounted revisions on their own.
        """

        requested = None if section_revision_ids is None else set(section_revision_ids)
        with self.assistant.links.reader.reading.db() as s:
            from .input_versions import cached_projection
            from .jobs import PERSONAL_TASK
            from .models import Analysis

            analysis = s.get(Analysis, analysis_set_id) if analysis_set_id and not release_id else None
            if (
                self.service.personal_mode
                and PERSONAL_TASK.get() == (self.service, pid)
                and analysis is not None
                and analysis.project_id == pid
                and analysis.state == "draft"
            ):
                key = (
                    "personal-document-scope",
                    pid,
                    analysis_set_id,
                    None if requested is None else tuple(sorted(requested)),
                    permission,
                    analysis.selection_version if permission != "knowledge:edit" else None,
                )
                facts = cached_projection(
                    self.service,
                    s,
                    key,
                    lambda: self._document_scope_rows(
                        s, actor, pid, analysis_set_id, release_id, requested, permission
                    ),
                )
                from copy import deepcopy

                return deepcopy(facts)
            return self._document_scope_rows(s, actor, pid, analysis_set_id, release_id, requested, permission)

    def _document_scope_rows(self, s, actor, pid, analysis_set_id, release_id, requested, permission):
        from .models import Artifact
        from .reading import artifact_policy

        self.service.require(s, actor, pid, permission)
        analysis, release, choices, _ = self.assistant.links.reader.scope(s, actor, pid, analysis_set_id, release_id)
        from .corpus_cache import corpus_rows

        rows = corpus_rows(self.assistant, s, actor, pid, choices, analysis_id=analysis.id if not release else None)
        selected = {r.id for e, r in rows if e.type == "section"}
        policies, chapters, provenance = {}, [], {}
        for entity, document in rows:
            if entity.type != "document":
                continue
            for chapter in self.assistant.links.documents._sections(s, entity, document):
                if chapter.id not in selected or (requested is not None and chapter.id not in requested):
                    continue
                if chapter.artifact_id not in policies:
                    art = s.get(Artifact, chapter.artifact_id)
                    policy = artifact_policy(s, art)
                    policies[chapter.artifact_id] = {
                        "sensitivity": art.sensitivity if art else "confidential",
                        "requires_source_read": bool(policy.get("requires_source_read")),
                        "requires_trace_read": bool(policy.get("requires_trace_read")),
                    }
                chapters.append(
                    {
                        "revision_id": chapter.id,
                        "name": chapter.name,
                        "content": chapter.content,
                        "document_revision_id": document.id,
                        "document_name": document.name,
                    }
                )
                provenance[chapter.id] = {
                    "sha": chapter.content_sha256,
                    "document": document.id,
                    "artifact": chapter.artifact_id,
                    "policy": policies[chapter.artifact_id],
                }
        if requested is not None and requested != {r["revision_id"] for r in chapters}:
            failure("VKS_NOT_FOUND", "导出章节不在当前可读取版本中", 404)
        fingerprint = sha(
            canonical(
                {
                    "analysis": analysis.id,
                    "state": analysis.state,
                    # Background association writes advance the draft
                    # counter without changing any document input. Exact
                    # export projections still bind that counter.
                    "version": analysis.selection_version if permission != "knowledge:edit" else None,
                    "release": release.id if release else None,
                    "chapters": provenance,
                }
            )
        )
        return chapters, fingerprint, provenance

    def _export_vectors(self, pid, space, chunks, dimensions, *, encode=True):
        """Project only exact expected chunks; never copy the shared SQLite DB."""
        expected = {c["id"]: c for c in chunks}
        result = {}
        with self.store.db() as db:
            db.execute("CREATE TEMP TABLE allowed(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO allowed VALUES (?)", ((cid,) for cid in expected))
            rows = db.execute(
                "SELECT id,revision,start,content,embedding FROM chunks "
                "WHERE project=? AND space=? AND id IN (SELECT id FROM allowed)",
                (pid, space),
            )
            for cid, revision, start, content, blob in rows:
                row = expected[cid]
                if (
                    not isinstance(blob, bytes)
                    or len(blob) != dimensions * 4
                    or revision != row["revision"]
                    or start != row["start"]
                    or content != row["content"]
                ):
                    continue
                values = vector(blob)
                norm = math.hypot(*values)
                if not math.isfinite(norm) or not 0.999 <= norm <= 1.001:
                    continue
                if not encode:
                    result[cid] = True
                    continue
                # Browser readers decode a declared little-endian Float32
                # representation, independent of the publisher's CPU.
                if sys.byteorder != "little":
                    values.byteswap()
                    blob = values.tobytes()
                result[cid] = b64encode(blob).decode("ascii")
        return result

    def export_index(
        self,
        actor,
        pid,
        *,
        analysis_set_id=None,
        release_id=None,
        section_revision_ids=None,
        ensure=True,
        validate_scope=None,
    ):
        """Build missing ordinary-Markdown vectors and return a portable subset.

        Only content and normalized vectors from the requested current readable
        chapters are returned. Connection settings, credentials, query history,
        usage, other projects and other revisions never enter this projection.
        """
        return self._document_index(
            actor,
            pid,
            analysis_set_id=analysis_set_id,
            release_id=release_id,
            section_revision_ids=section_revision_ids,
            ensure=ensure,
            permission="knowledge:export",
            validate_scope=validate_scope,
        )

    def build_documents(
        self, actor, pid, *, analysis_set_id, form=None, validate_scope=None, job_id=None, job_attempt=None
    ):
        """Index plain documents in the owning durable background job.

        The job supplies its cancellation/attempt/composition fence explicitly,
        since ContextVars do not propagate into the embedding thread pool.
        """
        state = {"state": "building", "phase": "文档章节向量", "completed": 0, "total": 0, "error": ""}
        status_token = None
        if job_id is not None:
            state.update(job_id=job_id, job_attempt=job_attempt)

        def progress(completed, total, token):
            nonlocal status_token
            status_token = token
            state.update(completed=completed, total=total)
            self.store.index_state(pid, state, token, space=space)

        try:
            chapters, baseline, _ = self.document_scope(
                actor, pid, analysis_set_id=analysis_set_id, permission="knowledge:edit"
            )
            settings, config, space = self.config(actor, pid)

            def unchanged():
                if validate_scope:
                    validate_scope()
                now = self.document_scope(actor, pid, analysis_set_id=analysis_set_id, permission="knowledge:edit")
                if now[1] != baseline or self.config(actor, pid) != (settings, config, space):
                    failure("VKS_VERSION_CONFLICT", "文档建库期间版本、权限或配置已变化", 412)

            if not form and settings.enabled:
                # Association jobs only need a verified ready index, not an
                # export of all vectors plus repeated build-lease validation.
                # Inspect every exact chunk/vector once; a count alone cannot
                # establish coverage or detect malformed cached embeddings.
                chunks = list(split(chapters))
                ready = self._export_vectors(pid, space, chunks, settings.dimensions, encode=False)
                if len(ready) == len(chunks):
                    unchanged()
                    return {
                        "total_chunks": len(chunks),
                        "indexed_chunks": len(chunks),
                        "total_sections": len(chapters),
                        "indexed_sections": len({row["revision"] for row in chunks}),
                        "complete": True,
                    }

            result = self._document_index(
                actor,
                pid,
                analysis_set_id=analysis_set_id,
                permission="knowledge:edit",
                validate_scope=unchanged,
                progress=progress,
            )
            if form:
                prepared = self.assistant.prepare(actor, pid, form)

                def module_unchanged():
                    unchanged()
                    if self.assistant.prepare(actor, pid, form)[3] != prepared[3]:
                        failure("VKS_VERSION_CONFLICT", "模块查询预热期间资料范围已变化", 412)

                state.update(phase="预热模块查询")
                if status_token:
                    self.store.index_state(pid, state, status_token)
                self._prewarm_modules(actor, pid, form, settings, config, space, prepared[2], module_unchanged)
            unchanged()
            state.update(state="ready", phase="文档向量检索可用")
            if status_token:
                self.store.index_state(pid, state, status_token)
            return result["coverage"]
        except Exception as e:
            state.update(
                state="failed",
                error=e.message if isinstance(e, DomainError) else "文档索引构建中断，可重试",
            )
            if status_token:
                self.store.index_state(pid, state, status_token)
            raise

    def _document_index(
        self,
        actor,
        pid,
        *,
        analysis_set_id=None,
        release_id=None,
        section_revision_ids=None,
        ensure=True,
        permission,
        validate_scope=None,
        progress=None,
    ):
        if validate_scope:
            validate_scope()
        scope = {
            "analysis_set_id": analysis_set_id,
            "release_id": release_id,
            "section_revision_ids": None if section_revision_ids is None else tuple(section_revision_ids),
            "permission": permission,
        }
        chapters, baseline, provenance = self.document_scope(actor, pid, **scope)
        chunks = list(split(chapters))
        settings = self.store.settings(pid)
        embeddings, space, config, claim_check = {}, None, None, None
        try:
            settings, config, space = self.config(actor, pid)
        except DomainError:
            if ensure and chunks:
                raise
        if ensure and chunks and not settings.enabled:
            failure("VKS_STATE_CONFLICT", "请先启用向量检索，再导出含向量库的成品", 409)

        def unchanged():
            if claim_check:
                claim_check()
            if validate_scope:
                validate_scope()
            now = self.document_scope(actor, pid, **scope)
            if now[1] != baseline:
                failure("VKS_VERSION_CONFLICT", "导出期间文档版本或权限已变化，请重试", 412)
            if space:
                current_settings, current_config, current_space = self.config(actor, pid)
                if (current_settings, current_config, current_space) != (settings, config, space):
                    failure("VKS_VERSION_CONFLICT", "导出期间嵌入配置已变化，请重试", 412)

        if space:
            # A no-build projection reads the current WAL snapshot immediately;
            # it neither changes the vector store nor waits for model I/O.
            guard = self._index_build_lock(pid, space, unchanged) if ensure else nullcontext((None, None))
            with guard as (held, token):
                claim_check = held
                unchanged()
                embeddings = self._export_vectors(pid, space, chunks, settings.dimensions)
                pending = [row for row in chunks if row["id"] not in embeddings]
                if progress:
                    progress(len(embeddings), len(chunks), token)
                if ensure and pending:
                    for row in pending:
                        policy = provenance[row["revision"]]["policy"]
                        if policy["sensitivity"] == "confidential" or (
                            policy["requires_source_read"] and not config.get("allow_source")
                        ):
                            failure("VKS_FORBIDDEN", "模型设置不允许为所选文档建立向量索引", 403)

                    def batch(rows):
                        unchanged()
                        values = self.embed(actor, pid, settings, config, [r["input"] for r in rows])
                        unchanged()
                        with self.store.db() as db:
                            # Lease ownership and vector writes share this
                            # short transaction; a new process cannot take over
                            # between the token check and this batch's commit.
                            db.execute("BEGIN IMMEDIATE")
                            self.store.index_owner(db, pid, space, token)
                            db.executemany(
                                "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
                                [
                                    (pid, space, r["id"], r["revision"], r["start"], r["content"], value)
                                    for r, value in zip(rows, values, strict=True)
                                ],
                            )
                        return len(rows)

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [
                            pool.submit(copy_context().run, batch, pending[i : i + 32])
                            for i in range(0, len(pending), 32)
                        ]
                        completed = len(embeddings)
                        try:
                            for future in as_completed(futures):
                                completed += future.result()
                                if progress:
                                    progress(completed, len(chunks), token)
                        except Exception:
                            for future in futures:
                                future.cancel()
                            raise
                    embeddings = self._export_vectors(pid, space, chunks, settings.dimensions)
                unchanged()
                claim_check = None
        unchanged()
        complete = len(embeddings) == len(chunks)
        if ensure and not complete:
            failure("VKS_STATE_CONFLICT", "所选章节的向量索引尚未完整建立，请重试", 409)
        documents = {c["revision_id"]: c["document_revision_id"] for c in chapters}
        return {
            "model": settings.model_id,
            "dimensions": settings.dimensions,
            "encoding": "float32-le-base64",
            "chunks": [
                {
                    "id": c["id"],
                    "section_id": c["revision"],
                    "document_id": documents[c["revision"]],
                    "start": c["start"],
                    "content": c["content"],
                    "vector": embeddings[c["id"]],
                }
                for c in chunks
                if c["id"] in embeddings
            ],
            "coverage": {
                "total_chunks": len(chunks),
                "indexed_chunks": len(embeddings),
                "total_sections": len(chapters),
                "indexed_sections": len({c["revision"] for c in chunks if c["id"] in embeddings}),
                "complete": complete,
            },
            "retrieval": "hybrid" if complete and chunks else "keyword",
        }

    def config(self, actor, pid):
        from .inference_pipeline import CURRENT_PIPELINE, READ_SESSION, WRITE_SESSION

        pipeline = CURRENT_PIPELINE.get()
        if (
            pipeline
            and pipeline.capacity > 1
            and pipeline.service is self.service
            and not (READ_SESSION.get() or WRITE_SESSION.get())
        ):
            return pipeline.read(lambda: self.config(actor, pid), priority=True)
        with self.assistant.links.reader.reading.db() as s:
            self.service.require(s, actor, pid)
            scoped = s.info.get("vks_read_batch") or s.info.get("vks_completion_batch")
            cached = s.info.setdefault("vks_rag_configs", {}) if scoped else {}
            if (actor, pid) in cached:
                settings, config, space = cached[(actor, pid)]
                return settings.model_copy(deep=True), dict(config), space
            from .corpus_validation import policy_row

            policy = policy_row(s, pid)
            if not policy or not policy.settings.get("enabled") or not policy.settings.get("allow_external"):
                failure("VKS_MODEL_NOT_CONFIGURED", "请先保存模型连接并允许使用项目资料", 503)
            settings = self.store.settings(pid)
            source = policy.settings
            if settings.connection_id:
                conn = s.get(ModelConnection, settings.connection_id)
                if not conn or conn.project_id != pid:
                    failure("VKS_NOT_FOUND", "嵌入连接不存在", 404)
                source = conn.settings
            config = {
                **policy.settings,
                "endpoint": source["endpoint"],
                "secret_ref": source.get("secret_ref"),
                "protocol": "embeddings",
            }
            space = sha(
                canonical(
                    {
                        "endpoint": config["endpoint"],
                        "model": settings.model_id,
                        "dimensions": settings.dimensions,
                        "split": "section-900-120-v1",
                    }
                )
            )
            cached[(actor, pid)] = (settings, config, space)
            return settings.model_copy(deep=True), dict(config), space

    def embed(self, actor, pid, settings, config, texts):
        bound = sum(len(t.encode()) for t in texts) + 128
        key = self.store.reserve(pid, settings, bound)
        used = None
        submitted = False
        try:
            current, current_config, _ = self.config(actor, pid)
            if current != settings or current_config != config:
                failure("VKS_VERSION_CONFLICT", "嵌入配置已变化", 412)
            submitted = True
            result = ai_gateway.request_model(
                config,
                {
                    "model": settings.model_id,
                    "input": texts,
                    "dimensions": settings.dimensions,
                    "encoding_format": "float",
                },
            )
            value = result.get("usage", {}).get("total_tokens")
            if type(value) is not int or value < 0 or value > 1000000000:
                failure("VKS_INVALID_INPUT", "嵌入响应未返回有效用量")
            used = value
            rows = result.get("data")
            if (
                not isinstance(rows, list)
                or len(rows) != len(texts)
                or {r.get("index") for r in rows} != set(range(len(texts)))
            ):
                failure("VKS_INVALID_INPUT", "嵌入响应条目不完整")
            embeddings = [
                normalize(row["embedding"], settings.dimensions) for row in sorted(rows, key=lambda r: r["index"])
            ]
            self.store.settle(key, used, "succeeded")
            return embeddings
        except ModelNotSent:
            self.store.settle(key, 0, "not_sent")
            raise
        except Exception:
            self.store.settle(key, used if submitted else 0, "failed")
            raise

    def available(self, actor, pid, chapters):
        from .inference_pipeline import CURRENT_PIPELINE, READ_SESSION, WRITE_SESSION

        pipeline = CURRENT_PIPELINE.get()
        if pipeline and pipeline.capacity > 1 and not (READ_SESSION.get() or WRITE_SESSION.get()):
            return pipeline.read(lambda: self.available(actor, pid, chapters), priority=True)
        try:
            settings, _, space = self.config(actor, pid)
            return bool(settings.enabled and self.store.has_vectors(pid, space, (c["revision_id"] for c in chapters)))
        except DomainError:
            return False

    def prefetch_queries(self, actor, pid, keys):
        settings, _config, space = self.config(actor, pid)
        if settings.enabled:
            self.store.query_vectors(pid, space, keys)

    def document_status(self, actor, pid, form):
        """Status for an exact readable Markdown corpus, with no prototype dependency."""
        from .models import Job

        scope = form.model_dump()
        chapters, baseline, _ = self.document_scope(actor, pid, **scope, permission="project:manage")
        chunks = list(split(chapters))
        state, settings = self.store.state(pid), self.store.settings(pid)
        data = {
            "state": "empty",
            "phase": "等待建立文档索引",
            "error": "",
            "settings": settings.model_dump(),
            "completed": 0,
            "total": len(chunks),
            "indexed_chunks": 0,
            "indexed_sections": 0,
            "total_sections": len(chapters),
            "used_tokens": 0,
        }
        try:
            settings, _, space = self.config(actor, pid)
            data["settings"] = settings.model_dump()
            known = self._export_vectors(pid, space, chunks, settings.dimensions)
            data.update(
                completed=len(known),
                indexed_chunks=len(known),
                indexed_sections=len({c["revision"] for c in chunks if c["id"] in known}),
            )
            if chunks and len(known) == len(chunks):
                data.update(state="ready", phase="文档向量索引已就绪")
            elif known:
                data.update(state="partial", phase="文档或嵌入模型已更新，需要增量索引")
        except DomainError as error:
            data.update(state="unavailable", phase="请先完成嵌入连接配置", configuration_error=error.message)
        with self.service.db() as s:
            # Only this caller's jobs are inspected; scope and counts are still
            # recomputed from the caller's current exact document selections.
            jobs = s.scalars(
                select(Job)
                .where(Job.project_id == pid, Job.created_by == actor.id, Job.kind == "material_migrate")
                .order_by(Job.updated_at.desc())
                .limit(20)
            )
            for job in jobs:
                job_scope = job.input_json
                if (
                    not form.release_id
                    and job_scope.get("analysis_set_id") == form.analysis_set_id
                    and (job_scope.get("index_documents") or job_scope.get("index_package_id"))
                ):
                    if job.state in {"queued", "running", "retry_wait"} and not job.cancel_requested:
                        if data["state"] != "ready":
                            data.update(
                                state="building",
                                phase="文档索引任务执行中" if job.state == "running" else "等待文档索引任务",
                            )
                    elif job.state == "failed" and data["state"] not in {"ready", "unavailable"}:
                        data.update(
                            state="failed", phase="文档索引任务未完成", error="文档索引未完成，可重试继续已完成的片段"
                        )
                    break
        if state.get("state") == "failed" and data["state"] in {"empty", "partial"}:
            data.update(state="failed", error=state.get("error", ""))
        with self.store.db() as db:
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            data["used_tokens"] = db.execute(
                "SELECT COALESCE(SUM(used),0) FROM usage WHERE project=? AND day=?", (pid, day)
            ).fetchone()[0]
        if self.document_scope(actor, pid, **scope, permission="project:manage")[1] != baseline:
            failure("VKS_VERSION_CONFLICT", "读取索引状态期间文档版本或权限已变化，请刷新", 412)
        return data

    def start_document_job(self, actor, pid, form, key):
        """Queue manual document indexing through the existing durable job boundary."""
        from .materials import MaterialService, StudioWorkspace
        from .models import Job

        if form.release_id:
            failure("VKS_STATE_CONFLICT", "快照为只读版本，请回到项目当前草稿更新索引", 409)
        settings, _, _ = self.config(actor, pid)
        if not settings.enabled:
            failure("VKS_STATE_CONFLICT", "请先启用向量检索", 409)
        chapters, _, _ = self.document_scope(actor, pid, **form.model_dump(), permission="project:manage")
        if not chapters:
            failure("VKS_STATE_CONFLICT", "当前版本没有可建立向量索引的文档章节", 409)
        manager = MaterialService(self.service)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            analysis = manager._draft(s, actor, pid, form.analysis_set_id)
            workspace = s.get(StudioWorkspace, pid)
            if workspace and workspace.analysis_set_id != analysis.id:
                failure("VKS_VERSION_CONFLICT", "项目工作区已切换，请刷新后建立索引", 409)
            composition = manager.composition_digest(s, pid, analysis.id)
            _, _, choices, _ = self.assistant.links.reader.scope(s, actor, pid, analysis.id, None)
            package = next(
                (p for p in self.assistant.links._packages(s, actor, pid, choices) if p["kind"] == "prototype"), None
            )

            def enqueue():
                for active in s.scalars(
                    select(Job).where(
                        Job.project_id == pid,
                        Job.created_by == actor.id,
                        Job.kind == "material_migrate",
                        Job.state.in_(["queued", "running", "retry_wait"]),
                        Job.cancel_requested.is_(False),
                    )
                ):
                    previous = active.input_json
                    if (
                        previous.get("index_documents")
                        and previous.get("analysis_set_id") == analysis.id
                        and previous.get("composition") == composition
                        and previous.get("index_selection_version") == analysis.selection_version
                    ):
                        return {"started": False, "job_id": active.id, "state": active.state}
                job = self.service._new_job(
                    s,
                    actor,
                    pid,
                    "material_migrate",
                    {
                        "analysis_set_id": analysis.id,
                        "targets": [],
                        "issues": [],
                        "composition": composition,
                        "index_documents": True,
                        "index_package_id": package["id"] if package else None,
                        "index_selection_version": analysis.selection_version,
                    },
                )
                return {"started": True, "job_id": job.id, "state": job.state}

            return self.service.idem(s, actor, pid, "rag.documents.build", key, form.model_dump(), enqueue)

    def status(self, actor, pid, form):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
        settings = self.store.settings(pid)
        state = self.store.state(pid)
        with self.lock:
            running = pid in self.running and self.running[pid].is_alive()
        if state["state"] == "building" and state.get("job_id"):
            from .models import Job, now

            with self.service.db() as s:
                job = s.get(Job, state["job_id"])
                running = bool(
                    job
                    and job.project_id == pid
                    and job.attempt == state.get("job_attempt")
                    and job.state == "running"
                    and not job.cancel_requested
                    and (not job.lease_until or job.lease_until > now())
                )
        if state["state"] == "building" and not running:
            # A small resumed build can finish during the status read. Reload
            # its final checkpoint before claiming the process was interrupted.
            latest = self.store.state(pid)
            state = (
                latest
                if latest["state"] != "building"
                else {**latest, "state": "interrupted", "error": "进程已重启，可继续未完成的索引"}
            )
        data = {k: state.get(k, default) for k, default in (("state", "empty"), ("phase", ""), ("error", ""))}
        data.update(
            settings=settings.model_dump(), completed=0, total=0, indexed_chunks=0, indexed_sections=0, total_sections=0
        )
        try:
            _, chapters, _, _, _, _ = self.assistant.prepare(actor, pid, form)
            _, _, space = self.config(actor, pid)
            data["indexed_chunks"], data["indexed_sections"] = self.store.count(
                pid, space, (c["revision_id"] for c in chapters)
            )
            data["total_sections"] = len(chapters)
            # Progress is restricted to this caller's exact, authorized scope,
            # rather than exposing counts from another version/build scope.
            data["total"] = sum(1 + max(0, math.ceil((len(c["content"]) - 900) / 780)) for c in chapters)
            data["completed"] = min(data["total"], data["indexed_chunks"])
            if data["state"] == "ready" and data["indexed_sections"] < len(chapters):
                data.update(state="partial", phase="文档或嵌入模型已更新，需要增量索引")
        except DomainError as e:
            data.update(
                configuration_error=e.message, state="unavailable", phase="当前配置或权限下不可读取索引范围", error=""
            )
        with self.store.db() as db:
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            data["used_tokens"] = db.execute(
                "SELECT COALESCE(SUM(used),0) FROM usage WHERE project=? AND day=?", (pid, day)
            ).fetchone()[0]
        return data

    def save(self, actor, pid, settings):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            if settings.connection_id:
                conn = s.get(ModelConnection, settings.connection_id)
                if not conn or conn.project_id != pid:
                    failure("VKS_NOT_FOUND", "嵌入连接不存在", 404)
            self.service.audit(s, actor, pid, "rag.settings.save", pid)
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (pid, settings.model_dump_json()))
        return settings.model_dump()

    def start(self, actor, pid, form):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
        settings, _, _ = self.config(actor, pid)
        if not settings.enabled:
            failure("VKS_STATE_CONFLICT", "请先启用向量检索", 409)
        prepared = self.assistant.prepare(actor, pid, form)
        if not prepared[1]:
            failure("VKS_STATE_CONFLICT", "当前版本没有可建立向量索引的文档章节", 409)
        with self.lock:
            if pid in self.running and self.running[pid].is_alive():
                return {"started": False}
            self.store.state(pid, {"state": "building", "completed": 0, "total": 0, "error": "", "phase": "准备章节"})
            thread = threading.Thread(target=self._build, args=(actor, pid, form), daemon=True)
            self.running[pid] = thread
            thread.start()
        return {"started": True}

    def _build(self, actor, pid, form):
        state = {"state": "building", "completed": 0, "total": 0, "error": "", "phase": "章节向量"}
        try:
            settings, config, space = self.config(actor, pid)
            prepared = self.assistant.prepare(actor, pid, form)
            chapters, corpus = prepared[1:3]
            state["space"] = space
            chunks = list(split(chapters))
            state["total"] = len(chunks)
            with self.store.db() as db:
                done = {
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM chunks WHERE project=? AND space=? AND embedding IS NOT NULL", (pid, space)
                    )
                }
            pending = [c for c in chunks if c["id"] not in done]
            state["completed"] = len(chunks) - len(pending)
            self.store.state(pid, state)

            def batch(rows):
                now = self.assistant.prepare(actor, pid, form)
                if now[2] != corpus:
                    failure("VKS_VERSION_CONFLICT", "文档范围或权限已变化，请重新建立索引", 412)
                embeddings = self.embed(actor, pid, settings, config, [r["input"] for r in rows])
                if self.assistant.prepare(actor, pid, form)[2] != corpus:
                    failure("VKS_VERSION_CONFLICT", "嵌入期间资料或权限已变化", 412)
                with self.store.db() as db:
                    db.executemany(
                        "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?)",
                        [
                            (pid, space, r["id"], r["revision"], r["start"], r["content"], v)
                            for r, v in zip(rows, embeddings, strict=True)
                        ],
                    )
                return len(rows)

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(copy_context().run, batch, pending[i : i + 32]) for i in range(0, len(pending), 32)
                ]
                try:
                    for future in as_completed(futures):
                        state["completed"] += future.result()
                        self.store.state(pid, state)
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
            state["phase"] = "预热模块查询"
            self.store.state(pid, state)

            def unchanged():
                if self.assistant.prepare(actor, pid, form)[2] != corpus or self.config(actor, pid) != (
                    settings,
                    config,
                    space,
                ):
                    failure("VKS_VERSION_CONFLICT", "模块预热期间资料或配置已变化", 412)

            self._prewarm_modules(actor, pid, form, settings, config, space, corpus, unchanged)
            state.update(state="ready", phase="混合检索可用", corpus=corpus)
            self.store.state(pid, state)
        except Exception as e:  # noqa: BLE001 - persist a safe background-task failure
            state.update(
                state="failed",
                error=e.message if isinstance(e, DomainError) else "索引构建中断，可重试继续已完成的批次",
            )
            self.store.state(pid, state)

    def _prewarm_modules(self, actor, pid, form, settings, config, space, corpus, validate_scope):
        """Reuse query vectors with the same version and durable-job fence."""
        validate_scope()
        from .models import Artifact

        with self.service.db() as s:
            _, _, choices, _ = self.assistant.links.reader.scope(s, actor, pid, form.analysis_set_id, form.release_id)
            package_artifact = s.get(Artifact, form.package_id)
            pages = [
                (r.name, r.metadata_json.get("prototype_manifest", {}).get("routeAliases", []))
                for e, r in self.assistant.links._rows(s, actor, pid, choices)
                if e.type == "page"
                and r.artifact_id
                and s.get(Artifact, r.artifact_id).snapshot_id == package_artifact.snapshot_id
            ]
        queries = {
            f"module:{corpus}:{form.package_id}:{route}": "模块：" + name.split(" · ")[0]
            for name, routes in pages
            for route in routes
        }
        items = list(queries.items())
        for start in range(0, len(items), 32):
            batch_items = items[start : start + 32]
            with self.store.db() as db:
                known = {r[0] for r in db.execute("SELECT id FROM queries WHERE project=? AND space=?", (pid, space))}
            missing = [x for x in batch_items if x[0] not in known]
            if not missing:
                continue
            validate_scope()
            vectors = self.embed(actor, pid, settings, config, [x[1] for x in missing])
            validate_scope()
            with self.store.db() as db:
                db.executemany(
                    "INSERT OR REPLACE INTO queries VALUES (?,?,?,?)",
                    [(pid, space, key, v) for (key, _), v in zip(missing, vectors, strict=True)],
                )
        # The original runtime reports physical HTML paths, whereas the
        # imported page catalog stores logical router aliases. Warm both
        # identities with the same vector so real page entry stays local.
        from packages.analyzers.offline_prototype import PROTOTYPE, _inflate, _scripts

        raw = self.assistant.links.content(actor, pid, form.package_id, form.analysis_set_id, form.release_id)
        payload = _scripts(raw, {PROTOTYPE: "application/octet-stream"}).get(PROTOTYPE)
        validate_scope()
        if payload:
            route_map = json.loads(_inflate(payload)).get("routeMap", {})
            with self.store.db() as db:
                for logical, physical in route_map.items():
                    if not isinstance(physical, str):
                        continue
                    row = db.execute(
                        "SELECT embedding FROM queries WHERE project=? AND space=? AND id=?",
                        (pid, space, f"module:{corpus}:{form.package_id}:{logical}"),
                    ).fetchone()
                    if row:
                        db.execute(
                            "INSERT OR REPLACE INTO queries VALUES (?,?,?,?)",
                            (pid, space, f"module:{corpus}:{form.package_id}:/" + physical.lstrip("/"), row[0]),
                        )
        validate_scope()

    def hybrid(self, actor, pid, form, chapters, corpus, lexical):
        text = (
            "模块：" + form.target.label.split(" · ")[0]
            if form.context.scope == "module"
            else "页面："
            + form.context.page_title.split(" · ")[0]
            + "\n功能："
            + form.target.label
            + "\n上下文："
            + form.context.section_title
        )
        question = getattr(form, "question", None)
        if question:
            text = (
                "问题："
                + question
                + "\n对话："
                + " ".join(m.content for m in form.history if m.role == "user")[-4000:]
                + "\n"
                + form.context.page_title
                + "\n"
                + text
            )
        key = (
            f"module:{corpus}:{form.package_id}:{form.target.route}"
            if form.context.scope == "module" and not question
            else sha(text)
        )
        # A new module direction must not reuse the generic warmed query.
        instruction = getattr(form, "instruction", "").strip()
        if instruction and form.context.scope == "module":
            text += "\n重新关联方向：" + instruction
            key += ":" + sha(text)

        def inputs():
            settings, config, space = self.config(actor, pid)
            scope = {chapter["revision_id"] for chapter in chapters}
            if not settings.enabled or not self.store.has_vectors(pid, space, scope):
                return None
            snapshot = self.vector_search.snapshot(self.store, pid, space, scope, settings.dimensions)
            return settings, config, space, self.store.query_vector(pid, space, key), snapshot

        from .inference_pipeline import CURRENT_PIPELINE, READ_SESSION, WRITE_SESSION

        pipeline = CURRENT_PIPELINE.get()
        ready = (
            pipeline.read(inputs, priority=True, vector_lookup=(self, actor, pid, key))
            if pipeline and pipeline.capacity > 1 and not (READ_SESSION.get() or WRITE_SESSION.get())
            else inputs()
        )
        if ready is None:
            return lexical
        settings, config, space, query, snapshot = ready
        with self.lock:
            lock = self.query_locks.setdefault((pid, space, key), threading.Lock())
        with lock:
            learned_key = (str(self.store.path), pid, space, key)
            if query is None and pipeline:
                with self.lock:
                    query = pipeline.new_query_vectors.get(learned_key)
            if isinstance(query, bytes) and len(query) == settings.dimensions * 4:
                length = math.hypot(*vector(query))
                if not math.isfinite(length) or not 0.999 <= length <= 1.001:
                    query = None
            else:
                query = None
            if query is None:
                query = self.embed(actor, pid, settings, config, [text])[0]
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?)", (pid, space, key, query))
                if pipeline:
                    with self.lock:
                        pipeline.new_query_vectors[learned_key] = query
                        if len(pipeline.new_query_vectors) > 1024:
                            pipeline.new_query_vectors.pop(next(iter(pipeline.new_query_vectors)))
        q = vector(query)
        lookup = {c["revision_id"]: c for c in chapters}

        matches = self.vector_search.nearest(
            self.store, pid, space, lookup, q, limit=max(36, settings.candidate_limit * 2), snapshot=snapshot
        )
        scores = {}
        candidates = {r["revision_id"]: r for r in lexical}
        seen = set()
        for rank, row in enumerate(lexical):
            scores[row["revision_id"]] = 1 / (60 + rank)
        for cid, rid, start, content, score in matches:
            if rid in seen or score < 0.15:
                continue
            chapter = lookup[rid]
            if (
                not isinstance(start, int)
                or start < 0
                or start % 780
                or chapter["content"][start : start + 900] != content
            ):
                continue
            expected = chapter["document_name"][:160] + "\n" + chapter["name"][:160] + "\n" + content
            if cid != sha(canonical([rid, start, expected])):
                continue
            seen.add(rid)
            scores[rid] = scores.get(rid, 0) + 1.15 / (60 + len(seen))
            candidates[rid] = {**lookup[rid], "content": content}
        result = []
        docs = {}
        for rid in sorted(scores, key=lambda r: -scores[r]):
            row = candidates[rid]
            doc = row["document_revision_id"]
            if docs.get(doc, 0) >= settings.per_document_limit:
                continue
            docs[doc] = docs.get(doc, 0) + 1
            result.append(row)
            if len(result) == settings.candidate_limit:
                break
        return result


def install_rag(app, service, actor, envelope, assistant, AssistanceInput, RelatedOutput):
    rag = assistant.rag
    prefix = "/api/v3/projects/{pid}/interactive/rag"
    ActorDep = Annotated[Actor, Depends(actor)]

    @app.get(prefix + "/settings", response_model=PrototypeEnvelope[dict])
    def settings(pid: str, a: ActorDep, request: Request):
        with service.db() as s:
            service.require(s, a, pid, "project:manage")
        return envelope(request, rag.store.settings(pid).model_dump())

    @app.post(prefix + "/settings", response_model=PrototypeEnvelope[dict])
    def save(pid: str, payload: RagSettings, a: ActorDep, request: Request):
        return envelope(request, rag.save(a, pid, payload))

    @app.post(prefix + "/status", response_model=PrototypeEnvelope[dict])
    def status(pid: str, payload: AssistanceInput, a: ActorDep, request: Request):
        return envelope(request, rag.status(a, pid, payload))

    @app.post(prefix + "/documents/status", response_model=PrototypeEnvelope[dict])
    def document_status(pid: str, payload: DocumentIndexInput, a: ActorDep, request: Request):
        return envelope(request, rag.document_status(a, pid, payload))

    @app.post(prefix + "/documents/build", response_model=PrototypeEnvelope[dict])
    def document_build(
        pid: str,
        payload: DocumentIndexInput,
        a: ActorDep,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ):
        return envelope(request, rag.start_document_job(a, pid, payload, idempotency_key))

    @app.post(prefix + "/test", response_model=PrototypeEnvelope[dict])
    def test(pid: str, a: ActorDep, request: Request):
        with service.db() as s:
            service.require(s, a, pid, "project:manage")
        started = time.perf_counter()
        settings, config, _ = rag.config(a, pid)
        output = rag.embed(a, pid, settings, config, ["收费标准定义价格、公式和计费周期。"])
        return envelope(
            request,
            {
                "ok": True,
                "model_id": settings.model_id,
                "dimensions": len(vector(output[0])),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

    @app.post(prefix + "/build", response_model=PrototypeEnvelope[dict])
    def build(pid: str, payload: AssistanceInput, a: ActorDep, request: Request):
        return envelope(request, rag.start(a, pid, payload))

    @app.post("/api/v3/projects/{pid}/interactive/semantic", response_model=PrototypeEnvelope[RelatedOutput])
    def semantic(pid: str, payload: AssistanceInput, a: ActorDep, request: Request):
        started = time.perf_counter()
        with assistant.links.reader.reading.db() as session:
            state = assistant.prepare(a, pid, payload, session=session)
            current = assistant._binding(a, pid, payload, session=session)
        if current and current["excluded"]:
            failure("VKS_STATE_CONFLICT", "此关联已移除", 409)
        if assistant._has_current_binding(current):
            return envelope(request, assistant._related_result(a, pid, payload, state, current, started))
        available = rag.available(a, pid, state[1])
        if not available:
            return envelope(
                request, assistant._related_result(a, pid, payload, state, current, started, vector_available=False)
            )
        lexical = assistant._index(state[2], state[1]).search(payload.target, payload.context, set())
        sections = rag.hybrid(a, pid, payload, state[1], state[2], lexical)
        # Semantic retrieval may call the embedding service. Recheck inputs and
        # an explicit removal together in a fresh snapshot after that boundary.
        with assistant.links.reader.reading.db() as session:
            latest = assistant.prepare(a, pid, payload, session=session)
            latest_current = assistant._binding(a, pid, payload, session=session)
        if latest[3] != state[3]:
            failure("VKS_VERSION_CONFLICT", "资料或模型配置已变化，请重试", 412)
        if latest_current and latest_current["excluded"]:
            failure("VKS_STATE_CONFLICT", "此关联已移除", 409)
        if assistant._has_current_binding(latest_current):
            return envelope(
                request,
                assistant._related_result(a, pid, payload, latest, latest_current, started, vector_available=True),
            )
        return envelope(
            request,
            {
                "title": payload.target.label,
                "sections": sections,
                "binding_id": latest_current["id"] if latest_current else None,
                "origin": "retrieval",
                "model_available": True,
                "selection_version": latest[4],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "retrieval": "hybrid",
                "vector_available": True,
            },
        )
