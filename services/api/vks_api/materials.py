"""Project-owned installable materials using the existing immutable revision graph.

A package is a stable page entity; its immutable revision declares exact member
revisions. Selecting that root and its members mounts a version in a draft or
snapshot. No package can supply server-side executable adapters.
"""

from copy import deepcopy
from pathlib import PurePosixPath
from typing import Annotated, ClassVar, Literal

from fastapi import Depends, Header, Request
from pydantic import Field
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import Mapped, mapped_column

from .documents import DocumentService, assert_section_locks, match_sections, parse_sections
from .knowledge import AnalysisSource, KnowledgeService, SourceSnapshot
from .models import (
    Analysis,
    Artifact,
    Base,
    Entity,
    Idempotency,
    Job,
    Relation,
    Release,
    Revision,
    Selection,
    now,
    uid,
)
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref, _identity, _new_entity, _repoint, _select, version
from .prototype_read import missing
from .service import Actor, DomainError, canonical, sha

ROOT = "material_package"
COMPOSITION = "studio_composition"


class StudioWorkspace(Base):
    __tablename__ = "studio_workspace"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    row_version: Mapped[int] = mapped_column(default=1)
    __mapper_args__: ClassVar[dict] = {"version_id_col": row_version}


class OpenWorkspace(Input):
    analysis_set_id: Ref | None = None


class PrepareMaterial(Input):
    analysis_set_id: Ref
    kind: Literal["prototype", "documents"]
    name: str = Field(min_length=1, max_length=160)
    artifact_ids: list[Ref] = Field(min_length=1, max_length=100)
    package_id: Ref | None = None
    entry: str = Field(default="", max_length=1000)


class ActivateMaterials(Input):
    analysis_set_id: Ref
    revision_ids: list[Ref] = Field(default_factory=list, max_length=100)
    remove_package_ids: list[Ref] = Field(default_factory=list, max_length=100)
    replace_all: bool = False
    primary_package_id: Ref | None = None
    auto_migrate: bool = True
    prepared: bool = False


class SaveMaterialVersion(Input):
    analysis_set_id: Ref
    name: str = Field(min_length=1, max_length=160)


class RestoreMaterialVersion(Input):
    release_id: Ref
    analysis_set_id: Ref


def roots(s, pid, choices=None):
    key = (pid, frozenset(choices.items())) if s.info.get("vks_read_only") and isinstance(choices, dict) else None
    cache = s.info.setdefault("vks_material_roots", {})
    if key is not None and key in cache:
        return list(cache[key])
    query = (
        select(Revision)
        .join(Entity, Revision.entity_id == Entity.id)
        .where(Entity.project_id == pid, Entity.type == "page", Entity.stable_key.startswith("material."))
    )
    if choices is not None:
        # Do not scan every historical package revision on normal reads.
        query = (
            query.join(Selection, Selection.entity_revision_id == Revision.id).where(
                Selection.analysis_set_id == choices
            )
            if isinstance(choices, str)
            else query.where(Revision.id.in_(list(choices.values())))
        )
    result = [r for r in s.scalars(query) if ROOT in r.metadata_json]
    if key is not None:
        cache[key] = tuple(result)
        # Keep the small package roots alive so later descriptor/navigation
        # reads do not decode their large target/member metadata again.
        from .reading import ReadingService

        ReadingService(None).preload(s, result)
        s.info.setdefault("vks_prefetched", {}).update({(Revision, row.id): row for row in result})
    return result


def root(s, pid, rid):
    row = s.get(Revision, rid)
    if not row or row.project_id != pid or ROOT not in row.metadata_json:
        raise missing()
    return row


def readable(service, s, actor, pid, row):
    from .reading import chunks

    members = row.metadata_json[ROOT].get("members", [])
    complete = service._can(s, actor, pid, "source:read") and service._can(s, actor, pid, "trace:read")
    if row.project_id != pid or (not complete and not service._visible_revision(s, actor, pid, row)):
        return False
    retained = s.info.setdefault("material_readable", {})
    cache_key = (actor.id, actor.scopes, pid, row.id)
    if cache_key in retained:
        return retained[cache_key]
    if complete:
        found = set()
        for batch in chunks(members):
            # Fetch by exact primary keys, then reject other-project members.
            # The project predicate otherwise selects a repeated project scan
            # on SQLite for every member batch.
            found.update(
                rid
                for rid, owner in s.execute(select(Revision.id, Revision.project_id).where(Revision.id.in_(batch)))
                if owner == pid
            )
        result = found == set(members)
    else:
        result = all(
            (r := s.get(Revision, rid)) and r.project_id == pid and service._visible_revision(s, actor, pid, r)
            for rid in members
        )
    retained[cache_key] = result
    return result


def primary(s, pid, choices):
    from .reading import chunks

    # Most choices are chapters. Inspect only the tiny composition JSON value,
    # preserving selection order without materializing every revision body.
    for batch in chunks(choices.values()):
        rows = {
            rid: value
            for rid, project, value in s.execute(
                select(Revision.id, Revision.project_id, Revision.metadata_json[COMPOSITION]).where(
                    Revision.id.in_(batch)
                )
            )
            if project == pid
        }
        for rid in batch:
            if isinstance(rows.get(rid), dict):
                return rows[rid].get("primary_package_id")
    return None


def descriptor(s, row):
    meta = row.metadata_json[ROOT]
    art = s.get(Artifact, row.artifact_id)
    return {
        "package_id": row.entity_id,
        "revision_id": row.id,
        "name": row.name,
        "kind": meta["kind"],
        "format": meta["format"],
        "created_at": row.created_at,
        "artifact_id": art.id,
        "sha256": art.sha256,
        "size_bytes": art.size_bytes,
        "documents": meta.get("document_count", 0),
        "sections": meta.get("section_count", 0),
        "target_count": len(meta.get("targets", [])),
        "warnings": meta.get("warnings", []),
    }


