"""Immutable graph persistence, source snapshots, and incremental draft imports."""

from __future__ import annotations
from hashlib import sha256
from io import BytesIO
import json
import re
from pathlib import PurePosixPath
from uuid import uuid4
import zipfile
from sqlalchemy import String, Text, JSON, Integer, ForeignKey, UniqueConstraint, select
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
from packages.analyzers.vks_ingest import ParsedBundle, parse_bundle, source_digest, _entity


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


def persist_bundle(service, s, pid, owner, bundle, analysis_id=None):
    from .service import sha, canonical
    from .documents import DocumentService, DocumentWorkflow, assert_section_locks, match_sections, parse_sections
    from copy import deepcopy
    from .reading import artifact_policy

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
    if not a:
        a = Analysis(
            id=uid(),
            project_id=pid,
            key=manifest["baseline_key"],
            name=manifest["baseline_key"],
            state="ready_for_review",
            sources=[],
            selection_version=1,
            created_by=owner,
            runtime_alignment="unknown",
        )
        s.add(a)
        s.flush()
    removed_revisions, aliases, inherited_policies = set(), {}, {}
    material_sources = {item["path"]: item["source_id"] for item in bundle.artifacts}
    for item in bundle.entities:
        if item["type"] != "section":
            continue
        entity = s.scalar(select(Entity).where(Entity.project_id == pid, Entity.type == "section",
                                               Entity.stable_key == item["stable_key"]))
        choice = s.get(Selection, (a.id, entity.id)) if entity else None
        previous = (s.get(Revision, choice.entity_revision_id) if choice else
                    s.scalar(select(Revision).where(Revision.entity_id == entity.id).limit(1)) if entity else None)
        if previous and previous.metadata_json.get("document_key") != item.get("metadata", {}).get("document_key"):
            error("VKS_STATE_CONFLICT", "章节逻辑键属于其他文档；请使用来源前缀或明确的身份迁移", 409)
    for item in bundle.entities:
        if item["type"] != "document":
            continue
        document = s.scalar(select(Entity).where(Entity.project_id == pid, Entity.type == "document",
                                                 Entity.stable_key == item["stable_key"]))
        selected = s.get(Selection, (a.id, document.id)) if document else None
        prior = (s.get(Revision, selected.entity_revision_id) if selected else
                 s.scalar(select(Revision).where(Revision.entity_id == document.id)
                          .order_by(Revision.created_at.desc(), Revision.id.desc()).limit(1)) if document else None)
        incoming_source = material_sources.get(item.get("artifact_path"))
        if prior and document_origin(s, prior) != incoming_source:
            error("VKS_STATE_CONFLICT", "文档逻辑键与另一来源冲突；请添加来源前缀，如 backend/README.md 与 frontend/README.md", 409)
        item["metadata"] = dict(item.get("metadata", {}), logical_source_key=incoming_source)
        if not prior:
            continue
        old = prior
        previous = DocumentService(service)._sections(s, document, old, a.id if selected else None)
        rows = parse_sections(item["content"], old.name)
        matched, _, ambiguities = match_sections(previous, rows)
        locked = set(old.metadata_json.get("locked_section_ids", []))
        assert_section_locks(previous, rows, matched, locked)
        incoming = [row for row in bundle.entities if row["type"] == "section"
                    and row.get("metadata", {}).get("document_key") == item["stable_key"]]
        for index, chapter in enumerate(incoming):
            prior = matched.get(index)
            if prior:
                stable = s.get(Entity, prior.entity_id).stable_key
                aliases[chapter["stable_key"]] = stable
                chapter["stable_key"] = stable
                chapter["locator"]["sectionKey"] = stable
                chapter["metadata"]["manual_lock"] = prior.entity_id in locked
        retained = {row["stable_key"] for row in incoming}
        for chapter in previous:
            if selected and s.get(Entity, chapter.entity_id).stable_key not in retained:
                removed_revisions.add(chapter.id)
                s.delete(s.get(Selection, (a.id, chapter.entity_id)))
        changed = old.content != item["content"]
        workflow = s.scalar(select(DocumentWorkflow).where(DocumentWorkflow.analysis_set_id == a.id,
                                                          DocumentWorkflow.document_id == document.id))
        metadata = dict(old.metadata_json, **item.get("metadata", {}))
        metadata.update(locked_section_ids=sorted(locked),
                        governed=bool(changed or workflow or old.metadata_json.get("governed")),
                        row_version=old.metadata_json.get("row_version", 1) + int(changed),
                        original_artifact_id=old.metadata_json.get("original_artifact_id", old.artifact_id),
                        identity_ambiguities=ambiguities)
        if metadata["governed"]:
            metadata["workflow_state"] = "editing" if changed else old.metadata_json.get("workflow_state", "editing")
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
    sources = list(a.sources)
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
        if not any(x.get("source_snapshot_id") == snapshot.id for x in sources):
            sources.append(row)
    a.sources = sources
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
            art.metadata_json = {**art.metadata_json, **{
                flag: bool(required or art.metadata_json.get(flag))
                for flag, required in inherited_policies[item["path"]].items()
            }}
    entity_map = {}
    replaced = {}
    for item in bundle.entities:
        entity = s.scalar(
            select(Entity).where(
                Entity.project_id == pid, Entity.type == item["type"], Entity.stable_key == item["stable_key"]
            )
        )
        if not entity:
            entity = Entity(id=uid(), project_id=pid, type=item["type"], stable_key=item["stable_key"])
            s.add(entity)
            s.flush()
        art = artifacts.get(item.get("artifact_path"))
        content = item.get("content", "")
        metadata = item.get("metadata", {})
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
        selection = s.get(Selection, (a.id, entity.id))
        if selection:
            if selection.entity_revision_id != revision.id:
                replaced[selection.entity_revision_id] = revision.id
                selection.entity_revision_id = revision.id
        else:
            s.add(Selection(analysis_set_id=a.id, entity_id=entity.id, entity_revision_id=revision.id))
        entity_map[item["stable_key"]] = revision
        if entity.type == "document" and metadata.get("governed"):
            workflow = s.scalar(select(DocumentWorkflow).where(DocumentWorkflow.analysis_set_id == a.id,
                                                              DocumentWorkflow.document_id == entity.id))
            if not workflow:
                workflow = DocumentWorkflow(id=uid(), project_id=pid, analysis_set_id=a.id,
                                            document_id=entity.id, revision_id=revision.id)
                s.add(workflow)
            if workflow.revision_id != revision.id or workflow.approval_sha256 != revision.content_sha256:
                workflow.revision_id = revision.id
                workflow.state = "editing"
                workflow.reviewed_by = None
                workflow.approval_sha256 = None
    s.flush()
    # Retain decisions while changed endpoints become stale. Never mutate another baseline.
    for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == a.id)):
        missing = bool({relation.from_revision_id, relation.to_revision_id} & removed_revisions)
        if missing or relation.from_revision_id in replaced or relation.to_revision_id in replaced:
            relation.from_revision_id = replaced.get(relation.from_revision_id, relation.from_revision_id)
            relation.to_revision_id = replaced.get(relation.to_revision_id, relation.to_revision_id)
            relation.validity_status = "broken" if missing else "stale"
            if relation.causal_basis == "correlated_runtime":
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
        locator["_redaction_version"] = "import-policy-v1"
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
        ev = s.scalar(
            select(Evidence).where(
                Evidence.analysis_set_id == a.id,
                Evidence.stable_key == item["stable_key"],
                Evidence.artifact_id == art.id,
                Evidence.content_sha256 == content_hash,
            )
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
            continue
        rel = Relation(
            id=uid(),
            project_id=pid,
            analysis_set_id=a.id,
            from_revision_id=fr.id,
            to_revision_id=to.id,
            relation_type=item["type"],
            review_status="candidate",
            validity_status="valid",
            causal_basis=item.get("causal_basis", "not_established"),
            evidence_ids=[evidence_map[k].id for k in item["evidence_keys"]],
            reason=item.get("reason", ""),
            row_version=1,
        )
        s.add(rel)
    a.selection_version += 1
    a.state = "ready_for_review"
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
        from packages.analyzers.vks_ingest import _read_zip
        from packages.collector.snapshots import Snapshot, bundle_bytes

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
        # Archive reads and parsing run outside the write transaction so local
        # cancellation and revocation requests can acquire the application lock.
        from packages.analyzers.cancellation import checkpoint

        files = {}
        for upload in uploads:
            checkpoint()
            raw = (service.data_dir / "objects" / upload["sha256"]).read_bytes()
            if sha256(raw).hexdigest() != upload["sha256"] or len(raw) != upload["size_bytes"]:
                error("VKS_INVALID_INPUT", "上传对象完整性失败")
            name = PurePosixPath(upload["filename"].replace("\\", "/")).name
            suffix = PurePosixPath(name).suffix.lower()
            if name in files:
                error("VKS_INVALID_INPUT", "上传文件名重复")
            if import_kind == "documents" and suffix not in (".md", ".markdown", ".json", ".yaml", ".yml"):
                error("VKS_INVALID_INPUT", "文档支持 Markdown/OpenAPI")
            if import_kind == "prototype" and suffix == ".zip":
                parts = _read_zip(raw, require_manifest=False)
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
        data = bundle_bytes(
            snapshot,
            project_key,
            baseline_key,
            source_key="uploads",
            role="documentation" if job.input_json["kind"] == "documents" else "prototype",
        )
        parsed = service._parse(data, project_key)
        for artifact in parsed.artifacts:
            path = artifact["path"]
            suffix = PurePosixPath(path).suffix.lower()
            if artifact["kind"] not in ("prototype", "screenshot") and suffix != ".htm":
                continue
            page_key = "page.upload." + sha256(path.encode()).hexdigest()[:24]
            if not any(e["type"] == "page" and e.get("artifact_path") == path for e in parsed.entities):
                content = artifact["content"].decode("utf-8-sig") if suffix in (".html", ".htm") else ""
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
            "requires_review": True,
            "baseline_key": parsed.manifest["baseline_key"],
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
            if any(not service._visible_revision(s, actor, pid, r) for r in service._revisions(s, aid)):
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
                            review_status=relation.review_status,
                            validity_status=relation.validity_status,
                            causal_basis=relation.causal_basis,
                            evidence_ids=[evidence_ids[e] for e in relation.evidence_ids],
                            reason=relation.reason,
                            row_version=1,
                            manual_lock=relation.manual_lock,
                        )
                    )
                s.flush()
                from .scenarios import ScenarioService

                ScenarioService(service).clone_for_analysis(s, actor, old, target)
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
    from fastapi import Depends, Request, Header
    from .service import Actor

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
