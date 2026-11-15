"""Immutable graph persistence, source snapshots, and incremental draft imports."""

from __future__ import annotations
from hashlib import sha256
import re
from pathlib import PurePosixPath
from sqlalchemy import String, JSON, Integer, ForeignKey, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, Request, Header
from .models import (
    Base,
    Identified,
    Project,
    Analysis,
    Artifact,
    Entity,
    Revision,
    Selection,
    Evidence,
    Relation,
    Job,
    Upload,
    uid,
)
from packages.analyzers.vks_ingest import _entity


class SourceSnapshot(Identified, Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (UniqueConstraint("project_id", "source_key", "provider", "provider_revision", "content_sha256"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(96))
    provider: Mapped[str] = mapped_column(String(12))
    provider_revision: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    service_key: Mapped[str] = mapped_column(String(96), default="")


class SourceConnector(Identified, Base):
    __tablename__ = "source_connector"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    key: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(16))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class AnalysisSource(Base):
    __tablename__ = "analysis_set_source"
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), primary_key=True)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshot.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))


def error(code, message, status=422, details=None):
    from .service import DomainError

    raise DomainError(code, message, status, details)


def document_origin(s, revision):
    """Resolve legacy identities through preserved original artifacts, never snapshot IDs."""
    if revision.metadata_json.get("logical_source_key"):
        return revision.metadata_json["logical_source_key"]
    artifact_id = revision.metadata_json.get("original_artifact_id") or revision.artifact_id
    seen = set()
    while artifact_id and artifact_id not in seen:
        seen.add(artifact_id)
        artifact = s.get(Artifact, artifact_id)
        if not artifact:
            break
        original = artifact.metadata_json.get("original_artifact_id")
        if original and original != artifact_id:
            artifact_id = original
            continue
        source = s.get(SourceSnapshot, artifact.snapshot_id)
        return source.source_key if source else None
    return None


def same_prototype_action(item, previous):
    metadata = item["metadata"]
    prior = previous.metadata_json
    return bool(
        metadata.get("prototype_action")
        and prior.get("prototype_action")
        and metadata.get("page_key") == prior.get("page_key")
        and metadata.get("event") == prior.get("event")
        and metadata.get("status") == prior.get("status")
        and item.get("display_name") == previous.name
        and item.get("content", "") == previous.content
    )


def _prototype_state_keys(manifest):
    states = {}
    for state in manifest["states"]:
        for anchor in state["anchors"]:
            states.setdefault(anchor["actionKey"], set()).add(state["stateKey"])
    return states


def _retain_offline_layout(service, s, pid, item, prior, incoming):
    """An offline directory refresh has no new observation of captured states."""
    from .prototype_edit import _artifact
    from .service import canonical

    prior_manifest = prior.metadata_json["prototype_manifest"]
    current = item["metadata"]["prototype_manifest"]
    current.update(
        defaultStateKey=prior_manifest["defaultStateKey"],
        states=prior_manifest["states"],
        routeAliases=list(dict.fromkeys(current["routeAliases"] + prior_manifest["routeAliases"]))[:16],
    )
    origin = s.get(Artifact, prior.artifact_id)
    if origin.id == incoming.id:
        return origin
    return _artifact(
        s,
        service,
        pid,
        origin.snapshot_id,
        "prototype-pages",
        canonical(current).encode(),
        "fixture",
        "application/json",
        {
            "original_artifact_id": origin.metadata_json.get("original_artifact_id") or origin.id,
            "prototype_surface_artifact_ids": origin.metadata_json.get("prototype_surface_artifact_ids", []),
            "prototype_input_artifact_ids": [incoming.id],
        },
    )


def _require_import_revision(service, s, actor, pid, revision):
    if actor and revision and not service._visible_revision(s, actor, pid, revision):
        error("VKS_NOT_FOUND", "未找到可更新的导入目标", 404)


def _require_reused_selections(service, s, actor, pid, aid, entities):
    if not actor:
        return
    incoming = {(row["type"], row["stable_key"]) for row in entities}
    for revision, entity in s.execute(
        select(Revision, Entity)
        .join(Selection, Selection.entity_revision_id == Revision.id)
        .join(Entity, Entity.id == Selection.entity_id)
        .where(Selection.analysis_set_id == aid)
    ):
        if (entity.type, entity.stable_key) in incoming:
            _require_import_revision(service, s, actor, pid, revision)


def _require_import_relation(service, s, actor, pid, relation):
    if not actor:
        return
    _require_import_revision(service, s, actor, pid, s.get(Revision, relation.from_revision_id))
    _require_import_revision(service, s, actor, pid, s.get(Revision, relation.to_revision_id))
    if not service._visible_relation(s, actor, pid, relation) or not all(
        service._visible_evidence(s, actor, pid, s.get(Evidence, key)) for key in relation.evidence_ids
    ):
        error("VKS_NOT_FOUND", "未找到可更新的导入关系", 404)


def _unchanged_handbook_input(s, pid, sources, revision, path, incoming):
    source_key = incoming["source_id"]
    if revision.metadata_json.get("source_kind") == "platform_draft":
        artifact_id = revision.metadata_json.get("imported_artifact_id")
        if not artifact_id:
            # Old manual revisions only recorded their original import. Keep them
            # when every recorded snapshot agrees on this file's exact bytes.
            snapshots = [row["source_snapshot_id"] for row in sources if row.get("source_id") == source_key]
            versions = set(
                s.scalars(
                    select(Artifact.sha256).where(
                        Artifact.project_id == pid, Artifact.snapshot_id.in_(snapshots), Artifact.relative_path == path
                    )
                )
            )
            if versions != {incoming["sha256"]}:
                return None
            artifact_id = revision.metadata_json.get("original_artifact_id")
    else:
        artifact_id = revision.artifact_id
    original = s.get(Artifact, artifact_id) if artifact_id else None
    snapshot = s.get(SourceSnapshot, original.snapshot_id) if original else None
    if (
        original
        and original.project_id == pid
        and original.kind == "markdown"
        and original.relative_path == path
        and original.sha256 == incoming["sha256"]
        and snapshot
        and snapshot.project_id == pid
        and snapshot.source_key == source_key
        and snapshot.role == "documentation"
    ):
        return original
    return None


