"""Immutable Markdown chapters, protected sections, and human document review."""

from __future__ import annotations

import json
import re
from collections import Counter
from hashlib import sha256
from typing import ClassVar, Literal

from fastapi import Depends, Header, Request
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .knowledge import AnalysisSource, SourceSnapshot
from .models import Analysis, Artifact, Base, Entity, Evidence, Identified, Relation, Revision, Selection, now, uid


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(code, message, status=422, details=None):
    from .service import DomainError

    raise DomainError(code, message, status, details)


class DocumentWorkflow(Identified, Base):
    __tablename__ = "document_workflow"
    __table_args__ = (UniqueConstraint("analysis_set_id", "document_id"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("entity.id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"))
    state: Mapped[str] = mapped_column(String(24), default="editing")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    submitted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approval_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    __mapper_args__: ClassVar[dict] = {"version_id_col": row_version}


class DocumentReview(Identified, Base):
    __tablename__ = "document_review"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    document_id: Mapped[str] = mapped_column(ForeignKey("entity.id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"))
    decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(36))
    base_version: Mapped[int] = mapped_column(Integer)


def parse_sections(markdown: str, fallback_title: str) -> list[dict]:
    """CommonMark AST, retaining whole raw blocks and zero executable HTML."""
    tokens = MarkdownIt("commonmark").parse(markdown)
    headings, parents = [], []
    lines = markdown.splitlines()
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        level = int(token.tag[1:])
        inline = tokens[index + 1]
        title = inline.content
        while parents and parents[-1][0] >= level:
            parents.pop()
        parents.append((level, title))
        headings.append(
            {
                "title": title,
                "heading_path": [x[1] for x in parents],
                "startLine": token.map[0] + 1,
                "headingEndLine": token.map[1],
            }
        )
    if not headings:
        headings = [{"title": fallback_title, "heading_path": [fallback_title], "startLine": 1, "headingEndLine": 0}]
    output = []
    for index, heading in enumerate(headings):
        end = headings[index + 1]["startLine"] - 1 if index + 1 < len(headings) else max(1, len(lines))
        body = "\n".join(lines[heading["headingEndLine"] : end]).strip()
        content = "\n".join(lines[heading["startLine"] - 1 : end])
        output.append(
            {
                **heading,
                "endLine": end,
                "content": content,
                "body_fingerprint": digest(body),
                "nonempty_body": bool(body),
                "ordinal": index,
            }
        )
    return output


def body_fingerprint(revision: Revision):
    if revision.metadata_json.get("body_fingerprint"):
        return revision.metadata_json["body_fingerprint"]
    lines = revision.content.splitlines()
    if lines and re.match(r"^ {0,3}#{1,6}\s", lines[0]):
        lines = lines[1:]
    return digest("\n".join(lines).strip())


def match_sections(previous, sections):
    old_path_counts = Counter(canonical(r.metadata_json.get("heading_path", [])) for r in previous)
    new_path_counts = Counter(canonical(row["heading_path"]) for row in sections)
    old_bodies = Counter(body_fingerprint(r) for r in previous)
    new_bodies = Counter(row["body_fingerprint"] for row in sections)
    matched, used, ambiguities = {}, set(), []
    for index, row in enumerate(sections):
        body_candidates = [
            r
            for r in previous
            if old_bodies[body_fingerprint(r)] == 1
            and new_bodies[row["body_fingerprint"]] == 1
            and body_fingerprint(r) == row["body_fingerprint"]
            and row["nonempty_body"]
        ]
        path = canonical(row["heading_path"])
        path_candidates = [
            r
            for r in previous
            if old_path_counts[path] == new_path_counts[path] == 1
            and canonical(r.metadata_json.get("heading_path", [])) == path
        ]
        candidate = body_candidates or path_candidates
        if len(candidate) == 1 and candidate[0].entity_id not in used:
            matched[index] = candidate[0]
            used.add(candidate[0].entity_id)
        elif any(r.name == row["title"] or body_fingerprint(r) == row["body_fingerprint"] for r in previous):
            ambiguities.append({"ordinal": index, "reason": "重复章节标题或正文无法唯一继承身份"})
    return matched, used, ambiguities