class MaterialService:
    def __init__(self, service):
        self.service = service
        self.docs = DocumentService(service)

    def _draft(self, s, actor, pid, aid):
        self.service.require(s, actor, pid, "knowledge:edit")
        analysis = s.scalar(select(Analysis).where(Analysis.id == aid, Analysis.project_id == pid).with_for_update())
        if not analysis:
            raise missing()
        if analysis.state == "frozen":
            raise DomainError("VKS_STATE_CONFLICT", "此版本已保存，请从版本记录恢复为可编辑工作区", 409)
        return analysis

    def _complete_access(self, s, actor, pid, analysis):
        if self.service._can(s, actor, pid, "source:read") and self.service._can(s, actor, pid, "trace:read"):
            return
        if any(not self.service._visible_revision(s, actor, pid, r) for r in self.service._revisions(s, analysis.id)):
            raise DomainError("VKS_FORBIDDEN", "管理完整资料组合需要读取其中全部资料的权限", 403)

    def _prepare_access(self, s, actor, pid, analysis, form):
        self.service.require(s, actor, pid, "source:import")
        self._complete_access(s, actor, pid, analysis)
        if form.package_id:
            candidates = [r for r in roots(s, pid) if r.entity_id == form.package_id]
            if not candidates or any(not readable(self.service, s, actor, pid, r) for r in candidates):
                raise missing()
            if any(r.metadata_json[ROOT]["kind"] != form.kind for r in candidates):
                raise DomainError("VKS_INVALID_INPUT", "更新资料包不能改变原型/文档类型", 422)

    def _revision(self, s, pid, kind, key, art, name, content, meta, locator=None):
        entity = _identity(s, pid, kind, key) or _new_entity(s, pid, kind, key)
        return self.docs._revision(
            s, entity, art, name, content, locator or {"kind": "file", "relative_path": art.relative_path}, meta
        )

    def _artifact(self, s, pid, snapshot, path, raw, kind="fixture", metadata=None, media_type=None):
        old = s.scalar(
            select(Artifact).where(
                Artifact.project_id == pid, Artifact.snapshot_id == snapshot, Artifact.relative_path == path
            )
        )
        if old:
            if old.sha256 != sha(raw):
                raise DomainError("VKS_INTEGRITY_ERROR", "不可变制品路径发生冲突", 409)
            return old
        art = Artifact(
            id=uid(),
            project_id=pid,
            snapshot_id=snapshot,
            relative_path=path,
            sha256=sha(raw),
            size_bytes=len(raw),
            kind=kind,
            media_type=media_type or ("text/markdown" if kind == "markdown" else "text/html"),
            sensitivity="internal",
            object_key=self.service._put_object(raw),
            metadata_json=metadata or {},
        )
        s.add(art)
        s.flush()
        return art

    def _composition(self, s, analysis, primary_id):
        rows = roots(s, analysis.project_id, analysis.id)
        if rows:
            art = s.get(Artifact, rows[0].artifact_id)
        else:
            current = next((r for r in self.service._revisions(s, analysis.id) if COMPOSITION in r.metadata_json), None)
            if not current:
                return
            art = s.get(Artifact, current.artifact_id)
        rev = self._revision(
            s,
            analysis.project_id,
            "page",
            "studio.composition",
            art,
            "资料启用组合",
            "",
            {COMPOSITION: {"primary_package_id": primary_id}},
        )
        _select(s, analysis, rev)

    def _adopt(self, s, actor, pid, analysis):
        """Adopt a legacy workspace without changing its knowledge identities or bytes."""
        if roots(s, pid, analysis.id) or any(
            COMPOSITION in r.metadata_json for r in self.service._revisions(s, analysis.id)
        ):
            return
        from .interactive import InteractiveKnowledgeService

        shared = InteractiveKnowledgeService(self.service)
        choices = {r.entity_id: r.id for r in self.service._revisions(s, analysis.id)}
        legacy = shared._packages(s, actor, pid, choices)
        rows = self.service._revisions(s, analysis.id)
        adopted_documents = set()
        first = None
        for package in legacy:
            art = s.get(Artifact, package["id"])
            kind = "prototype" if package["kind"] == "prototype" else "documents"
            members = []
            for row in rows:
                origin = s.get(Artifact, row.artifact_id) if row.artifact_id else None
                if origin and origin.snapshot_id == art.snapshot_id:
                    members.append(row.id)
                    if s.get(Entity, row.entity_id).type == "document":
                        adopted_documents.add(row.id)
            rev = self._revision(
                s,
                pid,
                "page",
                "material." + art.id,
                art,
                package["name"],
                "",
                {
                    ROOT: {
                        "kind": kind,
                        "format": "charge-prototype" if kind == "prototype" else "charge-handbook",
                        "members": members,
                        "targets": [],
                        "legacy": True,
                        "document_count": sum(
                            s.get(Entity, s.get(Revision, r).entity_id).type == "document" for r in members
                        ),
                        "section_count": sum(
                            s.get(Entity, s.get(Revision, r).entity_id).type == "section" for r in members
                        ),
                    }
                },
            )
            _select(s, analysis, rev)
            if kind == "prototype":
                first = first or rev.entity_id
        remaining = [r for r in rows if s.get(Entity, r.entity_id).type == "document" and r.id not in adopted_documents]
        if remaining:
            art = s.get(Artifact, remaining[0].artifact_id)
            members = [r.id for r in remaining]
            for doc in remaining:
                members.extend(c.id for c in self.docs._sections(s, s.get(Entity, doc.entity_id), doc))
            rev = self._revision(
                s,
                pid,
                "page",
                "material.legacy-docs." + analysis.id,
                art,
                "既有项目文档",
                "",
                {
                    ROOT: {
                        "kind": "documents",
                        "format": "markdown",
                        "members": members,
                        "targets": [],
                        "document_count": len(remaining),
                        "section_count": len(members) - len(remaining),
                    }
                },
            )
            _select(s, analysis, rev)
        if legacy or remaining:
            self._composition(s, analysis, first)
            analysis.selection_version += 1

    def open(self, actor, pid, aid=None):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            state = s.get(StudioWorkspace, pid)
            analysis = s.get(Analysis, aid or (state.analysis_set_id if state else ""))
            if aid and (not analysis or analysis.project_id != pid):
                raise missing()
            if analysis and analysis.project_id != pid:
                raise missing()
            if not analysis:
                analysis = s.scalar(
                    select(Analysis)
                    .where(Analysis.project_id == pid, Analysis.state != "frozen")
                    .order_by(Analysis.created_at.desc())
                )
            if not analysis:
                self.service.require(s, actor, pid, "knowledge:edit")
                analysis = Analysis(
                    id=uid(), project_id=pid, key="studio-" + uid(), name="项目工作区", created_by=actor.id
                )
                s.add(analysis)
                s.flush()
            if analysis.state != "frozen" and self.service._can(s, actor, pid, "knowledge:edit"):
                self._complete_access(s, actor, pid, analysis)
                self._adopt(s, actor, pid, analysis)
                if not state:
                    state = StudioWorkspace(project_id=pid, analysis_set_id=analysis.id)
                    s.add(state)
                elif not aid:
                    state.analysis_set_id = analysis.id
            return {
                "analysis_set_id": analysis.id,
                "selection_version": analysis.selection_version,
                "editable": analysis.state != "frozen",
            }

    def library(self, actor, pid, aid):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            analysis = s.get(Analysis, aid)
            if not analysis or analysis.project_id != pid:
                raise missing()
            choices = {r.entity_id: r.id for r in self.service._revisions(s, aid)}
            active = set(choices.values())
            library = {}
            for rev in roots(s, pid):
                if not readable(self.service, s, actor, pid, rev):
                    continue
                row = descriptor(s, rev)
                group = library.setdefault(
                    rev.entity_id,
                    {
                        "id": rev.entity_id,
                        "name": rev.name,
                        "kind": row["kind"],
                        "active_revision_id": None,
                        "versions": [],
                    },
                )
                group["versions"].append(row)
                if rev.id in active:
                    group["active_revision_id"] = rev.id
                    group["name"] = rev.name
            for group in library.values():
                group["versions"].sort(key=lambda r: (r["created_at"], r["revision_id"]), reverse=True)
            versions = []
            for release in s.scalars(
                select(Release).where(Release.project_id == pid).order_by(Release.created_at.desc())
            ):
                try:
                    self.service._saved_snapshot(s, actor, release)
                except DomainError:
                    continue
                versions.append(
                    {
                        "id": release.id,
                        "name": release.change_note,
                        "version": release.release_no,
                        "created_at": release.created_at,
                        "analysis_set_id": release.analysis_set_id,
                    }
                )
            visible_artifacts = {r["artifact_id"] for p in library.values() for r in p["versions"]}
            from .reading import ReadingService

            jobs = [
                ReadingService(self.service).job_dto(s, actor, j)
                for j in s.scalars(
                    select(Job)
                    .where(Job.project_id == pid, Job.kind == "material_migrate")
                    .order_by(Job.created_at.desc())
                    .limit(8)
                )
                if j.created_by == actor.id
                and all(t.get("package_id") in visible_artifacts for t in j.input_json.get("targets", []))
            ]
            return {
                "analysis_set_id": aid,
                "selection_version": analysis.selection_version,
                "editable": analysis.state != "frozen" and self.service._can(s, actor, pid, "knowledge:edit"),
                "packages": list(library.values()),
                "versions": versions,
                "primary_package_id": primary(s, pid, choices),
                "jobs": jobs,
            }

    def prepare(self, actor, pid, form, expected, key):
        from .material_parser import parse

        with self.service.db() as s:
            self.service.require(s, actor, pid, "source:import")
            analysis = self._draft(s, actor, pid, form.analysis_set_id)
            version(analysis.selection_version, expected)
            self._prepare_access(s, actor, pid, analysis, form)
            files = []
            for artifact_id in form.artifact_ids:
                raw, upload = self.service._upload_data(s, actor, pid, artifact_id)
                files.append((upload.filename, raw))
        parsed = parse(form.kind, files, form.entry)
        with self.service.db() as s:
            analysis = self._draft(s, actor, pid, form.analysis_set_id)
            version(analysis.selection_version, expected)
            # Parsing is intentionally outside the transaction. Recheck every
            # grant and source closure before committing derived revisions.
            self._prepare_access(s, actor, pid, analysis, form)
            return self.service.idem(
                s,
                actor,
                pid,
                "material.prepare",
                key,
                form.model_dump(),
                lambda: self._prepare(s, actor, pid, analysis, form, parsed, files),
            )

    def _prepare(self, s, actor, pid, analysis, form, parsed, files):
        package = (
            s.get(Entity, form.package_id) if form.package_id else _new_entity(s, pid, "page", "material." + uid())
        )
        selected = s.get(Selection, (analysis.id, package.id))
        previous = s.get(Revision, selected.entity_revision_id) if selected else None
        active_base = previous.id if previous else None
        if not previous:
            previous = s.scalar(
                select(Revision).where(Revision.entity_id == package.id).order_by(Revision.created_at.desc()).limit(1)
            )
        previous_meta = previous.metadata_json[ROOT] if previous else {}
        fingerprint = sha(
            canonical({"files": [(n, sha(b)) for n, b in files], "entry": parsed["entry"], "format": parsed["format"]})
        )
        snapshot = s.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.project_id == pid,
                SourceSnapshot.source_key == "material." + package.id,
                SourceSnapshot.provider_revision == fingerprint,
            )
        )
        if not snapshot:
            snapshot = SourceSnapshot(
                id=uid(),
                project_id=pid,
                source_key="material." + package.id,
                provider="local",
                provider_revision=fingerprint,
                content_sha256=fingerprint,
                role="prototype" if form.kind == "prototype" else "documentation",
            )
            s.add(snapshot)
            s.flush()
        inputs = [
            self._artifact(s, pid, snapshot.id, "inputs/" + str(i) + "/" + PurePosixPath(n).name, b)
            for i, (n, b) in enumerate(files)
        ]
        runtime_meta = {"prototype_input_artifact_ids": [a.id for a in inputs]}
        attachments = []
        if form.kind == "documents":
            for path, raw in parsed.get("files", []):
                if PurePosixPath(path).suffix.lower() in {".md", ".markdown", ".txt", ".html", ".htm"}:
                    continue
                attachments.append(
                    self._artifact(
                        s,
                        pid,
                        snapshot.id,
                        "documents/" + path,
                        raw,
                        metadata=dict(runtime_meta),
                        media_type="application/octet-stream",
                    )
                )
            # A whole-package reader (including warning/file names) inherits
            # current restrictions on every retained attachment. The individual
            # attachment still carries the original input provenance above.
            runtime_meta = {"prototype_input_artifact_ids": [a.id for a in [*inputs, *attachments]]}
        original = parsed["original"]
        if parsed["format"] == "static-site" or parsed["format"] == "html-document" and len(parsed["files"]) > 1:
            from .material_site import site_html

            original = site_html(parsed["entry"], parsed["files"])
        runtime = self._artifact(
            s, pid, snapshot.id, "runtime/" + sha(original) + ".html", original, metadata=runtime_meta
        )
        members, counts = [], {"added": 0, "updated": 0, "reused": 0, "removed": 0}
        old_docs = {
            r.metadata_json.get("material_path"): r
            for rid in previous_meta.get("members", [])
            if (r := s.get(Revision, rid)) and s.get(Entity, r.entity_id).type == "document"
        }
        consumed = set()
        for path, markdown in parsed["documents"]:
            source_body_sha256 = sha(markdown)
            preserved_manual = False
            prior = old_docs.get(path)
            if not prior:
                matches = [r for r in old_docs.values() if r.content == markdown and r.entity_id not in consumed]
                prior = matches[0] if len(matches) == 1 else None
            if prior:
                consumed.add(prior.entity_id)
                current_choice = s.get(Selection, (analysis.id, prior.entity_id))
                current = s.get(Revision, current_choice.entity_revision_id) if current_choice else prior
                if current and current.id != prior.id and prior.content == markdown:
                    if current.metadata_json.get("material_path") == path:
                        members.extend(
                            [
                                current.id,
                                *[c.id for c in self.docs._sections(s, s.get(Entity, current.entity_id), current)],
                            ]
                        )
                        counts["reused"] += 1
                        continue
                    markdown = current.content
                    preserved_manual = True
                prior = current or prior
            if prior and prior.content == markdown and prior.metadata_json.get("material_path") == path:
                members.extend(
                    [prior.id, *[c.id for c in self.docs._sections(s, s.get(Entity, prior.entity_id), prior)]]
                )
                counts["reused"] += 1
                continue
            art = self._artifact(
                s,
                pid,
                snapshot.id,
                "documents/" + path,
                markdown.encode(),
                "markdown",
                metadata={
                    "prototype_input_artifact_ids": [a.id for a in inputs]
                    + ([prior.artifact_id] if preserved_manual else [])
                },
            )
            doc_entity = (
                s.get(Entity, prior.entity_id)
                if prior
                else _new_entity(s, pid, "document", "material." + package.id + ".document." + uid())
            )
            sections = parse_sections(markdown, PurePosixPath(path).stem)
            previous_sections = self.docs._sections(s, s.get(Entity, prior.entity_id), prior) if prior else []
            matched, _, _ = match_sections(previous_sections, sections)
            if prior:
                assert_section_locks(
                    previous_sections, sections, matched, set(prior.metadata_json.get("locked_section_ids", []))
                )
            chapters = []
            for i, section in enumerate(sections):
                old = matched.get(i)
                entity = (
                    s.get(Entity, old.entity_id)
                    if old
                    else _new_entity(s, pid, "section", "material." + package.id + ".section." + uid())
                )
                metadata = {
                    "document_key": doc_entity.stable_key,
                    "heading_path": section["heading_path"],
                    "body_fingerprint": section["body_fingerprint"],
                    "material_package_id": package.id,
                    "manual_lock": bool(prior and entity.id in prior.metadata_json.get("locked_section_ids", [])),
                }
                chapter = self.docs._revision(
                    s,
                    entity,
                    art,
                    section["title"],
                    section["content"],
                    {
                        "kind": "line",
                        "relative_path": art.relative_path,
                        "startLine": section["startLine"],
                        "endLine": section["endLine"],
                    },
                    metadata,
                )
                chapters.append(chapter)
            title = next(
                (section["title"] for section in sections if section["headingEndLine"] > 0),
                sections[0]["title"] if sections else PurePosixPath(path).stem,
            )
            document = self.docs._revision(
                s,
                doc_entity,
                art,
                title,
                markdown,
                {"kind": "file", "relative_path": art.relative_path},
                {
                    "section_revision_ids": [c.id for c in chapters],
                    "section_count": len(chapters),
                    "material_package_id": package.id,
                    "material_path": path,
                    "source_body_sha256": source_body_sha256,
                    "logical_source_key": "material." + package.id,
                    "locked_section_ids": list(prior.metadata_json.get("locked_section_ids", [])) if prior else [],
                },
            )
            members.extend([document.id, *[c.id for c in chapters]])
            counts["updated" if prior else "added"] += 1
        counts["removed"] = len([r for r in old_docs.values() if r.entity_id not in consumed])
        meta = {
            "kind": form.kind,
            "format": parsed["format"],
            "entry": parsed["entry"],
            "members": members,
            "targets": parsed["targets"],
            "warnings": parsed["warnings"],
            "input_fingerprint": fingerprint,
            "document_count": len(parsed["documents"]),
            "section_count": len(members) - len(parsed["documents"]),
            "attachment_artifact_ids": [a.id for a in attachments],
            "document_navigation": parsed.get("navigation", {"order": [], "categories": []}),
            "base_revision_id": active_base,
            "changes": counts,
            "prepared_for": analysis.id,
        }
        rev = self.docs._revision(
            s, package, runtime, form.name, "", {"kind": "file", "relative_path": runtime.relative_path}, {ROOT: meta}
        )
        self.service.audit(s, actor, pid, "material.prepare", rev.id)
        return {**descriptor(s, rev), "changes": counts, "selection_version": analysis.selection_version}

    def checkpoint(self, actor, pid, aid, name, key):
        # Copy first, so saving a version never freezes the live editable workspace.
        cloned = KnowledgeService(self.service).clone(actor, pid, aid, name, "material-copy." + key)
        return self.service.save_snapshot(
            actor, pid, {"analysis_set_id": cloned["id"], "change_note": name}, "material-snapshot." + key
        )

    def activate(self, actor, pid, form, expected, key):
        # The checkpoint is independently idempotent; a stale apply leaves the old workspace intact.
        with self.service.db() as s:
            self.service.require(s, actor, pid, "source:import")
            analysis = self._draft(s, actor, pid, form.analysis_set_id)
            self._complete_access(s, actor, pid, analysis)
            existing_result = s.scalar(
                select(Idempotency).where(
                    Idempotency.project_id == pid,
                    Idempotency.actor_id == actor.id,
                    Idempotency.operation == "material.activate",
                    Idempotency.key == key,
                )
            )
            if not existing_result:
                version(analysis.selection_version, expected)
            has_content = bool(self.service._revisions(s, analysis.id))
        checkpoint = None
        if has_content and not existing_result:
            checkpoint = self.checkpoint(actor, pid, form.analysis_set_id, "替换前自动保存", "before." + key)["id"]
        with self.service.db() as s:
            analysis = self._draft(s, actor, pid, form.analysis_set_id)
            return self.service.idem(
                s,
                actor,
                pid,
                "material.activate",
                key,
                {**form.model_dump(), "expected": expected},
                lambda: self._activate(s, actor, pid, analysis, form, expected, checkpoint),
            )

    def _activate(self, s, actor, pid, analysis, form, expected, checkpoint):
        version(analysis.selection_version, expected)
        old = {r.entity_id: r for r in roots(s, pid, analysis.id)}
        incoming = [root(s, pid, rid) for rid in form.revision_ids]
        if len({r.entity_id for r in incoming}) != len(incoming):
            raise DomainError("VKS_INVALID_INPUT", "同一资料包不能同时启用两个版本", 422)
        if form.prepared:
            for rev in incoming:
                meta = rev.metadata_json[ROOT]
                current = old.get(rev.entity_id)
                if meta.get("base_revision_id") != (current.id if current else None):
                    raise DomainError("VKS_VERSION_CONFLICT", "预览之后资料包已变化，请重新解析更新资料", 409)
        for rev in [*incoming, *[old[k] for k in form.remove_package_ids if k in old]]:
            if not readable(self.service, s, actor, pid, rev):
                raise missing()
        if any(k not in old for k in form.remove_package_ids):
            raise missing()
        desired = {} if form.replace_all else {k: r for k, r in old.items() if k not in form.remove_package_ids}
        desired.update({r.entity_id: r for r in incoming})
        selected_before = {r.entity_id: r for r in self.service._revisions(s, analysis.id)}
        old_members = {eid: self._members(s, r, selected_before) for eid, r in old.items()}
        desired_members = {
            eid: self._members(s, r, selected_before if eid in old and old[eid].id == r.id else {})
            for eid, r in desired.items()
        }
        owned = {eid for rows in old_members.values() for eid in rows}
        wanted = {eid for rows in desired_members.values() for eid in rows}
        for eid, previous in old.items():
            if eid not in desired:
                actual = [r.id for r in old_members[eid].values()]
                if set(actual) != set(previous.metadata_json[ROOT]["members"]):
                    meta = deepcopy(previous.metadata_json)
                    meta[ROOT].update(members=actual, captured_edits=True, base_revision_id=previous.id)
                    self.docs._revision(
                        s,
                        s.get(Entity, eid),
                        s.get(Artifact, previous.artifact_id),
                        previous.name,
                        "",
                        previous.locator,
                        meta,
                    )
        for choice in list(s.scalars(select(Selection).where(Selection.analysis_set_id == analysis.id))):
            if choice.entity_id in old or choice.entity_id in owned - wanted:
                s.delete(choice)
        s.flush()
        for eid, rev in desired.items():
            _select(s, analysis, rev)
            for member in desired_members[eid].values():
                _select(s, analysis, member)
        s.flush()
        active_artifacts = {r.artifact_id for r in desired.values() if r.metadata_json[ROOT]["kind"] == "prototype"}
        for edge in s.scalars(
            select(Relation).where(
                Relation.analysis_set_id == analysis.id, Relation.relation_type == "has_business_point"
            )
        ):
            action = s.get(Revision, edge.from_revision_id)
            if action.metadata_json.get("interactive_anchor", {}).get("package_id") in active_artifacts and not s.get(
                Selection, (analysis.id, action.entity_id)
            ):
                _select(s, analysis, action)
        chosen = form.primary_package_id or primary(s, pid, {e: r.id for e, r in selected_before.items()})
        prototypes = [r.entity_id for r in desired.values() if r.metadata_json[ROOT]["kind"] == "prototype"]
        if form.primary_package_id and chosen not in prototypes:
            raise DomainError("VKS_INVALID_INPUT", "当前原型必须属于已启用的原型包", 422)
        chosen = chosen if chosen in prototypes else next(iter(prototypes), None)
        self._composition(s, analysis, chosen)
        report, targets, issues = self._migrate_graph(s, actor, pid, analysis, old, desired, selected_before)
        sources = list(analysis.sources)
        recorded_sources = {row.get("source_snapshot_id") for row in sources}
        for package in desired.values():
            origin = s.get(Artifact, package.artifact_id)
            snapshot = s.get(SourceSnapshot, origin.snapshot_id)
            if snapshot and snapshot.id not in recorded_sources:
                sources.append(
                    {
                        "source_snapshot_id": snapshot.id,
                        "source_id": snapshot.source_key,
                        "provider": snapshot.provider,
                        "provider_revision": snapshot.provider_revision,
                        "content_sha256": snapshot.content_sha256,
                        "role": snapshot.role,
                        "service_key": snapshot.service_key,
                    }
                )
                recorded_sources.add(snapshot.id)
            if snapshot and not s.get(AnalysisSource, (analysis.id, snapshot.id)):
                s.add(AnalysisSource(analysis_set_id=analysis.id, source_snapshot_id=snapshot.id, role=snapshot.role))
        analysis.sources = sources
        analysis.selection_version += 1
        state = s.get(StudioWorkspace, pid)
        if state:
            state.analysis_set_id = analysis.id
            state.row_version += 1
        else:
            s.add(StudioWorkspace(project_id=pid, analysis_set_id=analysis.id))
        job = None
        document_change = any(
            (k not in old or old[k].id != r.id) and r.metadata_json[ROOT]["kind"] == "documents"
            for k, r in desired.items()
        )
        document_change = document_change or (
            any(r.metadata_json[ROOT]["kind"] == "documents" for r in desired.values())
            and any(
                r.metadata_json[ROOT]["kind"] == "prototype" and (eid not in old or old[eid].id != r.id)
                for eid, r in desired.items()
            )
        )
        primary_artifact = desired[chosen].artifact_id if chosen else None
        if form.auto_migrate and (targets or issues or document_change):
            job = self.service._new_job(
                s,
                actor,
                pid,
                "material_migrate",
                {
                    "analysis_set_id": analysis.id,
                    "targets": targets,
                    "issues": issues,
                    "composition": self.composition_digest(s, pid, analysis.id),
                    "index_package_id": primary_artifact if document_change else None,
                    "index_documents": document_change,
                    "index_selection_version": analysis.selection_version,
                },
            )
        s.flush()
        self.service.audit(
            s, actor, pid, "material.activate", analysis.id, metadata={"checkpoint": checkpoint, **report}
        )
        return {
            "analysis_set_id": analysis.id,
            "selection_version": analysis.selection_version,
            "checkpoint_id": checkpoint,
            "migration": report,
            "job_id": job.id if job else None,
        }

    @staticmethod
    def composition_digest(s, pid, aid):
        return sha(canonical(sorted(r.id for r in roots(s, pid, aid))))

    def _members(self, s, package, selected):
        result = {}
        for rid in package.metadata_json[ROOT]["members"]:
            base = s.get(Revision, rid)
            if not base or base.project_id != package.project_id:
                raise DomainError("VKS_INTEGRITY_ERROR", "资料包成员不完整", 409)
            current = selected.get(base.entity_id, base)
            result[current.entity_id] = current
            if s.get(Entity, current.entity_id).type == "document":
                for chapter in self.docs._sections(s, s.get(Entity, current.entity_id), current):
                    result[chapter.entity_id] = chapter
        # A manual edit can remove or add sections beyond the imported member list.
        documents = {r.entity_id: r for r in result.values() if s.get(Entity, r.entity_id).type == "document"}
        doc_keys = {
            s.get(Entity, eid).stable_key: {c.id for c in self.docs._sections(s, s.get(Entity, eid), r)}
            for eid, r in documents.items()
        }
        return {
            eid: r
            for eid, r in result.items()
            if not (
                s.get(Entity, eid).type == "section"
                and r.metadata_json.get("document_key") in doc_keys
                and r.id not in doc_keys[r.metadata_json["document_key"]]
            )
        }

    def _migrate_graph(self, s, actor, pid, analysis, before, after, selected_before):
        selected = {r.entity_id: r for r in self.service._revisions(s, analysis.id)}
        edges = list(s.scalars(select(Relation).where(Relation.analysis_set_id == analysis.id)))
        report = {"relocated": 0, "reused": 0, "reassociate": 0, "unresolved": 0, "removed": 0}
        targets, issues = [], []
        old_by_art = {
            r.artifact_id: (eid, r) for eid, r in before.items() if r.metadata_json[ROOT]["kind"] == "prototype"
        }
        for action in selected_before.values():
            anchor = action.metadata_json.get("interactive_anchor")
            if not anchor:
                continue
            pair = old_by_art.get(anchor["package_id"])
            if not pair:
                continue
            package_id, old_package = pair
            updated = after.get(package_id)
            bindings = [e for e in edges if e.from_revision_id == action.id and e.relation_type == "has_business_point"]
            if not updated:
                choice = s.get(Selection, (analysis.id, action.entity_id))
                if choice:
                    s.delete(choice)
                report["removed"] += len(bindings)
                continue
            target = deepcopy(anchor["target"])
            if updated.id != old_package.id:
                candidates = updated.metadata_json[ROOT].get("targets", [])
                matching = match_target(target, candidates)
                if not matching:
                    # Keep the historical action but do not expose it under the new package.
                    for binding in bindings:
                        binding.validity_status = "broken"
                    report["unresolved"] += len([b for b in bindings if not b.excluded])
                    issues.extend(
                        {
                            "label": action.name,
                            "message": "新原型中目标缺失或存在多个候选；操作新控件时会重新自动关联",
                            "binding_id": b.id,
                        }
                        for b in bindings
                        if not b.excluded
                    )
                    continue
                target = matching
                art = s.get(Artifact, updated.artifact_id)
                replacement = self.docs._revision(
                    s,
                    s.get(Entity, action.entity_id),
                    art,
                    target["label"],
                    "",
                    action.locator,
                    {
                        **action.metadata_json,
                        "interactive_anchor": {**anchor, "package_id": art.id, "sha256": art.sha256, "target": target},
                    },
                )
                _select(s, analysis, replacement)
                _repoint(
                    [e for e in edges if e.from_revision_id == action.id or e.to_revision_id == action.id],
                    action,
                    replacement,
                    stale=False,
                )
                report["relocated"] += len(bindings)
            for binding in bindings:
                point = s.get(Revision, binding.to_revision_id)
                _select(s, analysis, point)
                if binding.excluded:
                    continue
                citations = [
                    e
                    for e in edges
                    if e.from_revision_id == point.id and e.relation_type == "supported_by_section" and not e.excluded
                ]
                affected = binding.validity_status != "valid" or updated.id != old_package.id
                for citation in citations:
                    prior = s.get(Revision, citation.to_revision_id)
                    current = selected.get(prior.entity_id)
                    if not current or current.id != prior.id:
                        citation.validity_status = "stale" if current else "broken"
                        affected = True
                if affected:
                    binding.validity_status = "stale"
                    report["reassociate"] += 1
                    targets.append(
                        {
                            "package_id": updated.artifact_id,
                            "target": target,
                            "context": {
                                "scope": "module" if target["selector"] == "body" else "control",
                                "page_title": target["label"],
                                "section_title": "",
                                "nearby_labels": [],
                            },
                        }
                    )
                else:
                    report["reused"] += 1
        return report, list({canonical(t): t for t in targets}.values()), issues

    def restore(self, actor, pid, form, expected, key):
        request = {**form.model_dump(), "expected": expected}
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            recorded = s.scalar(
                select(Idempotency).where(
                    Idempotency.project_id == pid,
                    Idempotency.actor_id == actor.id,
                    Idempotency.operation == "material.restore",
                    Idempotency.key == key,
                )
            )
            if recorded:
                return self.service.idem(s, actor, pid, "material.restore", key, request, lambda: None)
            analysis = self._draft(s, actor, pid, form.analysis_set_id)
            version(analysis.selection_version, expected)
            release = s.get(Release, form.release_id)
            if not release or release.project_id != pid:
                raise missing()
            self.service._saved_snapshot(s, actor, release)
            aid = release.analysis_set_id
        self.checkpoint(actor, pid, form.analysis_set_id, "回退前自动保存", "restore-before." + key)
        clone = KnowledgeService(self.service).clone(actor, pid, aid, "从版本恢复的工作区", "material-restore." + key)
        with self.service.db() as s:
            current = self._draft(s, actor, pid, form.analysis_set_id)
            version(current.selection_version, expected)

            def restore_pointer():
                state = s.get(StudioWorkspace, pid)
                if state and state.analysis_set_id != form.analysis_set_id:
                    raise DomainError("VKS_VERSION_CONFLICT", "项目工作区已切换，请刷新后再回退", 409)
                if state:
                    state.analysis_set_id = clone["id"]
                    state.row_version += 1
                else:
                    s.add(StudioWorkspace(project_id=pid, analysis_set_id=clone["id"]))
                self.service.audit(s, actor, pid, "material.restore", form.release_id)
                return {"analysis_set_id": clone["id"], "selection_version": clone["selection_version"]}

            return self.service.idem(s, actor, pid, "material.restore", key, request, restore_pointer)

    def retry_migration(self, actor, pid, aid, expected, key):
        with self.service.db() as s:
            analysis = self._draft(s, actor, pid, aid)
            version(analysis.selection_version, expected)
            self._complete_access(s, actor, pid, analysis)
            selected = {r.entity_id: r for r in self.service._revisions(s, aid)}
            targets = []
            for edge in s.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == aid,
                    Relation.relation_type == "has_business_point",
                    Relation.excluded == False,
                    Relation.validity_status != "valid",
                )
            ):
                action = s.get(Revision, edge.from_revision_id)
                anchor = action.metadata_json.get("interactive_anchor")
                if (
                    selected.get(action.entity_id) is action
                    and anchor
                    and any(r.artifact_id == anchor["package_id"] for r in roots(s, pid, aid))
                ):
                    targets.append(
                        {
                            "package_id": anchor["package_id"],
                            "target": anchor["target"],
                            "context": {
                                "scope": "module" if anchor["target"]["selector"] == "body" else "control",
                                "page_title": action.name,
                                "section_title": "",
                                "nearby_labels": [],
                            },
                        }
                    )
            return self.service.idem(
                s,
                actor,
                pid,
                "material.migrate",
                key,
                {"analysis_set_id": aid, "expected": expected},
                lambda: self.service.job_dto(
                    self.service._new_job(
                        s,
                        actor,
                        pid,
                        "material_migrate",
                        {
                            "analysis_set_id": aid,
                            "targets": targets,
                            "composition": self.composition_digest(s, pid, aid),
                        },
                    )
                ),
            )