def persist_bundle(
    service,
    s,
    pid,
    owner,
    bundle,
    analysis_id=None,
    *,
    complete_sources=None,
    actor=None,
    staged_sources=None,
    reuse_imported_manifest=False,
):
    from copy import deepcopy

    from .documents import DocumentService, assert_section_locks, match_sections, parse_sections
    from .reading import ReadingService, artifact_policy
    from .service import canonical, sha
    from .source_sync import source_scope

    # Parser/checkpoint results may be reused; only this transaction's copy is adapted.
    bundle = deepcopy(bundle)

    manifest = bundle.manifest
    fingerprint = sha(canonical(manifest))
    a = (
        s.get(Analysis, analysis_id)
        if analysis_id
        else s.scalar(select(Analysis).where(Analysis.project_id == pid, Analysis.key == manifest["baseline_key"]))
    )
    if a and a.project_id != pid:
        error("VKS_NOT_FOUND", "未找到基线", 404)
    if a and not analysis_id:
        if any(x.get("import_manifest_sha256") == fingerprint for x in a.sources):
            _require_reused_selections(service, s, actor, pid, a.id, bundle.entities)
            return {
                "result_refs": [{"kind": "analysis_set", "id": a.id}],
                "analysis_set_id": a.id,
                "accepted_count": len(bundle.artifacts),
                "reused": True,
                "issues": bundle.issues,
            }
        error("VKS_STATE_CONFLICT", "基线已存在且内容不同，请使用新的 baseline_key", 409)
    if a and a.state == "frozen":
        error("VKS_STATE_CONFLICT", "发布版不可写入，请先克隆为草稿", 409)
    if (
        a
        and analysis_id
        and not complete_sources
        and (
            reuse_imported_manifest
            or manifest.get("prototypeManifest")
            or manifest.get("prototypeManifests")
            or any(source["source_id"] == "charge-offline-handbook" for source in bundle.sources)
        )
        and any(row.get("import_manifest_sha256") == fingerprint for row in (a.sources if staged_sources is None else staged_sources))
    ):
        _require_reused_selections(service, s, actor, pid, a.id, bundle.entities)
        return {
            "result_refs": [{"kind": "analysis_set", "id": a.id}],
            "analysis_set_id": a.id,
            "accepted_count": len(bundle.artifacts),
            "reused": True,
            "issues": bundle.issues,
        }
    if not a:
        a = Analysis(
            id=uid(),
            project_id=pid,
            key=manifest["baseline_key"],
            name=manifest["baseline_key"],
            state="draft",
            sources=[],
            selection_version=1,
            created_by=owner,
            runtime_alignment="unknown",
        )
        s.add(a)
        s.flush()
    # Internal reindex bundles may predate recorded scope. Preserve that unknown
    # state; only a verified material manifest can establish a new scope.
    incoming_scopes = source_scope([source for source in bundle.sources if source.get("root") is not None])
    if complete_sources:
        # Profile consent proves the incoming snapshot, not this target draft.
        # Bind deletion to both its recorded source vector and exact selected
        # revisions; never retrofit unverifiable scope onto historical revisions.
        for row in a.sources:
            key = row.get("source_id")
            if key in complete_sources and row.get("source_scope") != complete_sources[key]:
                error(
                    "VKS_SYNC_SCOPE_CHANGED", "目标草稿的完整来源范围无法与同步授权对齐，请从已验证原材料重建基线", 409
                )
        for revision, snapshot in s.execute(
            select(Revision, SourceSnapshot)
            .join(Selection, Selection.entity_revision_id == Revision.id)
            .join(Artifact, Artifact.id == Revision.artifact_id)
            .join(SourceSnapshot, SourceSnapshot.id == Artifact.snapshot_id)
            .where(Selection.analysis_set_id == a.id, SourceSnapshot.source_key.in_(complete_sources))
        ):
            scope = dict(complete_sources[snapshot.source_key], source_id=snapshot.source_key)
            vector = next((row for row in a.sources if row.get("source_snapshot_id") == snapshot.id), None)
            if (
                not vector
                or vector.get("source_id") != snapshot.source_key
                or vector.get("source_scope") != complete_sources[snapshot.source_key]
                or revision.metadata_json.get("source_scope") != scope
            ):
                error("VKS_SYNC_SCOPE_CHANGED", "目标修订缺少可证明的来源范围，请从已验证原材料重建基线", 409)
    removed_revisions, aliases, inherited_policies = set(), {}, {}
    prototype_format = bundle.analysis_metrics.get("prototype_import", {}).get("format")
    handbook_refresh = prototype_format == "charge-handbook" and not bundle.relations and not bundle.evidence
    retained_documents = {}
    retained_document_artifacts = {}
    material_records = {item["path"]: item for item in bundle.artifacts}
    material_sources = {item["path"]: item["source_id"] for item in bundle.artifacts}
    permissions = ReadingService(service).permissions(s, actor, pid) if actor else None
    for item in bundle.entities:
        if item["type"] != "section":
            continue
        entity = s.scalar(
            select(Entity).where(
                Entity.project_id == pid, Entity.type == "section", Entity.stable_key == item["stable_key"]
            )
        )
        choice = s.get(Selection, (a.id, entity.id)) if entity else None
        previous = (
            s.get(Revision, choice.entity_revision_id)
            if choice
            else s.scalar(select(Revision).where(Revision.entity_id == entity.id).limit(1))
            if entity
            else None
        )
        if previous and previous.metadata_json.get("document_key") != item.get("metadata", {}).get("document_key"):
            error("VKS_STATE_CONFLICT", "章节逻辑键属于其他文档；请使用来源前缀或明确的身份迁移", 409)
    for item in bundle.entities:
        if item["type"] != "document":
            continue
        document = s.scalar(
            select(Entity).where(
                Entity.project_id == pid, Entity.type == "document", Entity.stable_key == item["stable_key"]
            )
        )
        selected = s.get(Selection, (a.id, document.id)) if document else None
        prior = (
            s.get(Revision, selected.entity_revision_id)
            if selected
            else s.scalar(
                select(Revision)
                .where(Revision.entity_id == document.id)
                .order_by(Revision.created_at.desc(), Revision.id.desc())
                .limit(1)
            )
            if document
            else None
        )
        if selected:
            _require_import_revision(service, s, actor, pid, prior)
        incoming_source = material_sources.get(item.get("artifact_path"))
        if prior and document_origin(s, prior) != incoming_source:
            error(
                "VKS_STATE_CONFLICT",
                "文档逻辑键与另一来源冲突；请添加来源前缀，如 backend/README.md 与 frontend/README.md",
                409,
            )
        item["metadata"] = dict(item.get("metadata", {}), logical_source_key=incoming_source)
        if not prior:
            continue
        old = prior
        previous = DocumentService(service)._sections(s, document, old, a.id if selected else None)
        incoming = [
            row
            for row in bundle.entities
            if row["type"] == "section" and row.get("metadata", {}).get("document_key") == item["stable_key"]
        ]
        path = item["artifact_path"]
        original = (
            _unchanged_handbook_input(
                s, pid, a.sources if staged_sources is None else staged_sources, old, path, material_records[path]
            )
            if selected and handbook_refresh
            else None
        )
        if original:
            original_policy = artifact_policy(s, original)
            if permissions is not None and not ReadingService.policy_visible(
                original.kind, original.sensitivity, original_policy, *permissions
            ):
                error("VKS_NOT_FOUND", "未找到可更新的导入制品", 404)
            for chapter in previous:
                choice = s.get(Selection, (a.id, chapter.entity_id))
                if not choice or choice.entity_revision_id != chapter.id:
                    error("VKS_STATE_CONFLICT", "已选文档与章节修订不一致，请重新核对", 409)
            retained_documents[item["stable_key"]] = len(incoming) + 1
            kept = s.get(Artifact, old.artifact_id)
            retained_document_artifacts[path] = kept
            kept_policy = artifact_policy(s, kept)
            inherited_policies[path] = {
                flag: bool(original_policy[flag] or kept_policy[flag])
                for flag in ("requires_source_read", "requires_trace_read")
            }
            continue
        rows = parse_sections(item["content"], old.name)
        by_key = {s.get(Entity, row.entity_id).stable_key: row for row in previous}
        explicit = {
            index
            for index, chapter in enumerate(incoming)
            if chapter.get("metadata", {}).get("identity_origin") == "explicit"
        }
        matched = {
            index: by_key[incoming[index]["stable_key"]]
            for index in explicit
            if incoming[index]["stable_key"] in by_key
        }
        claimed = {row.entity_id for row in matched.values()}
        inferred_indices = [index for index in range(len(rows)) if index not in explicit]
        inferred, _, ambiguities = match_sections(
            [row for row in previous if row.entity_id not in claimed], [rows[index] for index in inferred_indices]
        )
        matched.update({inferred_indices[index]: row for index, row in inferred.items()})
        ambiguities = [dict(row, ordinal=inferred_indices[row["ordinal"]]) for row in ambiguities]
        for index in explicit:
            rows[index]["content"] = incoming[index]["content"]  # Declared ranges may span child headings.
        locked = set(old.metadata_json.get("locked_section_ids", []))
        assert_section_locks(previous, rows, matched, locked)
        reserved = {s.get(Entity, row.entity_id).stable_key for row in previous}
        allocated = reserved | {row["stable_key"] for row in incoming}
        for index, chapter in enumerate(incoming):
            prior = matched.get(index)
            original = chapter["stable_key"]
            stable = original
            if index in explicit:
                stable = original
            elif prior:
                stable = s.get(Entity, prior.entity_id).stable_key
            elif stable in reserved:
                stable = item["stable_key"] + ".section." + uid()
                while stable in allocated:
                    stable = item["stable_key"] + ".section." + uid()
            aliases[original] = stable
            chapter["stable_key"] = stable
            chapter["locator"]["sectionKey"] = stable
            chapter["metadata"]["manual_lock"] = bool(prior and prior.entity_id in locked)
            reserved.add(stable)
            allocated.add(stable)
        retained = {row["stable_key"] for row in incoming}
        for chapter in previous:
            if selected and s.get(Entity, chapter.entity_id).stable_key not in retained:
                removed_revisions.add(chapter.id)
                s.delete(s.get(Selection, (a.id, chapter.entity_id)))
        changed = old.content != item["content"]
        metadata = dict(old.metadata_json, **item.get("metadata", {}))
        metadata.update(
            locked_section_ids=sorted(locked),
            row_version=old.metadata_json.get("row_version", 1) + int(changed),
            original_artifact_id=old.metadata_json.get("original_artifact_id", old.artifact_id),
            identity_ambiguities=ambiguities,
        )
        metadata.pop("governed", None)
        metadata.pop("workflow_state", None)
        item["metadata"] = metadata
        original_artifact = s.get(Artifact, old.artifact_id)
        if original_artifact:
            inherited_policies[item["artifact_path"]] = {
                flag: bool(artifact_policy(s, original_artifact).get(flag))
                for flag in ("requires_source_read", "requires_trace_read")
            }
    for relation in bundle.relations:
        relation["from_key"] = aliases.get(relation["from_key"], relation["from_key"])
        relation["to_key"] = aliases.get(relation["to_key"], relation["to_key"])
    for evidence in bundle.evidence:
        loc = evidence.get("locator", {})
        if loc.get("sectionKey") in aliases:
            loc["sectionKey"] = aliases[loc["sectionKey"]]
    if complete_sources:
        # Only the source-sync service can authorize absence as deletion. Ordinary
        # uploads remain incremental. Ownership comes from the selected revision's
        # immutable source artifact, never its name or an inferred latest revision.
        incoming = {(item["type"], item["stable_key"]) for item in bundle.entities}
        for choice, entity, revision, snapshot in s.execute(
            select(Selection, Entity, Revision, SourceSnapshot)
            .join(Entity, Entity.id == Selection.entity_id)
            .join(Revision, Revision.id == Selection.entity_revision_id)
            .join(Artifact, Artifact.id == Revision.artifact_id)
            .join(SourceSnapshot, SourceSnapshot.id == Artifact.snapshot_id)
            .where(Selection.analysis_set_id == a.id, SourceSnapshot.source_key.in_(complete_sources))
        ):
            scope = complete_sources[snapshot.source_key]
            if (snapshot.provider, snapshot.role, snapshot.service_key or snapshot.source_key) != (
                scope["provider"],
                scope["role"],
                scope["service_key"],
            ):
                error("VKS_SYNC_SCOPE_CHANGED", "草稿中的来源身份与同步范围不一致，请重新核对", 409)
            if (entity.type, entity.stable_key) not in incoming:
                _require_import_revision(service, s, actor, pid, revision)
                removed_revisions.add(revision.id)
                s.delete(choice)
    sources = list(a.sources if staged_sources is None else staged_sources)
    previous_sources = {row.get("source_snapshot_id"): row for row in sources}
    if complete_sources:
        sources = [row for row in sources if row.get("source_id") not in complete_sources]
        for link in s.scalars(select(AnalysisSource).where(AnalysisSource.analysis_set_id == a.id)):
            if s.get(SourceSnapshot, link.source_snapshot_id).source_key in complete_sources:
                s.delete(link)
        s.flush()
    snapshot_by_source = {}
    for source in bundle.sources:
        revision = source.get("revision") or source.get("provider_revision") or source["content_sha256"]
        snapshot = s.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.project_id == pid,
                SourceSnapshot.source_key == source["source_id"],
                SourceSnapshot.provider == source["provider"],
                SourceSnapshot.provider_revision == revision,
                SourceSnapshot.content_sha256 == source["content_sha256"],
            )
        )
        if not snapshot:
            snapshot = SourceSnapshot(
                id=uid(),
                project_id=pid,
                source_key=source["source_id"],
                provider=source["provider"],
                provider_revision=revision,
                content_sha256=source["content_sha256"],
                role=source["role"],
                service_key=source.get("service_key", source["source_id"]),
            )
            s.add(snapshot)
            s.flush()
        snapshot_by_source[source["source_id"]] = snapshot.id
        if not s.get(AnalysisSource, (a.id, snapshot.id)):
            s.add(AnalysisSource(analysis_set_id=a.id, source_snapshot_id=snapshot.id, role=source["role"]))
        row = {
            "source_snapshot_id": snapshot.id,
            "source_id": source["source_id"],
            "role": source["role"],
            "provider": source["provider"],
            "provider_revision": revision,
            "content_sha256": source["content_sha256"],
            "service_key": source.get("service_key", source["source_id"]),
            "import_manifest_sha256": fingerprint,
        }
        if source["source_id"] in incoming_scopes:
            row["source_scope"] = incoming_scopes[source["source_id"]]
        if source.get("version_labels"):
            row["version_labels"] = dict(source["version_labels"])
        existing_vector = previous_sources.get(snapshot.id)
        if existing_vector and existing_vector.get("version_labels", {}) != row.get("version_labels", {}):
            error("VKS_VERSION_CONFLICT", "相同来源快照的版本标签发生冲突，请建立新基线", 412)
        if existing_vector and existing_vector.get("source_scope") not in (None, row.get("source_scope")):
            error("VKS_SYNC_SCOPE_CHANGED", "相同来源快照声明了不同采集范围，请建立准确的新基线", 409)
        if any(x.get("source_snapshot_id") == snapshot.id for x in sources):
            sources = [row if x.get("source_snapshot_id") == snapshot.id else x for x in sources]
        else:
            sources.append(row)
    artifacts = {}
    for item in bundle.artifacts:
        raw = item["content"]
        h = service._put_object(raw)
        sid = snapshot_by_source[item["source_id"]]
        art = s.scalar(
            select(Artifact).where(
                Artifact.project_id == pid, Artifact.snapshot_id == sid, Artifact.relative_path == item["path"]
            )
        )
        if not art:
            art = Artifact(
                id=uid(),
                project_id=pid,
                snapshot_id=sid,
                relative_path=item["path"],
                sha256=h,
                size_bytes=len(raw),
                kind=item["kind"],
                media_type=item.get("media_type", "application/octet-stream"),
                sensitivity=item.get("sensitivity", "internal"),
                object_key=h,
                metadata_json={
                    "encoding": item.get("detected_encoding", item.get("encoding", "utf-8")),
                    "allow_platform_llm": bool(manifest.get("allow_platform_llm", False)),
                    "parse_status": item.get("parse_status", "parsed"),
                    "parse_error_code": item.get("parse_error_code"),
                },
            )
            s.add(art)
            s.flush()
        elif art.sha256 != h:
            error("VKS_INVALID_EVIDENCE", "固定来源快照的字节发生冲突")
        else:
            if permissions is not None and not ReadingService.policy_visible(
                art.kind, art.sensitivity, artifact_policy(s, art), *permissions
            ):
                error("VKS_NOT_FOUND", "未找到可更新的导入制品", 404)
            # Deduplicated bytes never relax a prior policy, and a restrictive
            # re-import must not inherit an older permission to send to models.
            meta = dict(art.metadata_json)
            if not manifest.get("_preserve_material_policy"):
                meta["allow_platform_llm"] = bool(
                    meta.get("allow_platform_llm", False) and manifest.get("allow_platform_llm", False)
                )
            if item.get("sensitivity") == "confidential":
                art.sensitivity = "confidential"
            if item.get("kind") == "source":
                meta["requires_source_read"] = True
            if item.get("parse_status") == "failed":
                meta.update(parse_status="failed", parse_error_code=item.get("parse_error_code"))
            art.metadata_json = meta
        artifacts[item["path"]] = art
        if item["path"] in inherited_policies:
            art.metadata_json = {
                **art.metadata_json,
                **{
                    flag: bool(required or art.metadata_json.get(flag))
                    for flag, required in inherited_policies[item["path"]].items()
                },
            }
        if kept := retained_document_artifacts.get(item["path"]):
            policy = artifact_policy(s, art)
            kept.metadata_json = dict(
                kept.metadata_json,
                requires_source_read=bool(kept.metadata_json.get("requires_source_read") or policy["requires_source_read"]),
                requires_trace_read=bool(kept.metadata_json.get("requires_trace_read") or policy["requires_trace_read"]),
                allow_platform_llm=bool(
                    kept.metadata_json.get("allow_platform_llm") and art.metadata_json.get("allow_platform_llm")
                ),
            )
    prototype_refs = (
        [manifest["prototypeManifest"]] if manifest.get("prototypeManifest") else manifest.get("prototypeManifests", [])
    )
    for prototype in prototype_refs:
        sidecar = artifacts[prototype["path"]]
        surfaces = {}
        pages = [
            item
            for item in bundle.entities
            if item.get("artifact_path") == prototype["path"] and item["metadata"].get("prototype_manifest")
        ]
        for item in pages:
            for state in item["metadata"]["prototype_manifest"]["states"]:
                surface = state.get("surface")
                if not surface:
                    continue
                art = artifacts[surface["artifactPath"]]
                if art.kind != "screenshot" or art.media_type != "image/png" or art.snapshot_id != sidecar.snapshot_id:
                    error("VKS_STATE_CONFLICT", "固定制品的类型与声明的原型画面冲突", 409)
                surfaces[art.id] = art
        if sidecar.kind != "fixture":
            error("VKS_STATE_CONFLICT", "固定制品的类型与原型清单冲突", 409)
        # The manifest and its action/page names depend on every surface in the
        # page. Re-evaluate those same artifacts after later policy tightening.
        if pages:
            sidecar.metadata_json = dict(
                sidecar.metadata_json,
                prototype_surface_artifact_ids=sorted(
                    set(surfaces) | set(sidecar.metadata_json.get("prototype_surface_artifact_ids", []))
                ),
            )
    if point_ref := manifest.get("businessPointsManifest"):
        point_art = artifacts[point_ref["path"]]
        ids = {
            artifacts[path].id for item in bundle.entities for path in item["metadata"].pop("prototype_input_paths", [])
        }
        point_art.metadata_json = dict(
            point_art.metadata_json,
            prototype_input_artifact_ids=sorted(
                ids | set(point_art.metadata_json.get("prototype_input_artifact_ids", []))
            ),
        )
    entity_map = {}
    replaced, stable_semantics, action_state_changes = {}, set(), {}
    offline_directory = prototype_format == "charge-prototype"
    operations = {"added": 0, "updated": 0, "reused": sum(retained_documents.values())}
    # Sections are immutable first; a document's full hash then includes their
    # exact ordered revision IDs, without circular section->document hashes.
    for item in sorted(bundle.entities, key=lambda row: row["type"] == "document"):
        if (
            item["type"] == "document" and item["stable_key"] in retained_documents
            or item["type"] == "section" and item["metadata"].get("document_key") in retained_documents
        ):
            continue
        entity = s.scalar(
            select(Entity).where(
                Entity.project_id == pid, Entity.type == item["type"], Entity.stable_key == item["stable_key"]
            )
        )
        art = artifacts.get(item.get("artifact_path"))
        content = item.get("content", "")
        metadata = dict(item.get("metadata", {}))
        selection = s.get(Selection, (a.id, entity.id)) if entity else None
        selected = s.get(Revision, selection.entity_revision_id) if selection else None
        _require_import_revision(service, s, actor, pid, selected)
        if entity and item["type"] in ("page", "action"):
            prototype_entity = bool(metadata.get("prototype_manifest") or metadata.get("prototype_action"))
            previous = selected or s.scalar(
                select(Revision)
                .where(Revision.entity_id == entity.id)
                .order_by(Revision.created_at.desc(), Revision.id.desc())
                .limit(1)
            )
            if previous:
                prior = previous.metadata_json
                was_prototype = bool(prior.get("prototype_manifest") or prior.get("prototype_action"))
                if (prototype_entity or was_prototype) and (
                    prototype_entity != was_prototype
                    or prior.get("source_scope", {}).get("source_id") != material_sources.get(item.get("artifact_path"))
                    or prior.get("page_key") != metadata.get("page_key")
                ):
                    error("VKS_STATE_CONFLICT", "原型逻辑键已属于其他页面或来源", 409)
        if selected and metadata.get("prototype_manifest"):
            prior_manifest = selected.metadata_json["prototype_manifest"]
            if offline_directory and any(
                metadata["prototype_manifest"].get(key) != prior_manifest.get(key) for key in ("defaultStateKey", "states")
            ):
                art = _retain_offline_layout(service, s, pid, item, selected, art)
            prior_states = _prototype_state_keys(prior_manifest)
            incoming_states = _prototype_state_keys(metadata["prototype_manifest"])
            for action, states in prior_states.items():
                current = incoming_states.get(action, set())
                if states - current:
                    action_state_changes[action] = (states - current, bool(current))
        if not entity:
            entity = Entity(id=uid(), project_id=pid, type=item["type"], stable_key=item["stable_key"])
            s.add(entity)
            s.flush()
        if art:
            source_key = material_sources[item["artifact_path"]]
            if source_key in incoming_scopes:
                metadata["source_scope"] = dict(incoming_scopes[source_key], source_id=source_key)
            else:
                metadata.pop("source_scope", None)
        if item["type"] == "document":
            chapters = sorted(
                (
                    row
                    for row in bundle.entities
                    if row["type"] == "section" and row.get("metadata", {}).get("document_key") == item["stable_key"]
                ),
                key=lambda row: row["metadata"]["ordinal"],
            )
            metadata = DocumentService(service).with_sections(
                s, entity, art, [entity_map[row["stable_key"]] for row in chapters], metadata
            )
            if handbook_refresh:
                metadata["imported_artifact_id"] = art.id
        locator = item.get("locator", {})
        name = item.get("display_name") or item["stable_key"]
        full = {
            "artifact_id": art.id if art else None,
            "locator": locator,
            "metadata": metadata,
            "content": content,
            "name": name,
            "schema_version": "1.0",
        }
        h = sha(canonical(full))
        revision = None
        if selected and same_prototype_action(item, selected):
            stable_semantics.add(selected.id)
            old_policy = artifact_policy(s, s.get(Artifact, selected.artifact_id))
            new_policy = artifact_policy(s, art)
            if all(not new_policy[flag] or old_policy[flag] for flag in ("requires_source_read", "requires_trace_read")):
                revision = selected
        if not revision:
            revision = s.scalar(select(Revision).where(Revision.entity_id == entity.id, Revision.content_sha256 == h))
        if not revision:
            revision = Revision(
                id=uid(),
                project_id=pid,
                entity_id=entity.id,
                artifact_id=art.id if art else None,
                name=name,
                content=content,
                content_sha256=h,
                semantic_content_sha256=sha(content),
                locator=locator,
                metadata_json=metadata,
            )
            s.add(revision)
            s.flush()
        if selection:
            if selection.entity_revision_id != revision.id:
                replaced[selection.entity_revision_id] = revision.id
                selection.entity_revision_id = revision.id
                operations["updated"] += 1
            else:
                operations["reused"] += 1
        else:
            s.add(Selection(analysis_set_id=a.id, entity_id=entity.id, entity_revision_id=revision.id))
            operations["added"] += 1
        entity_map[item["stable_key"]] = revision
    s.flush()
    state_changes_by_id = {}
    for key, changes in action_state_changes.items():
        action = s.scalar(select(Entity).where(Entity.project_id == pid, Entity.type == "action", Entity.stable_key == key))
        choice = s.get(Selection, (a.id, action.id)) if action else None
        if choice:
            state_changes_by_id[choice.entity_revision_id] = changes
    # Retain decisions while changed endpoints become stale. Never mutate another baseline.
    for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == a.id)):
        endpoints = {relation.from_revision_id, relation.to_revision_id}
        start = replaced.get(relation.from_revision_id, relation.from_revision_id)
        end = replaced.get(relation.to_revision_id, relation.to_revision_id)
        missing = bool(endpoints & removed_revisions)
        affected = bool((endpoints & replaced.keys()) - stable_semantics)
        if start in state_changes_by_id:
            lost_states, still_located = state_changes_by_id[start]
            missing = missing or not still_located
            if relation.relation_type == "has_business_point":
                affected = affected or bool(
                    lost_states.intersection(relation.metadata_json.get("prototype_scope", {}).get("states", []))
                )
        validity = "broken" if missing else "stale" if affected else relation.validity_status
        if validity != relation.validity_status or start != relation.from_revision_id or end != relation.to_revision_id:
            _require_import_relation(service, s, actor, pid, relation)
            relation.from_revision_id = start
            relation.to_revision_id = end
            relation.validity_status = validity
            if validity != "valid" and relation.causal_basis == "correlated_runtime":
                relation.causal_basis = "not_established"
            relation.row_version += 1
    evidence_map = {}
    for item in bundle.evidence:
        art = artifacts[item["artifact_path"]]
        loc = item.get("locator", {})
        locator = {"relative_path": item["artifact_path"]}
        # Persist producer provenance with this immutable evidence revision.
        # Reserved fields are emitted as DTO metadata, never source locators.
        locator["_extractor_version"] = item.get("extractor_version", "vks-explicit-1")
        for old, new in (
            ("startLine", "line_start"),
            ("endLine", "line_end"),
            ("line_start", "line_start"),
            ("line_end", "line_end"),
            ("stepKey", "step_key"),
            ("requestId", "request_id"),
            ("traceId", "trace_id"),
        ):
            if old in loc:
                locator[new] = loc[old]
        content_hash = item.get("sha256", art.sha256)
        candidates = s.scalars(
            select(Evidence).where(
                Evidence.analysis_set_id == a.id,
                Evidence.stable_key == item["stable_key"],
                Evidence.artifact_id == art.id,
                Evidence.content_sha256 == content_hash,
            )
        )
        ev = next(
            (
                row
                for row in candidates
                if row.locator == locator
                and row.evidence_kind == item["kind"]
                and row.excerpt == item.get("content", "")[:30000]
                and row.claim == item.get("claim", "")
            ),
            None,
        )
        if not ev:
            ev = Evidence(
                id=uid(),
                project_id=pid,
                analysis_set_id=a.id,
                artifact_id=art.id,
                stable_key=item["stable_key"],
                evidence_kind=item["kind"],
                content_sha256=content_hash,
                locator=locator,
                excerpt=item.get("content", "")[:30000],
                claim=item.get("claim", ""),
            )
            s.add(ev)
            s.flush()
        evidence_map[item["stable_key"]] = ev
    for item in bundle.relations:
        fr = entity_map[item["from_key"]]
        to = entity_map[item["to_key"]]
        existing = s.scalar(
            select(Relation).where(
                Relation.analysis_set_id == a.id,
                Relation.from_revision_id == fr.id,
                Relation.to_revision_id == to.id,
                Relation.relation_type == item["type"],
            )
        )
        if existing:
            current_evidence = [evidence_map[key].id for key in item["evidence_keys"]]
            if existing.evidence_ids != current_evidence:
                _require_import_relation(service, s, actor, pid, existing)
                existing.evidence_ids = current_evidence
                existing.validity_status = "stale"
                if existing.causal_basis == "correlated_runtime":
                    existing.causal_basis = "not_established"
                existing.row_version += 1
            continue
        rel = Relation(
            id=uid(),
            project_id=pid,
            analysis_set_id=a.id,
            from_revision_id=fr.id,
            to_revision_id=to.id,
            relation_type=item["type"],
            excluded=False,
            validity_status="valid",
            causal_basis=item.get("causal_basis", "not_established"),
            evidence_ids=[evidence_map[k].id for k in item["evidence_keys"]],
            reason=item.get("reason", ""),
            row_version=1,
            metadata_json={"prototype_scope": item["prototype_scope"]} if "prototype_scope" in item else {},
        )
        s.add(rel)
    if staged_sources is None:
        a.sources = sources
        a.selection_version += 1
        a.state = "draft"
    else:
        staged_sources[:] = sources
    service.audit(
        s, owner, pid, "import.commit", a.id, metadata={"entities": len(entity_map), "relations": len(bundle.relations)}
    )
    return {
        "result_refs": [{"kind": "analysis_set", "id": a.id}],
        "analysis_set_id": a.id,
        "accepted_count": len(bundle.artifacts),
        "snapshot_ids": list(snapshot_by_source.values()),
        "issues": bundle.issues,
        "reused": False,
        "operations": operations,
    }


