"""Rebuildable local vector database and permission-scoped hybrid RAG.

SQLite stores real normalized float32 embeddings, immutable chunk identities and
query vectors. Exact cosine search uses math.sumprod in C; no external DB server
or extension is needed for this personal-use corpus. Knowledge remains in VKS.
"""

import json
import math
import os
import sqlite3
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from pydantic import Field, model_validator
from sqlalchemy import select

from . import ai_gateway
from .ai_gateway import ModelNotSent, ModelPolicy, failure
from .model_connections import ModelConnection
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input
from .service import Actor, DomainError, canonical, sha


class RagSettings(Input):
    enabled: bool = True
    connection_id: str | None = Field(default=None, max_length=96)
    model_id: str = Field(default="text-embedding-3-small", min_length=1, max_length=160)
    dimensions: int = Field(default=512, ge=64, le=3072)
    daily_token_limit: int = Field(default=10000000, ge=10000, le=100000000)

    @model_validator(mode="after")
    def supported_dimensions(self):
        if self.model_id == "text-embedding-3-small" and self.dimensions > 1536:
            raise ValueError("text-embedding-3-small 的维度不能超过 1536")
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config(project TEXT PRIMARY KEY, settings TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS status(project TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks(project TEXT, space TEXT, id TEXT, revision TEXT, start INTEGER, content TEXT, embedding BLOB, PRIMARY KEY(project,space,id));
                CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(project,space,revision);
                CREATE TABLE IF NOT EXISTS queries(project TEXT, space TEXT, id TEXT, embedding BLOB, PRIMARY KEY(project,space,id));
                CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,project TEXT,day TEXT,model TEXT,reserved INTEGER,used INTEGER,state TEXT);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            yield db

    def settings(self, pid):
        with self.db() as db:
            row = db.execute("SELECT settings FROM config WHERE project=?", (pid,)).fetchone()
        return RagSettings.model_validate_json(row[0]) if row else RagSettings(enabled=False)

    def state(self, pid, data=None):
        with self.db() as db:
            if data is not None:
                db.execute("INSERT OR REPLACE INTO status VALUES (?,?)", (pid, canonical(data)))
            row = db.execute("SELECT data FROM status WHERE project=?", (pid,)).fetchone()
            return json.loads(row[0]) if row else {"state": "empty", "completed": 0, "total": 0, "error": ""}

    def reserve(self, pid, settings, bound):
        from .models import uid

        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        key = uid()
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute(
                "SELECT COALESCE(SUM(used+reserved),0) FROM usage WHERE project=? AND day=?", (pid, day)
            ).fetchone()[0]
            if used + bound > settings.daily_token_limit:
                failure("VKS_LIMIT_EXCEEDED", "嵌入模型每日预算已达到上限；可调整预算后继续", 429)
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
            db.execute("CREATE TEMP TABLE allowed(id TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO allowed VALUES (?)", ((r,) for r in revisions))
            return db.execute(
                "SELECT COUNT(*),COUNT(DISTINCT revision) FROM chunks WHERE project=? AND space=? AND embedding IS NOT NULL AND revision IN (SELECT id FROM allowed)",
                (pid, space),
            ).fetchone()


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
        self.lock = threading.RLock()
        self.running = {}
        self.query_locks = {}

    def config(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
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
            return settings, config, space

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
        try:
            settings, _, space = self.config(actor, pid)
            return bool(settings.enabled and self.store.count(pid, space, (c["revision_id"] for c in chapters))[0])
        except DomainError:
            return False

    def status(self, actor, pid, form):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
        settings = self.store.settings(pid)
        state = self.store.state(pid)
        with self.lock:
            running = pid in self.running and self.running[pid].is_alive()
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
                futures = [pool.submit(batch, pending[i : i + 32]) for i in range(0, len(pending), 32)]
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
            from .models import Artifact

            with self.service.db() as s:
                _, _, choices, _ = self.assistant.links.reader.scope(
                    s, actor, pid, form.analysis_set_id, form.release_id
                )
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
                    known = {
                        r[0] for r in db.execute("SELECT id FROM queries WHERE project=? AND space=?", (pid, space))
                    }
                missing = [x for x in batch_items if x[0] not in known]
                if not missing:
                    continue
                if self.assistant.prepare(actor, pid, form)[2] != corpus:
                    failure("VKS_VERSION_CONFLICT", "资料范围已变化", 412)
                vectors = self.embed(actor, pid, settings, config, [x[1] for x in missing])
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
            state.update(state="ready", phase="混合检索可用", corpus=corpus)
            self.store.state(pid, state)
        except Exception as e:  # noqa: BLE001 - persist a safe background-task failure
            state.update(
                state="failed",
                error=e.message if isinstance(e, DomainError) else "索引构建中断，可重试继续已完成的批次",
            )
            self.store.state(pid, state)

    def hybrid(self, actor, pid, form, chapters, corpus, lexical):
        settings, config, space = self.config(actor, pid)
        if not settings.enabled or not self.store.count(pid, space, (c["revision_id"] for c in chapters))[0]:
            return lexical
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
        key = f"module:{corpus}:{form.package_id}:{form.target.route}" if form.context.scope == "module" else sha(text)
        with self.lock:
            lock = self.query_locks.setdefault((pid, space, key), threading.Lock())
        with lock:
            with self.store.db() as db:
                row = db.execute(
                    "SELECT embedding FROM queries WHERE project=? AND space=? AND id=?", (pid, space, key)
                ).fetchone()
            if row:
                query = row[0]
            else:
                query = self.embed(actor, pid, settings, config, [text])[0]
                with self.store.db() as db:
                    db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?)", (pid, space, key, query))
        q = vector(query)
        lookup = {c["revision_id"]: c for c in chapters}
        with self.store.db() as db:
            # Filter current authorized revisions BEFORE computing nearest matches.
            db.execute("CREATE TEMP TABLE allowed(id TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO allowed VALUES (?)", ((rid,) for rid in lookup))
            db.create_function("vks_dot", 1, lambda blob: math.sumprod(vector(blob), q))
            matches = db.execute(
                "SELECT revision,content,vks_dot(embedding) AS score FROM chunks WHERE project=? AND space=? AND revision IN (SELECT id FROM allowed) ORDER BY score DESC LIMIT 36",
                (pid, space),
            ).fetchall()
        scores = {}
        candidates = {r["revision_id"]: r for r in lexical}
        seen = set()
        for rank, row in enumerate(lexical):
            scores[row["revision_id"]] = 1 / (60 + rank)
        for rid, content, score in matches:
            if rid in seen or score < 0.15:
                continue
            seen.add(rid)
            scores[rid] = scores.get(rid, 0) + 1.15 / (60 + len(seen))
            candidates[rid] = {**lookup[rid], "content": content}
        result = []
        docs = {}
        for rid in sorted(scores, key=lambda r: -scores[r]):
            row = candidates[rid]
            doc = row["document_revision_id"]
            if docs.get(doc, 0) >= 3:
                continue
            docs[doc] = docs.get(doc, 0) + 1
            result.append(row)
            if len(result) == 6:
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
        state = assistant.prepare(a, pid, payload)
        current = assistant._binding(a, pid, payload)
        if current and current["excluded"]:
            failure("VKS_STATE_CONFLICT", "此关联已移除", 409)
        if current:
            return envelope(request, assistant.related(a, pid, payload))
        available = rag.available(a, pid, state[1])
        if not available:
            return envelope(request, assistant.related(a, pid, payload))
        lexical = assistant._index(state[2], state[1]).search(payload.target, payload.context, set())
        sections = rag.hybrid(a, pid, payload, state[1], state[2], lexical)
        assistant._unchanged(a, pid, payload, state[3], None, None)
        return envelope(
            request,
            {
                "title": payload.target.label,
                "sections": sections,
                "binding_id": current["id"] if current else None,
                "origin": "binding" if current else "retrieval",
                "model_available": True,
                "selection_version": state[4],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "retrieval": "hybrid",
                "vector_available": True,
            },
        )