def match_target(old, targets):
    """Conservative unique static match; runtime rechecks actual visibility on navigation."""
    import re

    def identity(selector):
        if selector.startswith("#"):
            return "id", selector[1:]
        found = re.fullmatch(r"\[(id|data-vks-id|data-testid|data-key)=[\"\'](.*)[\"\']\]", selector)
        return found.groups() if found else None

    same_page = [t for t in targets if t["route"] == old["route"] and t["frame_path"] == old["frame_path"]]
    if old["selector"] == "body":
        rows = [t for t in same_page if t["selector"] == "body"]
    elif identity(old["selector"]):
        rows = [t for t in same_page if identity(t["selector"]) == identity(old["selector"])]
    else:
        rows = [t for t in same_page if t["label"] == old["label"] and t["state"] == old["state"]]
    if len(rows) != 1:
        return None
    matched = deepcopy(rows[0])
    if identity(old["selector"]):
        matched["selector"] = old["selector"]
    return {**matched, "open_steps": old.get("open_steps", [])}


def migrate_job(service, jid):
    from packages.analyzers.cancellation import checkpoint

    from .interactive_ai import AssistanceInput, InteractiveAssistant
    from .jobs import current_actor

    assistant, manager = InteractiveAssistant(service), MaterialService(service)
    with service.db() as s:
        job = s.get(Job, jid)
        payload, pid = dict(job.input_json), job.project_id
        attempt = job.attempt
        completed = dict(job.result_json.get("completed", {}))
    targets = payload["targets"]

    def validate_index_scope():
        with service.db() as s:
            active = s.get(Job, jid)
            if (
                not active
                or active.attempt != attempt
                or active.state != "running"
                or active.cancel_requested
                or active.lease_until
                and active.lease_until <= now()
            ):
                raise DomainError("VKS_CANCELLED", "索引任务已取消或执行资格已失效", 409)
            current = current_actor(service, active.created_by, pid, s, job=active)
            service.require(s, current, pid, "knowledge:edit")
            workspace = s.get(StudioWorkspace, pid)
            analysis = s.get(Analysis, payload["analysis_set_id"])
            if (
                not analysis
                or analysis.state == "frozen"
                or payload.get("index_selection_version") is not None
                and analysis.selection_version != payload["index_selection_version"]
                or workspace
                and workspace.analysis_set_id != payload["analysis_set_id"]
                or manager.composition_digest(s, pid, payload["analysis_set_id"]) != payload["composition"]
            ):
                raise DomainError("VKS_VERSION_CONFLICT", "资料组合已变化，停止旧版本文档索引", 409)

    if (payload.get("index_documents") or payload.get("index_package_id")) and assistant.rag.store.settings(
        pid
    ).enabled:
        validate_index_scope()
        with service.db() as s:
            job = s.get(Job, jid)
            actor = current_actor(service, job.created_by, pid, s, job=job)
            job.stage = "DOCUMENT_INDEX"
        form = (
            AssistanceInput(
                analysis_set_id=payload["analysis_set_id"],
                package_id=payload["index_package_id"],
                target={
                    "route": "/",
                    "frame_path": [],
                    "selector": "body",
                    "label": "项目资料",
                    "state": "",
                    "open_steps": [],
                },
                context={"scope": "module"},
            )
            if payload.get("index_package_id")
            else None
        )
        assistant.rag.build_documents(
            actor,
            pid,
            analysis_set_id=payload["analysis_set_id"],
            form=form,
            validate_scope=validate_index_scope,
            job_id=jid,
            job_attempt=attempt,
        )
    for index, target in enumerate(targets):
        checkpoint()
        signature = sha(canonical(target))
        if signature in completed and completed[signature].get("saved"):
            continue
        with service.db() as s:
            job = s.get(Job, jid)
            actor = current_actor(service, job.created_by, pid, s, job=job)
            state = s.get(StudioWorkspace, pid)
            analysis = s.get(Analysis, payload["analysis_set_id"])
            if (
                not analysis
                or analysis.state == "frozen"
                or state
                and state.analysis_set_id != analysis.id
                or manager.composition_digest(s, pid, analysis.id) != payload["composition"]
            ):
                raise DomainError("VKS_VERSION_CONFLICT", "资料组合已变化，停止旧版本关联迁移", 409)
            job.progress = int(index * 100 / max(1, len(targets)))
            job.stage = "ASSOCIATE"
        try:

            def validate_scope(session):
                active_job = session.get(Job, jid)
                current_actor(service, active_job.created_by, pid, session, job=active_job)
                workspace = session.get(StudioWorkspace, pid)
                if (
                    workspace
                    and workspace.analysis_set_id != payload["analysis_set_id"]
                    or manager.composition_digest(session, pid, payload["analysis_set_id"]) != payload["composition"]
                ):
                    raise DomainError("VKS_VERSION_CONFLICT", "资料组合已变化，丢弃旧迁移结果", 409)

            result = assistant.run(
                actor,
                pid,
                AssistanceInput(analysis_set_id=payload["analysis_set_id"], refresh=True, **target),
                migrate_manual=True,
                validate_scope=validate_scope,
            )
            completed[signature] = {
                "label": target["target"]["label"],
                "saved": result["saved"],
                "confidence": result["confidence"],
                "message": "关联已迁移" if result["saved"] else "依据不足，保留为待关联",
            }
        except DomainError as e:
            if e.code in {"VKS_FORBIDDEN", "VKS_UNAUTHENTICATED", "VKS_VERSION_CONFLICT"}:
                raise
            completed[signature] = {
                "label": target["target"]["label"],
                "saved": False,
                "message": e.message,
                "code": e.code,
            }
        with service.db() as s:
            job = s.get(Job, jid)
            job.result_json = {
                "completed": completed,
                "total": len(targets),
                "saved": sum(bool(r.get("saved")) for r in completed.values()),
            }
    return {
        "completed": completed,
        "total": len(targets) + len(payload.get("issues", [])),
        "issues": payload.get("issues", []),
        "saved": sum(bool(r.get("saved")) for r in completed.values()),
        "pending": sum(not r.get("saved") for r in completed.values()) + len(payload.get("issues", [])),
        "result_refs": [{"kind": "analysis_set", "id": payload["analysis_set_id"]}],
    }