class ImportService:
    def __init__(self, service):
        self.service = service

    def enqueue(self, actor, pid, payload, key):
        service = self.service
        with service.db() as s:
            p = service.require(s, actor, pid, "source:import")
            aid = payload.get("analysis_set_id")
            a = s.get(Analysis, aid)
            if not a or a.project_id != pid:
                error("VKS_NOT_FOUND", "未找到目标分析集合", 404)
            if a.state == "frozen":
                error("VKS_STATE_CONFLICT", "发布版不可导入，请创建新草稿", 409)
            if payload["kind"] == "prototype" and len(payload["artifact_ids"]) != 1:
                error("VKS_INVALID_INPUT", "原型导入一次只支持一个对象")
            for artifact_id in payload["artifact_ids"]:
                service._upload_data(s, actor, pid, artifact_id)
            return service.idem(
                s,
                actor,
                pid,
                "import",
                key,
                payload,
                lambda: service.job_dto(service._new_job(s, actor, pid, "import", payload)),
            )

    def prepare(self, job_id=None, *, actor=None, pid=None, payload=None):
        from types import SimpleNamespace
        from packages.analyzers.offline_prototype import offline_bundle
        from packages.analyzers.prototype_manifest import uploaded_png_bundle
        from packages.analyzers.vks_ingest import BundleError, _read_zip
        from packages.collector.snapshots import CollectionError, Snapshot, bundle_bytes

        service = self.service
        with service.db() as s:
            job = (
                s.get(Job, job_id)
                if job_id
                else SimpleNamespace(id=uid(), project_id=pid, input_json=payload, created_by=actor.id)
            )
            if not job:
                error("VKS_NOT_FOUND", "未找到导入任务", 404)
            p = s.get(Project, job.project_id)
            a = s.get(Analysis, job.input_json.get("analysis_set_id"))
            if not p or not a or a.project_id != p.id:
                error("VKS_NOT_FOUND", "未找到目标基线", 404)
            if actor:
                service.require(s, actor, p.id, "source:import")
            if a.state == "frozen":
                error("VKS_STATE_CONFLICT", "发布基线不能写入", 409)
            uploads = []
            for artifact_id in job.input_json["artifact_ids"]:
                upload = s.scalar(
                    select(Upload).where(
                        Upload.artifact_id == artifact_id,
                        Upload.project_id == p.id,
                        Upload.created_by == job.created_by,
                        Upload.complete.is_(True),
                    )
                )
                if not upload:
                    error("VKS_NOT_FOUND", "未找到授权上传对象", 404)
                uploads.append({"sha256": upload.sha256, "size_bytes": upload.size_bytes, "filename": upload.filename})
            project_key = p.key
            baseline_key = a.key
            import_kind = job.input_json["kind"]
            native_prototype = job.input_json.get("_native_prototype", False)
        # Archive reads and parsing run outside the write transaction so local
        # cancellation and revocation requests can acquire the application lock.
        from packages.analyzers.cancellation import checkpoint

        files = {}
        for upload in uploads:
            checkpoint()
            try:
                raw = (service.data_dir / "objects" / upload["sha256"]).read_bytes()
            except OSError:
                from .service import DomainError

                raise DomainError(
                    "VKS_DEPENDENCY_UNAVAILABLE", "上传对象存储暂不可用", 503, retryable=True
                ) from None
            if sha256(raw).hexdigest() != upload["sha256"] or len(raw) != upload["size_bytes"]:
                error("VKS_INVALID_INPUT", "上传对象完整性失败")
            name = PurePosixPath(upload["filename"].replace("\\", "/")).name
            suffix = PurePosixPath(name).suffix.lower()
            if name in files:
                error("VKS_INVALID_INPUT", "上传文件名重复")
            if import_kind == "prototype" and suffix in (".html", ".htm"):
                try:
                    offline = offline_bundle(raw, project_key, baseline_key)
                except BundleError as exc:
                    error("VKS_INVALID_INPUT", exc.message, 422, {"reason_code": exc.code, **exc.details})
                if offline:
                    packed, report = offline
                    parsed = service._parse(packed, project_key)
                    parsed.analysis_metrics["prototype_import"] = report
                    return parsed
            if import_kind == "prototype" and native_prototype and suffix == ".png":
                try:
                    return service._parse(uploaded_png_bundle(raw, name, project_key, baseline_key), project_key)
                except BundleError as exc:
                    error("VKS_INVALID_INPUT", exc.message, 422, {"reason_code": exc.code, **exc.details})
            if import_kind == "documents" and suffix not in (".md", ".markdown", ".json", ".yaml", ".yml"):
                error("VKS_INVALID_INPUT", "文档支持 Markdown/OpenAPI")
            if import_kind == "prototype" and suffix == ".zip":
                try:
                    parts = _read_zip(raw, require_manifest=False)
                except BundleError as exc:
                    error("VKS_INVALID_INPUT", exc.message, 422, {"reason_code": exc.code, **exc.details})
                if "manifest.json" in parts:
                    if len(uploads) != 1:
                        error("VKS_INVALID_INPUT", "正式 bundle 必须作为独立材料导入")
                    parsed = service._parse(raw, project_key)
                    if parsed.manifest["baseline_key"] != baseline_key:
                        error("VKS_INVALID_INPUT", "原型包的 baseline_key 必须与目标草稿一致")
                    return parsed
                if set(parts).intersection(files):
                    error("VKS_INVALID_INPUT", "原型材料含重复路径")
                files.update(parts)
            elif import_kind == "prototype" and suffix not in (".html", ".htm", ".png", ".jpg", ".jpeg", ".webp"):
                error("VKS_INVALID_INPUT", "原型支持 HTML、截图和安全 ZIP")
            else:
                files[name] = raw
        snapshot = Snapshot("local", "", files)
        try:
            data = bundle_bytes(
                snapshot,
                project_key,
                baseline_key,
                source_key="uploads",
                role="documentation" if import_kind == "documents" else "prototype",
            )
        except (BundleError, CollectionError) as exc:
            error("VKS_INVALID_INPUT", exc.message, 422, {"reason_code": exc.code})
        parsed = service._parse(data, project_key)
        for artifact in parsed.artifacts:
            path = artifact["path"]
            suffix = PurePosixPath(path).suffix.lower()
            if artifact.get("parse_status") == "failed" or (
                artifact["kind"] not in ("prototype", "screenshot") and suffix != ".htm"
            ):
                continue
            page_key = "page.upload." + sha256(path.encode()).hexdigest()[:24]
            if not any(e["type"] == "page" and e.get("artifact_path") == path for e in parsed.entities):
                encoding = artifact.get("detected_encoding", "utf-8").replace("-bom", "")
                content = artifact["content"].decode("utf-8-sig" if encoding == "utf-8" else encoding) if suffix in (".html", ".htm") else ""
                parsed.entities.append(
                    _entity(
                        page_key,
                        "page",
                        PurePosixPath(path).stem,
                        content,
                        path,
                        {"kind": "file", "relative_path": path},
                        {
                            "preview_mode": "snapshot",
                            "source_kind": "manual",
                            "artifact_kind": artifact["kind"],
                            "runtime_source_alignment": "unknown",
                        },
                    )
                )
        return parsed

    def preview(self, actor, pid, payload):
        parsed = self.prepare(actor=actor, pid=pid, payload=payload)

        return {
            "valid": True,
            "file_count": len(parsed.artifacts),
            "size_bytes": sum(x["size_bytes"] for x in parsed.artifacts),
            "entity_count": len(parsed.entities),
            "relation_count": len(parsed.relations),
            "source_count": len(parsed.sources),
            "issues": parsed.issues,
            "allow_platform_llm": False,
            "baseline_key": parsed.manifest["baseline_key"],
            **(
                {"prototype_import": parsed.analysis_metrics["prototype_import"]}
                if "prototype_import" in parsed.analysis_metrics
                else {}
            ),
        }


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloneInput(StrictInput):
    name: str = Field(min_length=1, max_length=160)


