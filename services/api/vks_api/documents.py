"""Immutable Markdown chapters, protected sections, and direct document editing."""

from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import nullcontext
from hashlib import sha256

from markdown_it import MarkdownIt
from sqlalchemy import select

from .knowledge import AnalysisSource, SourceSnapshot
from .models import (
    Analysis,
    Artifact,
    Entity,
    Evidence,
    Relation,
    Release,
    Revision,
    Selection,
    uid,
)


def digest(value):
    return sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(code, message, status=422, details=None):
    from .service import DomainError

    raise DomainError(code, message, status, details)


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
    elif headings[0]["startLine"] > 1 and "\n".join(lines[: headings[0]["startLine"] - 1]).strip():
        headings.insert(
            0, {"title": fallback_title, "heading_path": [fallback_title], "startLine": 1, "headingEndLine": 0}
        )
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
            (row for index, row in enumerate(sections) if matched.get(index) and matched[index].entity_id == locked_id),
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

    @staticmethod
    def _section_gap(reason):
        fail(
            "VKS_DOCUMENT_SECTIONS_UNRESOLVED",
            "文档修订的章节映射不完整或存在歧义；请核对冻结发布清单，不能以最新章节替代历史内容",
            409,
            {"reason": reason},
        )

    @classmethod
    def _mapped_sections(cls, s, document, artifact_id, ids):
        if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids) or len(ids) != len(set(ids)):
            cls._section_gap("invalid_mapping")
        # Keep the exact revisions and their identities alive for this transaction;
        # SQLAlchemy's weak identity map otherwise turns each check into another SELECT.
        from .reading import ReadingService

        retained = s.info.setdefault("vks_prefetched", {})
        missing = [rid for rid in ids if (Revision, rid) not in retained]
        retained.update({(Revision, row.id): row for row in ReadingService(None).revision_rows(s, missing)})
        rows = [retained.get((Revision, rid)) or s.get(Revision, rid) for rid in ids]
        identities = set()
        for row in rows:
            entity = (retained.get((Entity, row.entity_id)) or s.get(Entity, row.entity_id)) if row else None
            if (
                not row
                or not entity
                or entity.type != "section"
                or entity.project_id != document.project_id
                or row.project_id != document.project_id
                or row.artifact_id != artifact_id
                or row.metadata_json.get("document_key") != document.stable_key
                or row.entity_id in identities
            ):
                cls._section_gap("invalid_mapping")
            identities.add(row.entity_id)
        return rows

    @classmethod
    def with_sections(cls, s, document, artifact, rows, metadata):
        """Membership participates in the immutable document revision's full hash."""
        ids = [row.id for row in rows]
        cls._mapped_sections(s, document, artifact.id if artifact else None, ids)
        return dict(metadata, section_revision_ids=ids, section_count=len(ids))

    @classmethod
    def _legacy_sections(cls, s, document, revision, rows):
        # A legacy set must be complete and match this exact document's bytes
        # and locators. Multiple revisions of one entity are never collapsed.
        rows = [
            row
            for row in rows
            if s.get(Entity, row.entity_id).type == "section"
            and row.metadata_json.get("document_key") == document.stable_key
        ]
        expected = revision.metadata_json.get("section_count")
        if expected == 0 and not rows:
            return []
        headings = parse_sections(revision.content, revision.name)
        if len(rows) != len(headings) or (expected is not None and len(rows) != expected):
            cls._section_gap("ambiguous_legacy_sections")
        ordinals = [row.metadata_json.get("ordinal") for row in rows]
        if (
            any(type(index) is not int for index in ordinals)
            or set(ordinals) != set(range(len(headings)))
            or len(set(ordinals)) != len(ordinals)
        ):
            cls._section_gap("ambiguous_legacy_sections")
        rows.sort(key=lambda row: row.metadata_json["ordinal"])
        cls._mapped_sections(s, document, revision.artifact_id, [row.id for row in rows])
        lines = revision.content.splitlines()
        for row, heading in zip(rows, headings):
            start, end = row.locator.get("startLine"), row.locator.get("endLine")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start != heading["startLine"]
                or end < start
                or end > max(1, len(lines))
                or row.metadata_json.get("heading_path") != heading["heading_path"]
                or row.metadata_json.get("document_revision_id") not in (None, revision.id)
                or row.content.rstrip("\r\n") != "\n".join(lines[start - 1 : end]).rstrip("\r\n")
            ):
                cls._section_gap("invalid_legacy_sections")
        return rows

    @classmethod
    def _membership_mapping(cls, document, revision):
        ids = revision.metadata_json["section_revision_ids"]
        if (
            not isinstance(ids, list)
            or any(not isinstance(rid, str) for rid in ids)
            or len(ids) != len(set(ids))
            or revision.metadata_json.get("section_count") != len(ids)
        ):
            cls._section_gap("invalid_mapping")
        key = (document.id, document.project_id, document.stable_key, revision.id, revision.artifact_id, tuple(ids))
        return ids, key

    @classmethod
    def prefetch_section_members(cls, s, documents):
        """Validate complete maps in one SQL query, retaining per-document results.

        Invalid maps are deliberately left uncached: the ordinary resolver
        rejects only the affected item, without failing its healthy neighbours.
        No chapter bodies or permission decisions enter this snapshot cache.
        """
        if s.bind.dialect.name != "sqlite" or not (s.info.get("vks_read_only") or s.info.get("vks_completion_batch")):
            return
        from sqlalchemy import and_, func, true

        from .reading import chunks
        from .service import DomainError

        cache = s.info.setdefault("vks_mapped_section_members", set())
        pending = {}
        for document, revision in documents:
            if not document or document.type != "document" or "section_revision_ids" not in revision.metadata_json:
                continue
            try:
                ids, key = cls._membership_mapping(document, revision)
            except DomainError:
                continue
            if key not in cache:
                pending[key] = {
                    "pid": document.project_id,
                    "key": document.stable_key,
                    "artifact": revision.artifact_id,
                    "ids": ids,
                }
        for batch in chunks(pending, 200):
            values = (
                func.json_each(canonical([pending[key] for key in batch]))
                .table_valued("key", "value")
                .alias("documents")
            )
            ids = func.json_each(func.json_extract(values.c.value, "$.ids")).table_valued("value").alias("members")
            pid = func.json_extract(values.c.value, "$.pid")
            artifact = func.json_extract(values.c.value, "$.artifact")
            key = func.json_extract(values.c.value, "$.key")
            counts = s.execute(
                select(values.c.key, func.count(Entity.id), func.count(func.distinct(Entity.id)))
                .select_from(values)
                .outerjoin(ids, true())
                .outerjoin(
                    Revision,
                    and_(
                        Revision.id == ids.c.value,
                        Revision.project_id == pid,
                        Revision.artifact_id.is_not_distinct_from(artifact),
                        Revision.metadata_json["document_key"].as_string() == key,
                    ),
                )
                .outerjoin(
                    Entity, and_(Entity.id == Revision.entity_id, Entity.project_id == pid, Entity.type == "section")
                )
                .group_by(values.c.key)
            )
            for index, count, distinct in counts:
                mapping_key = batch[int(index)]
                if count == distinct == len(pending[mapping_key]["ids"]):
                    cache.add(mapping_key)

    @classmethod
    def section_members(cls, s, document, revision, wanted):
        """Check the ENTIRE exact mapping in SQL without hydrating every body.

        This has the same project, kind, artifact, document-key and unique
        identity checks as _mapped_sections. Legacy mappings retain their full
        resolver. Only the small requested membership set is returned.
        """
        if "section_revision_ids" not in revision.metadata_json or s.bind.dialect.name != "sqlite":
            return {row.id for row in cls._sections(s, document, revision)} & set(wanted)
        ids, key = cls._membership_mapping(document, revision)
        from sqlalchemy import func

        cache = s.info.setdefault("vks_mapped_section_members", set())
        scoped = s.info.get("vks_read_only") or s.info.get("vks_completion_batch")
        if not scoped or key not in cache:
            values = func.json_each(canonical(ids)).table_valued("value")
            count, distinct = s.execute(
                select(func.count(), func.count(func.distinct(Revision.entity_id)))
                .join(Entity, Entity.id == Revision.entity_id)
                .where(
                    Revision.id.in_(select(values.c.value)),
                    Entity.type == "section",
                    Entity.project_id == document.project_id,
                    Revision.project_id == document.project_id,
                    Revision.artifact_id == revision.artifact_id,
                    Revision.metadata_json["document_key"].as_string() == document.stable_key,
                )
            ).one()
            if count != len(ids) or distinct != len(ids):
                cls._section_gap("invalid_mapping")
            if scoped:
                cache.add(key)
        return set(ids) & set(wanted)

    @classmethod
    def _sections(cls, s, document, revision, aid=None):
        if "section_revision_ids" in revision.metadata_json:
            rows = cls._mapped_sections(
                s, document, revision.artifact_id, revision.metadata_json["section_revision_ids"]
            )
            if revision.metadata_json.get("section_count") != len(rows):
                cls._section_gap("invalid_mapping")
            return rows
        # Frozen releases are authoritative for legacy revisions. All matching
        # releases must agree; neither newest nor earliest wins a conflict.
        frozen_sets = {}
        for release in s.scalars(select(Release).where(Release.project_id == document.project_id)):
            choices = release.manifest_json["selections"]
            if any(choice["entity_revision_id"] == revision.id for choice in choices):
                if digest(canonical(release.manifest_json)) != release.manifest_sha256:
                    cls._section_gap("invalid_release_mapping")
                rows = [s.get(Revision, choice["entity_revision_id"]) for choice in choices]
                if any(row is None for row in rows):
                    cls._section_gap("invalid_release_mapping")
                rows = cls._legacy_sections(s, document, revision, rows)
                frozen_sets[tuple(row.id for row in rows)] = rows
        if len(frozen_sets) > 1:
            cls._section_gap("conflicting_release_mappings")
        if frozen_sets:
            return next(iter(frozen_sets.values()))
        candidates = list(
            s.scalars(
                select(Revision).where(
                    Revision.project_id == document.project_id, Revision.artifact_id == revision.artifact_id
                )
            )
        )
        linked = [row for row in candidates if row.metadata_json.get("document_revision_id") == revision.id]
        if linked:
            return cls._legacy_sections(s, document, revision, linked)
        if aid:
            # Only a writer explicitly operating on this selected draft may
            # use its selections to construct a NEW mapped document revision.
            selected = s.get(Selection, (aid, document.id))
            if selected and selected.entity_revision_id == revision.id:
                rows = list(
                    s.scalars(
                        select(Revision)
                        .join(Selection, Selection.entity_revision_id == Revision.id)
                        .where(Selection.analysis_set_id == aid)
                    )
                )
                return cls._legacy_sections(s, document, revision, rows)
        return cls._legacy_sections(s, document, revision, candidates)

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
        known = s.info.get("vks_known_revisions", {}).get((entity.id, fingerprint))
        if known is not None:
            return known
        if s.info.get("vks_completion_batch") and s.bind.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            statement = (
                insert(Revision)
                .values(
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
                .on_conflict_do_nothing(index_elements=["entity_id", "content_sha256"])
                .returning(Revision)
            )
            created = s.scalar(statement)
            if created is not None:
                s.info.setdefault("vks_prefetched", {})[(Revision, created.id)] = created
                s.info.setdefault("vks_known_revisions", {})[(entity.id, fingerprint)] = created
                return created
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

    def _artifact(self, s, analysis, document, old, markdown, generation=None):
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
        provenance = list(source.metadata_json.get("input_revision_provenance", [])) if source else []
        if generation:
            from .reading import revision_policy

            provenance = [
                *provenance,
                {
                    "revision_id": old.id,
                    "entity_id": old.entity_id,
                    "sha256": old.content_sha256,
                    "artifact_id": old.artifact_id,
                    **revision_policy(s, old),
                },
            ]
        if provenance:
            inherited_policy["input_revision_provenance"] = provenance
        if generation:
            inherited_policy["interactive_generation"] = generation
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
                    **({"input_revision_provenance": provenance} if provenance else {}),
                    **({"interactive_generation": generation} if generation else {}),
                },
            )
            s.add(artifact)
            s.flush()
        return artifact

    def save_draft(self, actor, eid, payload, version, *, session=None, validate=None, generation=None):
        markdown = payload["markdown"]
        if len(markdown.encode()) > 1_000_000:
            fail("VKS_LIMIT_EXCEEDED", "文档草稿超过大小限制", 413)
        with nullcontext(session) if session is not None else self.service.db() as s:
            document, analysis, selection, old = self._selected_document(s, actor, eid, payload["analysis_set_id"])
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布文档不可修改，请先创建草稿", 409)
            if old.id != payload["base_revision_id"]:
                fail("VKS_VERSION_CONFLICT", "文档修订已更新，请比较后重新保存", 412)
            self.service._version(old.metadata_json.get("row_version", 1), version)
            if validate:
                validate(s)
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
            artifact = self._artifact(s, analysis, document, old, markdown, generation=generation)
            metadata = dict(
                old.metadata_json,
                row_version=old.metadata_json.get("row_version", 1) + 1,
                source_kind="platform_draft",
                original_artifact_id=old.metadata_json.get("original_artifact_id", old.artifact_id),
                locked_section_ids=sorted(requested),
                section_count=len(sections),
                identity_ambiguities=ambiguities,
            )
            old_to_new, evidence_for_revision, chapters = {}, {}, []
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
                chapters.append(chapter)
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
            metadata = self.with_sections(s, document, artifact, chapters, metadata)
            new = self._revision(
                s,
                document,
                artifact,
                next((section["title"] for section in sections if section["headingEndLine"] > 0), sections[0]["title"]),
                markdown,
                {"kind": "file", "path": artifact.relative_path},
                metadata,
            )
            selection.entity_revision_id = new.id
            old_to_new[old.id] = new.id
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
            self.service.audit(s, actor, document.project_id, "workspace.document.save", new.id)
            return self._dto(s, document, new, analysis.id)

    def _dto(self, s, document, revision, aid=None, *, sections=None):
        if sections is None:
            sections = self._sections(s, document, revision, aid)
        return {
            "document_id": document.id,
            "entity_revision_id": revision.id,
            "content_sha256": revision.content_sha256,
            "markdown": revision.content,
            "sections": [self.service.entity_dto(s, r, True) for r in sections],
            "row_version": revision.metadata_json.get("row_version", 1),
            "original_artifact_id": revision.metadata_json.get("original_artifact_id", revision.artifact_id),
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
    def assert_publishable(s, analysis):
        from .service import DomainError

        blockers = []
        selected_sections = {}
        for row in s.scalars(
            select(Revision)
            .join(Selection, Selection.entity_revision_id == Revision.id)
            .where(Selection.analysis_set_id == analysis.id)
        ):
            key = row.metadata_json.get("document_key")
            if key and s.get(Entity, row.entity_id).type == "section":
                selected_sections.setdefault(key, set()).add(row.id)
        for selection in s.scalars(select(Selection).where(Selection.analysis_set_id == analysis.id)):
            revision = s.get(Revision, selection.entity_revision_id)
            entity = s.get(Entity, selection.entity_id)
            if entity.type != "document":
                continue
            try:
                expected = {row.id for row in DocumentService._sections(s, entity, revision)}
                current = selected_sections.get(entity.stable_key, set())
                if expected != current:
                    blockers.append("文档修订与当前章节选择不一致：" + entity.id)
            except DomainError:
                blockers.append("文档历史章节映射存在缺口：" + entity.id)
        return blockers


def install_documents(app, service, actor, envelope, API="/api/v1"):
    """Document reads and saves are installed by the shared API."""