def install_materials(app, service, actor, envelope):
    shared = MaterialService(service)
    ActorDep = Annotated[Actor, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/materials"

    @app.post(prefix + "/open", response_model=PrototypeEnvelope[dict])
    def open_workspace(pid: str, payload: OpenWorkspace, a: ActorDep, request: Request):
        return envelope(request, shared.open(a, pid, payload.analysis_set_id))

    @app.get(prefix, response_model=PrototypeEnvelope[dict])
    def library(pid: str, analysis_set_id: str, a: ActorDep, request: Request):
        return envelope(request, shared.library(a, pid, analysis_set_id))

    @app.post(prefix + "/prepare", response_model=PrototypeEnvelope[dict])
    def prepare(
        pid: str,
        payload: PrepareMaterial,
        a: ActorDep,
        request: Request,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=100),
    ):
        return envelope(request, shared.prepare(a, pid, payload, if_match, idempotency_key))

    @app.post(prefix + "/activate", response_model=PrototypeEnvelope[dict])
    def activate(
        pid: str,
        payload: ActivateMaterials,
        a: ActorDep,
        request: Request,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=90),
    ):
        return envelope(request, shared.activate(a, pid, payload, if_match, idempotency_key))

    @app.post(prefix + "/versions", response_model=PrototypeEnvelope[dict])
    def save_version(
        pid: str,
        payload: SaveMaterialVersion,
        a: ActorDep,
        request: Request,
        idempotency_key: str = Header(..., max_length=80),
    ):
        return envelope(request, shared.checkpoint(a, pid, payload.analysis_set_id, payload.name, idempotency_key))

    @app.post(prefix + "/restore", response_model=PrototypeEnvelope[dict])
    def restore(
        pid: str,
        payload: RestoreMaterialVersion,
        a: ActorDep,
        request: Request,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=80),
    ):
        return envelope(request, shared.restore(a, pid, payload, if_match, idempotency_key))

    @app.post(prefix + "/migrate", response_model=PrototypeEnvelope[dict])
    def migrate(
        pid: str,
        payload: OpenWorkspace,
        a: ActorDep,
        request: Request,
        if_match: str = Header(...),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, shared.retry_migration(a, pid, payload.analysis_set_id, if_match, idempotency_key))