class ConnectorInput(StrictInput):
    key: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    provider: str = Field(pattern=r"^(local|git|svn|upload)$")
    remote_locator: str = Field(default="", max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=128)


class KnowledgeService:
    def __init__(self, service):
        self.service = service

    def clone(self, actor, pid, aid, name, key):
        service = self.service
        with service.db() as s:
            service.require(s, actor, pid, "knowledge:edit")
            old = s.get(Analysis, aid)
            if not old or old.project_id != pid:
                error("VKS_NOT_FOUND", "未找到基线", 404)
            from .reading import ReadingService

            rows = service._revisions(s, aid)
            ReadingService(service).preload(s, rows)
            if any(not service._visible_revision(s, actor, pid, r) for r in rows):
                error("VKS_FORBIDDEN", "克隆完整基线需要相应来源访问权限", 403)

            def create():
                target = Analysis(
                    id=uid(),
                    project_id=pid,
                    key="draft-" + uid(),
                    name=name,
                    state="draft",
                    sources=list(old.sources),
                    selection_version=1,
                    created_by=actor.id,
                    runtime_alignment=old.runtime_alignment,
                    ruleset_version=old.ruleset_version,
                )
                s.add(target)
                s.flush()
                from .document_presentation import clone_presentation

                clone_presentation(s, old, target)
                for choice in s.scalars(select(Selection).where(Selection.analysis_set_id == aid)):
                    s.add(
                        Selection(
                            analysis_set_id=target.id,
                            entity_id=choice.entity_id,
                            entity_revision_id=choice.entity_revision_id,
                        )
                    )
                for link in s.scalars(select(AnalysisSource).where(AnalysisSource.analysis_set_id == aid)):
                    s.add(
                        AnalysisSource(
                            analysis_set_id=target.id, source_snapshot_id=link.source_snapshot_id, role=link.role
                        )
                    )
                evidence_ids = {}
                for ev in s.scalars(select(Evidence).where(Evidence.analysis_set_id == aid)):
                    copy = Evidence(
                        id=uid(),
                        project_id=pid,
                        analysis_set_id=target.id,
                        artifact_id=ev.artifact_id,
                        stable_key=ev.stable_key,
                        evidence_kind=ev.evidence_kind,
                        content_sha256=ev.content_sha256,
                        locator=ev.locator,
                        excerpt=ev.excerpt,
                        claim=ev.claim,
                    )
                    s.add(copy)
                    evidence_ids[ev.id] = copy.id
                for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
                    s.add(
                        Relation(
                            id=uid(),
                            project_id=pid,
                            analysis_set_id=target.id,
                            from_revision_id=relation.from_revision_id,
                            to_revision_id=relation.to_revision_id,
                            relation_type=relation.relation_type,
                            excluded=relation.excluded,
                            validity_status=relation.validity_status,
                            causal_basis=relation.causal_basis,
                            evidence_ids=[evidence_ids[e] for e in relation.evidence_ids],
                            reason=relation.reason,
                            row_version=1,
                            manual_lock=relation.manual_lock,
                            metadata_json=dict(relation.metadata_json or {}),
                        )
                    )
                s.flush()
                from .scenarios import ScenarioService

                changed = ScenarioService(service).clone_for_analysis(s, actor, old, target)
                from .journeys import JourneyService

                JourneyService(service).clone_for_analysis(s, actor, target, changed)
                service.audit(s, actor, pid, "analysis.clone", target.id, metadata={"from_analysis_set_id": aid})
                return service.analysis_public(s, actor, target)

            return service.idem(s, actor, pid, "analysis.clone", key, {"analysis_set_id": aid, "name": name}, create)

    def connectors(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            return [
                self.connector_dto(c)
                for c in s.scalars(select(SourceConnector).where(SourceConnector.project_id == pid))
            ]

    def connector_dto(self, c):
        return {
            "id": c.id,
            "key": c.key,
            "name": c.name,
            "provider": c.provider,
            "read_only": True,
            "row_version": c.row_version,
            **c.metadata_json,
        }

    def add_connector(self, actor, pid, payload):
        from urllib.parse import urlsplit

        locator = payload.get("remote_locator", "")
        url = urlsplit(locator)
        if url.username or url.password or re.search(r"(?i)(token|password|secret|authorization)=", locator):
            error("VKS_INVALID_INPUT", "来源地址不得包含凭据，请使用凭证引用")
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            c = SourceConnector(
                id=uid(),
                project_id=pid,
                key=payload["key"],
                name=payload["name"],
                provider=payload["provider"],
                metadata_json={
                    "remote_locator": locator,
                    "secret_ref": payload.get("secret_ref"),
                    "collection_mode": "client-read-only",
                },
                row_version=1,
            )
            s.add(c)
            s.flush()
            self.service.audit(s, actor, pid, "source.create", c.id)
            return self.connector_dto(c)

    def snapshots(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            rows = s.scalars(select(SourceSnapshot).where(SourceSnapshot.project_id == pid)).all()
            return self.service._visible_sources(
                s,
                actor,
                pid,
                [
                    {
                        "source_snapshot_id": r.id,
                        "source_id": r.source_key,
                        "role": r.role,
                        "provider": r.provider,
                        "provider_revision": r.provider_revision,
                        "content_sha256": r.content_sha256,
                        "service_key": r.service_key,
                    }
                    for r in rows
                ],
            )


def install_knowledge(app, service, actor, envelope, API="/api/v1"):

    knowledge = KnowledgeService(service)

    @app.post(API + "/projects/{pid}/analysis-sets/{aid}/clone", status_code=201)
    def clone(
        pid: str, aid: str, payload: CloneInput, request: Request, a=Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, knowledge.clone(a, pid, aid, payload.name, idempotency_key))

    @app.get(API + "/projects/{pid}/source-connectors")
    def connectors(pid: str, request: Request, a=Depends(actor)):
        return envelope(request, knowledge.connectors(a, pid))

    @app.post(API + "/projects/{pid}/source-connectors", status_code=201)
    def add_connector(pid: str, payload: ConnectorInput, request: Request, a=Depends(actor)):
        return envelope(request, knowledge.add_connector(a, pid, payload.model_dump()))

    @app.get(API + "/projects/{pid}/source-snapshots")
    def snapshots(pid: str, request: Request, a=Depends(actor)):
        return envelope(request, knowledge.snapshots(a, pid))

    return knowledge
