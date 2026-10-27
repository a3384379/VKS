"""Bounded fixed-baseline reads with per-transaction authorization only.

The shared cache contains immutable release facts, never actors, grants, artifact
policies, or permission decisions. Those are re-read for each transaction.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
import re

from sqlalchemy import Text, cast, func, literal, or_, select
from sqlalchemy.orm import load_only

from .models import Analysis, Artifact, Entity, Evidence, Relation, Release, Revision, Selection

UNKNOWN = ["尚未接入真实业务环境；运行与源码版本未核对", "静态候选和单次采集不能证明全部业务行为；发布前需人员审核"]
PUBLIC_FIELDS = (
    "entity_id",
    "entity_revision_id",
    "type",
    "name",
    "content_sha256",
    "semantic_content_sha256",
    "schema_version",
)


def relation_is_visible(row, revision_ids, evidence_ids):
    return (
        row["from_revision_id"] in revision_ids
        and row["to_revision_id"] in revision_ids
        and bool(set(row["evidence_ids"]) & evidence_ids)
    )


def scenario_trace_required(metadata):
    graph = (metadata or {}).get("dsl", {}).get("explanation_graph", {})
    return any(
        item.get("evidenceOrigin") == "observed" for item in [*graph.get("states", []), *graph.get("transitions", [])]
    )


def capture_source_required(s, pid, capture_ids):
    if not capture_ids:
        return False
    rows = s.scalars(select(Artifact).where(Artifact.project_id == pid, Artifact.snapshot_id.in_(capture_ids)))
    return any(
        art.kind == "source"
        or art.sensitivity == "confidential"
        or (art.metadata_json or {}).get("requires_source_read")
        for art in rows
    )


def scenario_source_required(s, pid, metadata):
    graph = (metadata or {}).get("dsl", {}).get("explanation_graph", {})
    ids = {
        row["captureRef"]
        for row in [*graph.get("states", []), *graph.get("transitions", [])]
        if row.get("evidenceOrigin") == "observed" and row.get("captureRef")
    }
    return capture_source_required(s, pid, ids)


def artifact_policy(s, artifact):
    """Read-time quarantine for legacy model outputs with unknown input scope."""
    metadata = dict(artifact.metadata_json or {})
    if artifact.relative_path.startswith(("journeys/", "task-reports/")):
        source, trace = False, False
        for rid in metadata.get("journey_binding_revisions", []):
            revision = s.get(Revision, rid)
            if not revision:
                source, trace = True, True
                continue
            target = s.get(Artifact, revision.artifact_id) if revision.artifact_id else None
            policy = artifact_policy(s, target) if target else {}
            source = (
                source
                or s.get(Entity, revision.entity_id).type in ("symbol", "sql", "table")
                or bool(target and (target.kind == "source" or target.sensitivity == "confidential"))
                or policy.get("requires_source_read", False)
            )
            trace = (
                trace
                or bool(target and target.kind in ("trace", "capture"))
                or policy.get("requires_trace_read", False)
            )
            if s.get(Entity, revision.entity_id).type == "scenario":
                trace = trace or scenario_trace_required(revision.metadata_json)
                source = source or scenario_source_required(s, revision.project_id, revision.metadata_json)
        for aid in metadata.get("journey_capture_artifacts", []):
            target = s.get(Artifact, aid)
            trace = True
            source = (
                source
                or not target
                or target.sensitivity == "confidential"
                or target.kind == "source"
                or (target.metadata_json or {}).get("requires_source_read", False)
            )
        return dict(
            metadata,
            requires_source_read=bool(source or metadata.get("requires_source_read")),
            requires_trace_read=bool(trace or metadata.get("requires_trace_read")),
        )
    root, seen = artifact, set()
    while root and root.id not in seen:
        seen.add(root.id)
        provenance = root.metadata_json.get("input_provenance")
        if (
            isinstance(provenance, list)
            and provenance
            and all(
                isinstance(row, dict)
                and {"artifact_id", "evidence_id", "sha256", "requires_source_read", "requires_trace_read"} <= set(row)
                and type(row["requires_source_read"]) is bool
                and type(row["requires_trace_read"]) is bool
                for row in provenance
            )
        ):
            return dict(
                metadata,
                requires_source_read=bool(
                    metadata.get("requires_source_read") or any(row["requires_source_read"] for row in provenance)
                ),
                requires_trace_read=bool(
                    metadata.get("requires_trace_read") or any(row["requires_trace_read"] for row in provenance)
                ),
            )
        if root.relative_path.startswith("documents/generated-"):
            legacy = s.scalar(
                select(Revision.id)
                .where(
                    Revision.artifact_id == root.id,
                    Revision.metadata_json["source_kind"].as_string().in_(["ai_generated", "generated"]),
                )
                .limit(1)
            )
            if legacy:
                return dict(metadata, requires_source_read=True, requires_trace_read=True)
        original = root.metadata_json.get("original_artifact_id")
        root = s.get(Artifact, original) if original else None
    return metadata


def explanation_relation(row, policy):
    if row["review_status"] == "rejected":
        return False
    if policy == "trusted":
        return row["review_status"] == "confirmed" and row["validity_status"] == "valid"
    if policy == "candidate":
        return row["review_status"] == "candidate" and row["validity_status"] == "valid"
    if policy == "review":
        return row["validity_status"] != "valid"
    if policy == "map":
        return True  # All visible, non-rejected relationships; UI keeps review/validity labels.
    raise ValueError("Unknown explanation relation policy")


def chunks(values, size=500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


@dataclass
class FrozenIndex:
    selections: dict
    outgoing: dict
    incoming: dict
    evidence_ids: set
    neighbors_by_policy: dict

    @classmethod
    def build(cls, manifest):
        selections = {row["entity_id"]: row["entity_revision_id"] for row in manifest["selections"]}
        outgoing = defaultdict(dict)
        incoming = defaultdict(dict)
        evidence = set()
        neighbors = {policy: defaultdict(set) for policy in (None, "trusted", "candidate", "review", "map")}
        for value in manifest["relations"]:
            row = dict(value, evidence_ids=tuple(value["evidence_ids"]))
            outgoing[row["from_revision_id"]].setdefault(row["to_revision_id"], []).append(row)
            incoming[row["to_revision_id"]].setdefault(row["from_revision_id"], []).append(row)
            evidence.update(row["evidence_ids"])
            if row["evidence_ids"]:
                for policy, adjacent in neighbors.items():
                    if policy is None or explanation_relation(row, policy):
                        adjacent[row["from_revision_id"]].add(row["to_revision_id"])
                        adjacent[row["to_revision_id"]].add(row["from_revision_id"])
        return cls(selections, dict(outgoing), dict(incoming), evidence, {p: dict(v) for p, v in neighbors.items()})


class ReadingService:
    def __init__(self, service):
        self.service = service

    @contextmanager
    def db(self):
        # Reads do not take the process mutation lock or SQLite's write lock.
        with self.service.Session.begin() as session:
            session.info["vks_read_only"] = True
            if self.service.engine.dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            elif self.service.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN")
            yield session

    def baseline(self, s, actor, pid, analysis_set_id=None, release_id=None):
        from .service import DomainError

        project = self.service.require(s, actor, pid)
        if analysis_set_id and release_id:
            raise DomainError("VKS_INVALID_INPUT", "release_id 与 analysis_set_id 只能选择一个")
        release_query = select(Release).options(
            load_only(
                Release.id,
                Release.project_id,
                Release.analysis_set_id,
                Release.release_no,
                Release.manifest_sha256,
                Release.published_by,
                Release.created_at,
            )
        )
        if release_id:
            release = s.scalar(release_query.where(Release.id == release_id, Release.project_id == pid))
            if not release:
                raise DomainError("VKS_NOT_FOUND", "未找到发布版", 404)
            return s.get(Analysis, release.analysis_set_id), release
        if analysis_set_id:
            analysis = s.get(Analysis, analysis_set_id)
            if not analysis or analysis.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
            release = s.scalar(release_query.where(Release.analysis_set_id == analysis.id))
            if not release:
                self.service.require(s, actor, pid, "knowledge:edit")
            return analysis, release
        if self.service._can(s, actor, pid, "knowledge:edit"):
            analysis = s.scalar(select(Analysis).where(Analysis.project_id == pid).order_by(Analysis.created_at.desc()))
            return analysis, s.scalar(release_query.where(Release.analysis_set_id == analysis.id)) if analysis else None
        if project.current_release_id:
            release = s.scalar(release_query.where(Release.id == project.current_release_id, Release.project_id == pid))
            if release:
                return s.get(Analysis, release.analysis_set_id), release
        return None, None

    def index(self, s, release):
        lock = self.service.__dict__.setdefault("_read_release_lock", RLock())
        with lock:
            cache = self.service.__dict__.setdefault("_read_release_cache", OrderedDict())
            metrics = self.service.__dict__.setdefault("_read_cache_metrics", {"hits": 0, "misses": 0})
            key = (release.id, release.manifest_sha256)
            if key in cache:
                metrics["hits"] += 1
                cache.move_to_end(key)
                return cache[key]
            from .service import DomainError, canonical, sha

            manifest = s.scalar(select(Release.manifest_json).where(Release.id == release.id))
            if sha(canonical(manifest)) != release.manifest_sha256:
                raise DomainError("VKS_INTEGRITY_ERROR", "发布清单摘要不匹配", 409)
            index = FrozenIndex.build(manifest)
            cache[key] = index
            metrics["misses"] += 1
            while len(cache) > 2:
                cache.popitem(last=False)
            return index

    def selections(self, s, analysis, release):
        if not analysis:
            return {}, None
        if release:
            index = self.index(s, release)
            return index.selections, index
        return dict(
            s.execute(
                select(Selection.entity_id, Selection.entity_revision_id).where(
                    Selection.analysis_set_id == analysis.id
                )
            ).all()
        ), None

    def permissions(self, s, actor, pid):
        return (self.service._can(s, actor, pid, "source:read"), self.service._can(s, actor, pid, "trace:read"))

    @staticmethod
    def policy_visible(kind, sensitivity, metadata, source_allowed, trace_allowed, entity_type=None):
        from .service import SOURCE_TYPES

        metadata = metadata or {}
        return (
            source_allowed
            or not (
                entity_type in SOURCE_TYPES
                or kind == "source"
                or sensitivity == "confidential"
                or metadata.get("requires_source_read")
            )
        ) and (trace_allowed or not (kind in ("trace", "capture") or metadata.get("requires_trace_read")))

    def visible_ids(self, s, actor, pid, selections):
        source, trace = self.permissions(s, actor, pid)
        if source and trace:
            return dict(selections)
        visible = {}
        for batch in chunks(selections.values()):
            rows = s.execute(
                select(
                    Revision.id,
                    Revision.entity_id,
                    Entity.type,
                    Artifact.kind,
                    Artifact.sensitivity,
                    Artifact.metadata_json,
                    Artifact.id,
                    Artifact.relative_path,
                    Revision.metadata_json,
                )
                .join(Entity, Revision.entity_id == Entity.id)
                .outerjoin(Artifact, Revision.artifact_id == Artifact.id)
                .where(Revision.project_id == pid, Revision.id.in_(batch))
            )
            for rid, eid, kind, art_kind, sensitivity, metadata, artifact_id, path, revision_metadata in rows:
                if artifact_id and (
                    (path or "").startswith(("documents/generated-", "journeys/"))
                    or (metadata or {}).get("original_artifact_id")
                ):
                    metadata = artifact_policy(s, s.get(Artifact, artifact_id))
                if kind == "scenario" and scenario_trace_required(revision_metadata):
                    metadata = dict(
                        metadata or {},
                        requires_trace_read=True,
                        requires_source_read=scenario_source_required(s, pid, revision_metadata),
                    )
                if self.policy_visible(art_kind, sensitivity, metadata, source, trace, kind):
                    visible[eid] = rid
        return visible

    def visible_evidence(self, s, actor, pid, aid, ids=None):
        source, trace = self.permissions(s, actor, pid)
        if source and trace:
            query = select(Evidence.id).where(Evidence.project_id == pid, Evidence.analysis_set_id == aid)
            if ids is None or len(ids) >= 500:
                found = set(s.scalars(query))
                return found if ids is None else found & set(ids)
            found = set()
            for batch in chunks(ids):
                found.update(s.scalars(query.where(Evidence.id.in_(batch))))
            return found
        visible = set()
        batches = chunks(ids) if ids is not None else [None]
        for batch in batches:
            query = (
                select(
                    Evidence.id,
                    Artifact.kind,
                    Artifact.sensitivity,
                    Artifact.metadata_json,
                    Artifact.id,
                    Artifact.relative_path,
                )
                .join(Artifact, Artifact.id == Evidence.artifact_id)
                .where(Evidence.project_id == pid, Evidence.analysis_set_id == aid)
            )
            if batch is not None:
                query = query.where(Evidence.id.in_(batch))
            for eid, kind, sensitivity, metadata, artifact_id, path in s.execute(query):
                if path.startswith("documents/generated-") or metadata.get("original_artifact_id"):
                    metadata = artifact_policy(s, s.get(Artifact, artifact_id))
                if self.policy_visible(kind, sensitivity, metadata, source, trace):
                    visible.add(eid)
        return visible

    def preload(self, s, revisions=(), evidence=()):
        revisions = list(revisions)
        evidence = list(evidence)
        retained = s.info.setdefault("vks_prefetched", {})
        entities = {r.entity_id for r in revisions}
        artifacts = {r.artifact_id for r in revisions if r.artifact_id} | {e.artifact_id for e in evidence}
        for model, ids in ((Entity, entities), (Artifact, artifacts)):
            for batch in chunks(ids):
                for row in s.scalars(select(model).where(model.id.in_(batch))):
                    retained[(model, row.id)] = row

    def revision_rows(self, s, ids):
        rows = []
        for batch in chunks(ids):
            rows.extend(s.scalars(select(Revision).where(Revision.id.in_(batch))))
        self.preload(s, rows)
        return rows

    def edge_rows(self, s, aid, index, frontier=None, selected=None):
        if index:
            output = []
            if selected is not None:
                for source in selected:
                    targets = index.outgoing.get(source, {})
                    candidates = targets.keys() & selected
                    for target in candidates:
                        output.extend(targets[target])
            else:
                seen = set()
                for node in frontier:
                    for direction in (index.outgoing, index.incoming):
                        for rows in direction.get(node, {}).values():
                            for row in rows:
                                if row["id"] not in seen:
                                    seen.add(row["id"])
                                    output.append(row)
            # Callers may add display names or redact evidence. Never hand out
            # the shared immutable release fact dictionaries themselves.
            return [dict(row, evidence_ids=list(row["evidence_ids"])) for row in output]
        query = select(Relation).where(Relation.analysis_set_id == aid)
        if selected is not None:
            query = query.where(Relation.from_revision_id.in_(selected), Relation.to_revision_id.in_(selected))
        elif frontier is not None:
            query = query.where(or_(Relation.from_revision_id.in_(frontier), Relation.to_revision_id.in_(frontier)))
        return [self.service.relation_dto(row) for row in s.scalars(query)]

    def _graph(
        self,
        s,
        actor,
        pid,
        eid,
        analysis_set_id,
        release_id,
        depth,
        max_nodes,
        relation_types,
        extended,
        explanation_policy=None,
    ):
        from .service import DomainError

        if not 1 <= depth <= 3 or not 1 <= max_nodes <= 200:
            raise DomainError("VKS_INVALID_INPUT", "图谱深度须为 1–3，节点上限为 200")
        analysis, release = self.baseline(s, actor, pid, analysis_set_id, release_id)
        choices, index = self.selections(s, analysis, release)
        visible = self.visible_ids(s, actor, pid, choices)
        seed = visible.get(eid) or (eid if eid in visible.values() else None)
        if not seed:
            raise DomainError("VKS_NOT_FOUND", "未找到当前版本实体", 404)
        allowed = set(visible.values())
        valid_evidence = self.visible_evidence(s, actor, pid, analysis.id, index.evidence_ids if index else None)
        selected = {seed}
        frontier = {seed}
        truncated = False

        def usable(row):
            return (
                (not relation_types or row["relation_type"] in relation_types)
                and relation_is_visible(row, allowed, valid_evidence)
                and (explanation_policy is None or explanation_relation(row, explanation_policy))
            )

        for _ in range(depth):
            neighbors = set()
            if index and not relation_types and index.evidence_ids <= valid_evidence:
                adjacent = index.neighbors_by_policy[explanation_policy]
                for node in frontier:
                    neighbors.update(adjacent.get(node, ()))
            else:
                for row in self.edge_rows(s, analysis.id, index, frontier=frontier):
                    if usable(row) and row["from_revision_id"] in allowed and row["to_revision_id"] in allowed:
                        neighbors.update((row["from_revision_id"], row["to_revision_id"]))
            neighbors = (neighbors & allowed) - selected
            available = max_nodes - len(selected)
            if len(neighbors) > available:
                neighbors = set(sorted(neighbors)[:available])
                truncated = True
            selected.update(neighbors)
            frontier = neighbors
            if not frontier:
                break
        rows = self.revision_rows(s, selected)
        nodes = [self.service.entity_dto(s, row, True) for row in rows]
        nodes.sort(key=lambda row: row["entity_revision_id"])
        if not extended:
            nodes = [{key: node[key] for key in PUBLIC_FIELDS} for node in nodes]
        edges = [
            dict(row, evidence_ids=[e for e in row["evidence_ids"] if e in valid_evidence])
            for row in self.edge_rows(s, analysis.id, index, selected=selected)
            if usable(row)
        ]
        edges.sort(key=lambda row: row["id"])
        return {"nodes": nodes, "edges": edges, "truncated": truncated, "unknowns": list(UNKNOWN)}, analysis, release

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
        with self.db() as s:
            return self._graph(
                s, actor, pid, eid, analysis_set_id, release_id, depth, max_nodes, relation_types, extended
            )[0]

    def action_context(
        self, actor, pid, eid, analysis_set_id=None, release_id=None, depth=2, max_nodes=50, relation_policy="trusted"
    ):
        from .service import DomainError, sha

        with self.db() as s:
            if relation_policy not in ("trusted", "candidate", "review"):
                raise DomainError("VKS_INVALID_INPUT", "讲解关系策略无效")
            graph, analysis, release = self._graph(
                s, actor, pid, eid, analysis_set_id, release_id, depth, max_nodes, None, True, relation_policy
            )
            action = next(
                (node for node in graph["nodes"] if node["entity_id"] == eid and node["type"] == "action"), None
            )
            if not action:
                raise DomainError("VKS_NOT_FOUND", "未找到当前版本操作", 404)
            evidence_ids = {eid for row in graph["edges"] for eid in row["evidence_ids"]}
            evidence = []
            for batch in chunks(evidence_ids):
                evidence.extend(
                    self.service.evidence_dto(row) for row in s.scalars(select(Evidence).where(Evidence.id.in_(batch)))
                )
            public = lambda node: {key: node[key] for key in PUBLIC_FIELDS}
            apis = []
            for node in graph["nodes"]:
                if node["type"] != "api":
                    continue
                metadata = node["metadata"]
                apis.append(
                    {
                        "entity_revision_id": node["entity_revision_id"],
                        "service_key": metadata.get("service_key", "demo"),
                        "method": metadata.get("method", "GET"),
                        "normalized_route": metadata.get(
                            "normalized_route", metadata.get("path", metadata.get("route", "/"))
                        ),
                        "condition_fingerprint": metadata.get("condition_fingerprint", sha("{}")),
                        "resolution_state": metadata.get("resolution_state", "resolved"),
                    }
                )
            result = {
                "project_id": pid,
                "analysis_set_id": analysis.id,
                "baseline_kind": "published" if release else "draft",
                "action": public(action),
                "api_endpoints": apis,
                "entities": [public(node) for node in graph["nodes"]],
                "relations": graph["edges"],
                "evidence": evidence,
                "unknowns": graph["unknowns"],
                "runtime_alignment": analysis.runtime_alignment,
            }
            if release:
                result["release_id"] = release.id
            return result

    def selection_workspace(self, actor, pid, analysis_set_id=None, release_id=None):
        with self.db() as s:
            analysis, release = self.baseline(s, actor, pid, analysis_set_id, release_id)
            choices, _ = self.selections(s, analysis, release)
            visible = self.visible_ids(s, actor, pid, choices)
            revisions = self.revision_rows(s, visible.values())
            return {
                "entities": [self.service.entity_dto(s, row, True) for row in revisions],
                "sources": self.service._visible_sources(s, actor, pid, analysis.sources) if analysis else [],
                "selected_analysis_set": self.service.analysis_dto(analysis) if analysis else None,
                "selected_release": self.service.release_dto(release) if release else None,
            }

    def search(self, actor, pid, payload):
        from .knowledge import SourceConnector, SourceSnapshot
        from .modules import BusinessModule, ModuleConnector, ModuleEntity, ModuleSource
        from .service import DomainError, canonical

        query = payload["query"].strip().casefold()
        if not query:
            raise DomainError("VKS_INVALID_INPUT", "请输入检索词")
        with self.db() as s:
            analysis, release = self.baseline(s, actor, pid, payload.get("analysis_set_id"), payload.get("release_id"))
            choices, index = self.selections(s, analysis, release)
            visible = self.visible_ids(s, actor, pid, choices)
            if payload.get("module_id"):
                module = s.get(BusinessModule, payload["module_id"])
                if not module or module.project_id != pid:
                    raise DomainError("VKS_NOT_FOUND", "未找到业务模块", 404)
                mids = {module.id}
                frontier = set(mids)
                while frontier and payload.get("include_descendants", True):
                    children = (
                        set(
                            s.scalars(
                                select(BusinessModule.id).where(
                                    BusinessModule.project_id == pid, BusinessModule.parent_id.in_(frontier)
                                )
                            )
                        )
                        - mids
                    )
                    mids.update(children)
                    frontier = children
                allowed = set(s.scalars(select(ModuleEntity.entity_id).where(ModuleEntity.module_id.in_(mids))))
                snapshots = set(
                    s.scalars(select(ModuleSource.source_snapshot_id).where(ModuleSource.module_id.in_(mids)))
                )
                connectors = list(
                    s.scalars(
                        select(SourceConnector.key)
                        .join(ModuleConnector, ModuleConnector.connector_id == SourceConnector.id)
                        .where(ModuleConnector.module_id.in_(mids), SourceConnector.project_id == pid)
                    )
                )
                if connectors:
                    snapshots.update(
                        s.scalars(
                            select(SourceSnapshot.id).where(
                                SourceSnapshot.project_id == pid, SourceSnapshot.source_key.in_(connectors)
                            )
                        )
                    )
                if snapshots:
                    for batch in chunks(visible.values()):
                        allowed.update(
                            s.scalars(
                                select(Revision.entity_id)
                                .join(Artifact, Revision.artifact_id == Artifact.id)
                                .where(Revision.id.in_(batch), Artifact.snapshot_id.in_(snapshots))
                            )
                        )
                visible = {eid: rid for eid, rid in visible.items() if eid in allowed}
            found = []
            source_keys = (
                dict(
                    s.execute(
                        select(SourceSnapshot.id, SourceSnapshot.service_key).where(SourceSnapshot.project_id == pid)
                    ).all()
                )
                if payload.get("service_key")
                else {}
            )
            for batch in chunks(visible.values()):
                statement = (
                    select(
                        Revision.id,
                        Revision.entity_id,
                        Revision.name,
                        Revision.content,
                        Revision.content_sha256,
                        Revision.semantic_content_sha256,
                        literal("1.0"),
                        Revision.metadata_json,
                        Revision.locator,
                        Entity.stable_key,
                        Entity.type,
                        Artifact.relative_path,
                        Artifact.snapshot_id,
                    )
                    .join(Entity, Revision.entity_id == Entity.id)
                    .outerjoin(Artifact, Revision.artifact_id == Artifact.id)
                    .where(Revision.id.in_(batch))
                )
                if payload.get("kinds"):
                    statement = statement.where(Entity.type.in_(payload["kinds"]))
                if self.service.engine.dialect.name == "sqlite":
                    # SQLite's builtin LOWER does not implement Unicode
                    # casefold. The deterministic connection UDF preserves the
                    # exact existing search behavior while reducing row DTOs.
                    statement = statement.where(
                        or_(
                            *(
                                func.vks_casefold(column).contains(query, autoescape=True)
                                for column in (
                                    Revision.name,
                                    Entity.stable_key,
                                    Revision.content,
                                    Artifact.relative_path,
                                )
                            ),
                            *(
                                func.vks_json_casefold(cast(column, Text)).contains(query, autoescape=True)
                                for column in (Revision.metadata_json, Revision.locator)
                            ),
                        )
                    )
                elif self.service.engine.dialect.name == "postgresql" and re.fullmatch(r"[A-Za-z0-9_./-]+", query):
                    # Conservative SQL candidates only. Unicode/escaped JSON
                    # always falls back to the exact Python casefold below.
                    columns = (
                        Revision.name,
                        Entity.stable_key,
                        Revision.content,
                        Artifact.relative_path,
                        cast(Revision.metadata_json, Text),
                        cast(Revision.locator, Text),
                    )
                    statement = statement.where(
                        or_(
                            *(
                                clause
                                for column in columns
                                for clause in (
                                    func.lower(column).contains(query, autoescape=True),
                                    column.op("~")(r"[^\x20-\x7e]"),
                                    column.contains("\\u", autoescape=True),
                                )
                            )
                        )
                    )
                for (
                    rid,
                    eid,
                    name,
                    content,
                    content_hash,
                    semantic_hash,
                    schema,
                    metadata,
                    locator,
                    stable_key,
                    kind,
                    path,
                    snapshot_id,
                ) in s.execute(statement):
                    path = path or ""
                    service_key = metadata.get("service_key") or source_keys.get(snapshot_id)
                    if payload.get("service_key") and payload["service_key"] != service_key:
                        continue
                    direct = (name, stable_key, path)
                    exact = any(query in value.casefold() for value in direct)
                    if (
                        not exact
                        and query not in content.casefold()
                        and query not in canonical(metadata).casefold()
                        and query not in canonical(locator).casefold()
                    ):
                        continue
                    found.append(
                        (
                            rid,
                            {
                                "entity": {
                                    "entity_id": eid,
                                    "entity_revision_id": rid,
                                    "type": kind,
                                    "name": name,
                                    "content_sha256": content_hash,
                                    "semantic_content_sha256": semantic_hash,
                                    "schema_version": schema,
                                },
                                "excerpt": content[:2000],
                                "evidence_ids": [],
                                "match_kind": "exact" if exact else "fulltext",
                            },
                        )
                    )
            found.sort(
                key=lambda pair: (
                    pair[1]["match_kind"] != "exact",
                    pair[1]["entity"]["name"],
                    pair[1]["entity"]["entity_id"],
                )
            )
            found = found[: max(1, min(int(payload.get("limit", 10)), 50))]
            ids = {rid for rid, _ in found}
            refs = defaultdict(set)
            if analysis:
                for relation in self.edge_rows(s, analysis.id, index, frontier=ids):
                    for rid in (relation["from_revision_id"], relation["to_revision_id"]):
                        if rid in ids:
                            refs[rid].update(relation["evidence_ids"])
                all_refs = set().union(*refs.values()) if refs else set()
                valid = self.visible_evidence(s, actor, pid, analysis.id, all_refs)
                for rid, row in found:
                    row["evidence_ids"] = sorted(refs[rid] & valid)
            return [row for _, row in found]

    def job_dto(self, s, actor, job):
        dto = self.service.job_dto(job)
        if not self.service._can(s, actor, job.project_id, "source:read") or not self.service._can(
            s, actor, job.project_id, "trace:read"
        ):
            hidden = {
                "issues",
                "analysis_metrics",
                "accepted_count",
                "snapshot_ids",
                "entity_count",
                "relation_count",
                "evidence_count",
                "source_file_count",
            }
            if "result" in dto:
                dto["result"] = {key: value for key, value in dto["result"].items() if key not in hidden}
            dto.pop("issue_count", None)
            dto["diagnostics_restricted"] = True
            if "error" in dto:
                dto["error"] = {key: value for key, value in dto["error"].items() if key in ("code", "retryable")} | {
                    "message": "任务诊断含受限来源信息，请联系有权人员查看"
                }
        return dto