def assert_section_locks(previous, sections, matched, locked):
    by_id = {r.entity_id: r for r in previous}
    for locked_id in locked:
        current = next(
            (
                row
                for index, row in enumerate(sections)
                if matched.get(index) and matched[index].entity_id == locked_id
            ),
            None,
        )
        if current is None or current["content"].rstrip("\r\n") != by_id[locked_id].content.rstrip("\r\n"):
            fail("VKS_STATE_CONFLICT", "人工锁定章节不可改写；请先由人员解锁", 409)


class DocumentService:
    def __init__(self, service):
        self.service = service

    def _selected_document(self, s, actor, eid, aid, scope="knowledge:edit"):
        entity, analysis = s.get(Entity, eid), s.get(Analysis, aid)
        if not entity or not analysis or entity.type != "document" or entity.project_id != analysis.project_id:
            fail("VKS_NOT_FOUND", "未找到文档或分析集合", 404)
        self.service.require(s, actor, entity.project_id, scope)
        s.refresh(analysis, with_for_update=True)
        selection = s.get(Selection, (aid, eid))
        revision = s.get(Revision, selection.entity_revision_id) if selection else None
        if not revision or not self.service._visible_revision(s, actor, entity.project_id, revision):
            fail("VKS_NOT_FOUND", "未找到当前版本文档", 404)
        return entity, analysis, selection, revision

    def _sections(self, s, document, revision, aid=None):
        if aid:
            rows = self.service._revisions(s, aid)
        else:
            rows = list(
                s.scalars(
                    select(Revision).where(
                        Revision.project_id == document.project_id, Revision.artifact_id == revision.artifact_id
                    )
                )
            )
        return [
            r
            for r in rows
            if (
                s.get(Entity, r.entity_id).type == "section"
                and r.metadata_json.get("document_key") == document.stable_key
                and (
                    not r.metadata_json.get("document_revision_id")
                    or r.metadata_json.get("document_revision_id") == revision.id
                )
            )
        ]

    def _revision(self, s, entity, artifact, name, content, locator, metadata):
        payload = {
            "artifact_id": artifact.id,
            "name": name,
            "content": content,
            "locator": locator,
            "metadata": metadata,
            "schema_version": "1.0",
        }
        fingerprint = digest(canonical(payload))
        existing = s.scalar(
            select(Revision).where(Revision.entity_id == entity.id, Revision.content_sha256 == fingerprint)
        )
        if existing:
            return existing
        revision = Revision(
            id=uid(),
            project_id=entity.project_id,
            entity_id=entity.id,
            artifact_id=artifact.id,
            name=name,
            content=content,
            locator=locator,
            metadata_json=metadata,
            content_sha256=fingerprint,
            semantic_content_sha256=digest(content),
        )
        s.add(revision)
        s.flush()
        return revision

    def _artifact(self, s, analysis, document, old, markdown):
        from .reading import artifact_policy
        raw = markdown.encode("utf-8")
        content_hash = self.service._put_object(raw)
        source = s.get(Artifact, old.artifact_id) if old.artifact_id else None
        source_policy = artifact_policy(s, source) if source else {}
        inherited_policy = {
            "requires_source_read": bool(source and source_policy.get("requires_source_read")),
            "requires_trace_read": bool(source and source_policy.get("requires_trace_read")),
            "allow_platform_llm": bool(source and source.metadata_json.get("allow_platform_llm", False)),
            "sensitivity": source.sensitivity if source else "internal",
        }
        # Equal bytes may have an older, weaker disclosure/model policy. Keep
        # the logical draft source stable while selecting a distinct immutable
        # snapshot when inherited policy changes, even for a content rollback.
        provider_revision = content_hash + "-" + digest(canonical(inherited_policy))[:16]
        source_key = "document-draft-" + document.id
        path = old.locator.get("path") or (
            s.get(Artifact, old.artifact_id).relative_path if old.artifact_id else "documents/" + document.id + ".md"
        )
        snapshot_hash = digest(canonical([{"path": path, "sha256": content_hash, "size_bytes": len(raw)}]))
        snapshot = s.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.project_id == document.project_id,
                SourceSnapshot.source_key == source_key,
                SourceSnapshot.provider_revision == provider_revision,
                SourceSnapshot.content_sha256 == snapshot_hash,
            )
        )
        if not snapshot:
            snapshot = SourceSnapshot(
                id=uid(),
                project_id=document.project_id,
                source_key=source_key,
                provider="local",
                provider_revision=provider_revision,
                content_sha256=snapshot_hash,
                role="documentation",
                service_key="drafts",
            )
            s.add(snapshot)
            s.flush()
        if not s.get(AnalysisSource, (analysis.id, snapshot.id)):
            s.add(AnalysisSource(analysis_set_id=analysis.id, source_snapshot_id=snapshot.id, role="documentation"))
        if not any(row.get("source_snapshot_id") == snapshot.id for row in analysis.sources):
            analysis.sources = [
                *analysis.sources,
                {
                    "source_snapshot_id": snapshot.id,
                    "source_id": source_key,
                    "provider": "local",
                    "provider_revision": provider_revision,
                    "content_sha256": snapshot_hash,
                    "role": "documentation",
                    "service_key": "drafts",
                },
            ]
        artifact = s.scalar(
            select(Artifact).where(
                Artifact.project_id == document.project_id,
                Artifact.snapshot_id == snapshot.id,
                Artifact.relative_path == path,
            )
        )
        if not artifact:
            artifact = Artifact(
                id=uid(),
                project_id=document.project_id,
                snapshot_id=snapshot.id,
                relative_path=path,
                sha256=content_hash,
                size_bytes=len(raw),
                kind="markdown",
                media_type="text/markdown",
                sensitivity=inherited_policy["sensitivity"],
                object_key=content_hash,
                metadata_json={
                    "encoding": "utf-8",
                    "source_kind": "platform_draft",
                    "original_artifact_id": old.metadata_json.get("original_artifact_id", old.artifact_id),
                    "requires_source_read": bool(source and source_policy.get("requires_source_read")),
                    "requires_trace_read": bool(source and source_policy.get("requires_trace_read")),
                    "allow_platform_llm": bool(source and source.metadata_json.get("allow_platform_llm", False)),
                },
            )
            s.add(artifact)
            s.flush()
        return artifact

    def save_draft(self, actor, eid, payload, version):
        markdown = payload["markdown"]
        if len(markdown.encode()) > 1_000_000:
            fail("VKS_LIMIT_EXCEEDED", "文档草稿超过大小限制", 413)
        if re.search(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b",
            markdown,
        ) or re.search(
            r"""(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*["']([^"'\r\n]{8,})["']""",
            markdown,
        ):
            fail("VKS_INVALID_INPUT", "草稿包含疑似秘密，请先脱敏")
        with self.service.db() as s:
            document, analysis, selection, old = self._selected_document(s, actor, eid, payload["analysis_set_id"])
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布文档不可修改，请先创建草稿", 409)
            if old.id != payload["base_revision_id"]:
                fail("VKS_VERSION_CONFLICT", "文档修订已更新，请比较后重新保存", 412)
            self.service._version(old.metadata_json.get("row_version", 1), version)
            previous = self._sections(s, document, old, analysis.id)
            by_id = {r.entity_id: r for r in previous}
            aliases = {r.id: r.entity_id for r in previous}
            requested = {aliases.get(value, value) for value in payload.get("locked_section_ids", [])}
            if requested - set(by_id):
                fail("VKS_INVALID_INPUT", "锁定章节不属于当前文档")
            locked = set(old.metadata_json.get("locked_section_ids", []))
            if not actor.human and requested != locked:
                fail("VKS_FORBIDDEN", "机器身份不能更改人工章节锁", 403)
            sections = parse_sections(markdown, old.name)
            matched, used, ambiguities = match_sections(previous, sections)
            assert_section_locks(previous, sections, matched, locked & requested)
            artifact = self._artifact(s, analysis, document, old, markdown)
            metadata = dict(
                old.metadata_json,
                row_version=old.metadata_json.get("row_version", 1) + 1,
                source_kind="platform_draft",
                original_artifact_id=old.metadata_json.get("original_artifact_id", old.artifact_id),
                locked_section_ids=sorted(requested),
                section_count=len(sections),
                workflow_state="editing",
                identity_ambiguities=ambiguities,
            )
            new = self._revision(
                s,
                document,
                artifact,
                sections[0]["title"],
                markdown,
                {"kind": "file", "path": artifact.relative_path},
                metadata,
            )
            selection.entity_revision_id = new.id
            old_to_new, evidence_for_revision = {old.id: new.id}, {}
            for index, row in enumerate(sections):
                prior = matched.get(index)
                entity = (
                    s.get(Entity, prior.entity_id)
                    if prior
                    else Entity(
                        id=uid(),
                        project_id=document.project_id,
                        type="section",
                        stable_key=document.stable_key + ".section." + uid(),
                    )
                )
                if not prior:
                    s.add(entity)
                    s.flush()
                chapter_metadata = {
                    "document_key": document.stable_key,
                    "document_revision_id": new.id,
                    "heading_path": row["heading_path"],
                    "ordinal": row["ordinal"],
                    "body_fingerprint": row["body_fingerprint"],
                    "identity_origin": prior.metadata_json.get("identity_origin", "inherited") if prior else "new",
                    "manual_lock": entity.id in requested,
                }
                locator = {
                    "kind": "document_section",
                    "sectionKey": entity.stable_key,
                    "startLine": row["startLine"],
                    "endLine": row["endLine"],
                }
                chapter = self._revision(s, entity, artifact, row["title"], row["content"], locator, chapter_metadata)
                selected = s.get(Selection, (analysis.id, entity.id))
                if selected:
                    selected.entity_revision_id = chapter.id
                else:
                    s.add(Selection(analysis_set_id=analysis.id, entity_id=entity.id, entity_revision_id=chapter.id))
                if prior:
                    old_to_new[prior.id] = chapter.id
                evidence = Evidence(
                    id=uid(),
                    project_id=document.project_id,
                    analysis_set_id=analysis.id,
                    artifact_id=artifact.id,
                    stable_key="document-edit." + chapter.id,
                    evidence_kind="manual",
                    content_sha256=artifact.sha256,
                    locator={
                        "relative_path": artifact.relative_path,
                        "line_start": row["startLine"],
                        "line_end": row["endLine"],
                    },
                    excerpt=row["content"],
                    claim="人员或授权编辑者保存的独立 Markdown 草稿章节",
                )
                s.add(evidence)
                s.flush()
                evidence_for_revision[chapter.id] = evidence.id
            removed = [r for r in previous if r.entity_id not in used]
            for removed_revision in removed:
                old_selection = s.get(Selection, (analysis.id, removed_revision.entity_id))
                if old_selection:
                    s.delete(old_selection)
            for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == analysis.id)):
                old_from, old_to = relation.from_revision_id, relation.to_revision_id
                changed = old_from in old_to_new or old_to in old_to_new
                missing = any(rid in {r.id for r in removed} for rid in (old_from, old_to))
                if old_from in old_to_new:
                    relation.from_revision_id = old_to_new[old_from]
                if old_to in old_to_new:
                    relation.to_revision_id = old_to_new[old_to]
                if changed or missing:
                    relation.validity_status = "broken" if missing else "stale"
                    if relation.causal_basis == "correlated_runtime":
                        relation.causal_basis = "not_established"
                    relation.row_version += 1
                    extra = [
                        evidence_for_revision[rid]
                        for rid in (relation.from_revision_id, relation.to_revision_id)
                        if rid in evidence_for_revision
                    ]
                    relation.evidence_ids = list(dict.fromkeys([*relation.evidence_ids, *extra]))
            workflow = s.scalar(
                select(DocumentWorkflow).where(
                    DocumentWorkflow.analysis_set_id == analysis.id, DocumentWorkflow.document_id == document.id
                )
            )
            if not workflow:
                workflow = DocumentWorkflow(
                    id=uid(),
                    project_id=document.project_id,
                    analysis_set_id=analysis.id,
                    document_id=document.id,
                    revision_id=new.id,
                )
                s.add(workflow)
            workflow.revision_id = new.id
            workflow.state = "editing"
            workflow.reviewed_by = None
            workflow.approval_sha256 = None
            workflow.updated_at = now()
            analysis.selection_version += 1
            self.service.audit(
                s,
                actor,
                document.project_id,
                "document.save",
                new.id,
                metadata={
                    "section_count": len(sections),
                    "removed_sections": len(removed),
                    "identity_ambiguities": len(ambiguities),
                },
            )
            s.flush()
            return self._dto(s, document, new, analysis.id, workflow)

    def _dto(self, s, document, revision, aid=None, workflow=None):
        sections = self._sections(s, document, revision, aid)
        return {
            "document_id": document.id,
            "entity_revision_id": revision.id,
            "content_sha256": revision.content_sha256,
            "markdown": revision.content,
            "sections": [
                self.service.entity_dto(s, r, True)
                for r in sorted(sections, key=lambda r: r.metadata_json.get("ordinal", 0))
            ],
            "row_version": revision.metadata_json.get("row_version", 1),
            "original_artifact_id": revision.metadata_json.get("original_artifact_id", revision.artifact_id),
            "workflow": self.workflow_dto(workflow) if workflow else None,
            "identity_ambiguities": revision.metadata_json.get("identity_ambiguities", []),
        }

    def read_revision(self, actor, eid, rid):
        with self.service.db() as s:
            document, revision = s.get(Entity, eid), s.get(Revision, rid)
            if not document or not revision or document.type != "document" or revision.entity_id != eid:
                fail("VKS_NOT_FOUND", "未找到文档修订", 404)
            self.service.require(s, actor, document.project_id)
            if not self.service._visible_revision(s, actor, document.project_id, revision):
                fail("VKS_NOT_FOUND", "未找到文档修订", 404)
            if not self.service._can(s, actor, document.project_id, "knowledge:edit"):
                from .models import Release

                releases = s.scalars(select(Release).where(Release.project_id == document.project_id))
                if not any(
                    any(x["entity_revision_id"] == rid for x in rel.manifest_json["selections"]) for rel in releases
                ):
                    fail("VKS_NOT_FOUND", "未找到文档修订", 404)
            return self._dto(s, document, revision)

    @staticmethod
    def workflow_dto(workflow):
        return {
            "id": workflow.id,
            "document_id": workflow.document_id,
            "revision_id": workflow.revision_id,
            "state": workflow.state,
            "row_version": workflow.row_version,
            "submitted_by": workflow.submitted_by,
            "reviewed_by": workflow.reviewed_by,
            "approval_sha256": workflow.approval_sha256,
            "reason": workflow.reason,
            "updated_at": workflow.updated_at,
        }

    def workflow(self, actor, eid, aid):
        with self.service.db() as s:
            _document, _analysis, _, revision = self._selected_document(s, actor, eid, aid)
            workflow = s.scalar(
                select(DocumentWorkflow).where(
                    DocumentWorkflow.analysis_set_id == aid, DocumentWorkflow.document_id == eid
                )
            )
            return (
                self.workflow_dto(workflow)
                if workflow
                else {"document_id": eid, "revision_id": revision.id, "state": "imported", "row_version": 1}
            )

    def submit(self, actor, eid, payload, version):
        with self.service.db() as s:
            document, analysis, _, revision = self._selected_document(s, actor, eid, payload["analysis_set_id"])
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "发布版不可提交修改", 409)
            if revision.id != payload["revision_id"]:
                fail("VKS_VERSION_CONFLICT", "文档修订已更新", 412)
            self.service._version(revision.metadata_json.get("row_version", 1), version)
            workflow = s.scalar(
                select(DocumentWorkflow).where(
                    DocumentWorkflow.analysis_set_id == analysis.id, DocumentWorkflow.document_id == eid
                )
            )
            if not workflow:
                workflow = DocumentWorkflow(
                    id=uid(),
                    project_id=document.project_id,
                    analysis_set_id=analysis.id,
                    document_id=eid,
                    revision_id=revision.id,
                )
                s.add(workflow)
            if workflow.state in ("approved", "submitted") and workflow.revision_id == revision.id:
                fail("VKS_STATE_CONFLICT", "此修订已提交或批准", 409)
            workflow.revision_id = revision.id
            workflow.state = "submitted"
            workflow.submitted_by = actor.id
            workflow.reviewed_by = None
            workflow.approval_sha256 = None
            workflow.reason = payload["reason"]
            workflow.updated_at = now()
            s.add(
                DocumentReview(
                    project_id=document.project_id,
                    analysis_set_id=analysis.id,
                    document_id=eid,
                    revision_id=revision.id,
                    decision="submit",
                    reason=payload["reason"],
                    actor_id=actor.id,
                    base_version=revision.metadata_json.get("row_version", 1),
                )
            )
            self.service.audit(s, actor, document.project_id, "document.submit", revision.id)
            s.flush()
            return self.workflow_dto(workflow)

    def review(self, actor, eid, payload, version):
        with self.service.db() as s:
            document, analysis, _, revision = self._selected_document(
                s, actor, eid, payload["analysis_set_id"], "links:review"
            )
            if not actor.human:
                fail("VKS_FORBIDDEN", "文档批准需要人员会话", 403)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布文档不可审核", 409)
            workflow = s.scalar(
                select(DocumentWorkflow).where(
                    DocumentWorkflow.analysis_set_id == analysis.id, DocumentWorkflow.document_id == eid
                )
            )
            if not workflow or workflow.state != "submitted":
                fail("VKS_STATE_CONFLICT", "文档尚未提交审核", 409)
            self.service._version(workflow.row_version, version)
            if payload["revision_id"] != revision.id or workflow.revision_id != revision.id:
                fail("VKS_VERSION_CONFLICT", "审核后文档素材已变化", 412)
            workflow.state = "approved" if payload["decision"] == "approve" else "changes_requested"
            workflow.reviewed_by = actor.id
            workflow.approval_sha256 = revision.content_sha256 if workflow.state == "approved" else None
            workflow.reason = payload["reason"]
            workflow.updated_at = now()
            s.add(
                DocumentReview(
                    project_id=document.project_id,
                    analysis_set_id=analysis.id,
                    document_id=eid,
                    revision_id=revision.id,
                    decision=payload["decision"],
                    reason=payload["reason"],
                    actor_id=actor.id,
                    base_version=workflow.row_version,
                )
            )
            self.service.audit(
                s,
                actor,
                document.project_id,
                "document.review",
                revision.id,
                metadata={"decision": payload["decision"]},
            )
            s.flush()
            return self.workflow_dto(workflow)

    @staticmethod
    def assert_publishable(s, analysis):
        blockers = []
        for selection in s.scalars(select(Selection).where(Selection.analysis_set_id == analysis.id)):
            revision = s.get(Revision, selection.entity_revision_id)
            entity = s.get(Entity, selection.entity_id)
            if entity.type != "document":
                continue
            workflow = s.scalar(
                select(DocumentWorkflow).where(
                    DocumentWorkflow.analysis_set_id == analysis.id, DocumentWorkflow.document_id == entity.id
                )
            )
            if (not workflow and not revision.metadata_json.get("governed")
                    and revision.metadata_json.get("source_kind") not in ("platform_draft", "generated", "ai_generated")):
                continue
            if (
                not workflow
                or workflow.state != "approved"
                or workflow.revision_id != revision.id
                or workflow.approval_sha256 != revision.content_sha256
            ):
                blockers.append("文档修订尚未通过人员审核：" + entity.id)
        return blockers


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    revision_id: str
    reason: str = Field(min_length=1, max_length=2000)


class DocumentReviewInput(WorkflowInput):
    decision: Literal["approve", "request_changes"]


def install_documents(app, service, actor, envelope, API="/api/v1"):
    documents = DocumentService(service)
    actor_dependency = Depends(actor)

    @app.get(API + "/documents/{eid}/workflow")
    def workflow(eid: str, analysis_set_id: str, request: Request, a=actor_dependency):
        return envelope(request, documents.workflow(a, eid, analysis_set_id))

    @app.post(API + "/documents/{eid}/submit")
    def submit(eid: str, payload: WorkflowInput, request: Request, a=actor_dependency, if_match: str = Header(...)):
        return envelope(request, documents.submit(a, eid, payload.model_dump(), if_match))

    @app.post(API + "/documents/{eid}/review")
    def review(
        eid: str, payload: DocumentReviewInput, request: Request, a=actor_dependency, if_match: str = Header(...)
    ):
        return envelope(request, documents.review(a, eid, payload.model_dump(), if_match))
