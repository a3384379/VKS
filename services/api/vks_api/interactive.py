"""Original HTML runtime projections and transactional, versioned knowledge links.

Only bytes leave this service: execution belongs to the user-opened browser runtime.
P0 stores the immutable DOM locator in an Action revision, using the same knowledge
and citation records as the raster editor. No private material is served statically.
"""

from contextlib import nullcontext
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import load_only

from .document_presentation import PresentationSettings, presentation_for_analysis, presentation_for_release
from .documents import DocumentService
from .knowledge import SourceSnapshot
from .models import Artifact, Entity, Evidence, Relation, Revision
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import (
    Input,
    PointText,
    PrototypeEditor,
    Ref,
    _edges,
    _evidence,
    _identity,
    _new_entity,
    _repoint,
    _require_edges,
    _select,
    _write_relation,
    draft,
    version,
)
from .prototype_read import PrototypeReader, missing, selected_id
from .reading import artifact_policy
from .service import Actor, DomainError, canonical, sha


class DOMTarget(Input):
    route: str = Field(min_length=1, max_length=1000, pattern=r"^/")
    frame_path: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(max_length=8)
    selector: str = Field(min_length=1, max_length=2000)
    label: str = Field(min_length=1, max_length=160)
    state: str = Field(default="", max_length=500)
    open_steps: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=8)


class InteractiveBinding(Input):
    analysis_set_id: Ref
    package_id: Ref
    target: DOMTarget
    point: PointText
    section_revision_ids: list[Ref] = Field(min_length=1, max_length=20)
    binding_id: Ref | None = None
    binding_version: int | None = Field(default=None, ge=1)
    point_revision_id: Ref | None = None


class Exclusion(Input):
    analysis_set_id: Ref
    binding_version: int = Field(ge=1)
    excluded: bool


class RuntimePackage(Input):
    id: str
    kind: str
    sha256: str
    size_bytes: int
    name: str
    adapter_version: str
    package_key: str = ""
    revision_id: str = ""
    format: str = ""
    native_index_modified: bool = False


class InteractiveDocumentInfo(Input):
    id: str
    revision_id: str
    name: str
    path: str
    namespace: str = ""
    package_id: str | None = None
    native_package_id: str | None = None
    native_index_modified: bool = False


class InteractiveCatalog(Input):
    analysis_set_id: str
    release_id: str | None
    selection_version: int
    source_fingerprint: str = ""
    editable: bool
    packages: list[RuntimePackage]
    documents: list[InteractiveDocumentInfo]
    primary_package_id: str | None = None
    navigation: dict = Field(default_factory=lambda: {"packages": []})
    presentation: PresentationSettings = Field(default_factory=PresentationSettings)


class InteractiveChapter(Input):
    id: str
    revision_id: str
    name: str
    content: str
    start_line: int = 1
    end_line: int = 1
    heading_level: int = 0


class NativeDocumentSection(Input):
    revision_id: str
    anchor: str
    selector: str
    heading: str
    heading_level: int
    heading_ordinal: int


class NativeDocument(Input):
    package_id: str
    package_key: str = ""
    revision_id: str = ""
    sha256: str
    format: str
    path: str
    document_anchor: str = ""
    sections: list[NativeDocumentSection]
    matches_index: bool
    source_document_revision_id: str | None = None


class InteractiveDocument(Input):
    id: str
    revision_id: str
    name: str
    content: str = ""
    partial: bool = False
    sections: list[InteractiveChapter]
    native: NativeDocument | None = None


class InteractiveCitation(Input):
    revision_id: str
    name: str
    content: str
    document_revision_id: str
    document_name: str


class InteractiveBindingOutput(Input):
    id: str
    binding_version: int
    excluded: bool
    validity_status: str
    point_revision_id: str
    title: str
    summary: str
    package_id: str
    target: DOMTarget
    sections: list[InteractiveCitation]
    scope: str = "control"
    origin: str = "manual"
    model_id: str = ""


class InteractiveSaved(Input):
    binding_id: str
    selection_version: int


class InteractiveSelection(Input):
    selection_version: int


class InteractiveKnowledgeService:
    def __init__(self, service):
        self.service = service
        self.reader = PrototypeReader(service)
        self.documents = DocumentService(service)
        self.editor = PrototypeEditor(service)

    @staticmethod
    def _read_key(s, actor, pid, choices):
        # Only reuse within an immutable read snapshot. Write-side validation
        # must see changes made earlier in its own transaction.
        if not s.info.get("vks_read_only"):
            return None
        return (actor, pid, frozenset(choices.items()))

    def _rows(self, s, actor, pid, choices, *, types=None, revision_ids=None):
        wanted = frozenset(types) if types is not None else None
        requested = frozenset(revision_ids) if revision_ids is not None else None
        base = self._read_key(s, actor, pid, choices)
        key = (base, wanted, requested) if base is not None else None
        cache = s.info.setdefault("vks_interactive_rows", {})
        if key is not None and key in cache:
            return list(cache[key])
        candidates = choices.items()
        if requested is not None:
            candidates = [(eid, rid) for eid, rid in candidates if rid in requested]
        if wanted is not None:
            from .reading import chunks

            kinds = {}
            for batch in chunks([eid for eid, _ in candidates]):
                kinds.update(s.execute(select(Entity.id, Entity.type).where(Entity.id.in_(batch))).all())
            candidates = [(eid, rid) for eid, rid in candidates if kinds.get(eid) in wanted]
        candidates = list(candidates)
        # Batch and retain immutable identities for this transaction. A fully
        # authorized reader need not recompute the same source/trace policy for
        # every chapter; project and entity/artifact ownership still apply.
        retained = s.info.setdefault("vks_prefetched", {})
        missing = [rid for _, rid in candidates if (Revision, rid) not in retained]
        revisions = self.reader.reading.revision_rows(s, missing)
        retained.update({(Revision, r.id): r for r in revisions})
        complete = self.service._can(s, actor, pid, "source:read") and self.service._can(s, actor, pid, "trace:read")
        result = []
        for eid, rid in candidates:
            row = retained.get((Revision, rid))
            entity = s.get(Entity, eid)
            if (
                not row
                or not entity
                or entity.type not in {"page", "action", "document", "section", "business_point"}
                or row.entity_id != eid
                or row.project_id != pid
                or entity.project_id != pid
            ):
                continue
            art = s.get(Artifact, row.artifact_id) if row.artifact_id else None
            if row.artifact_id and (not art or art.project_id != pid):
                continue
            if complete or self.service._visible_revision(s, actor, pid, row):
                result.append((entity, row))
        if key is not None:
            cache[key] = tuple(result)
        return result

    def _packages(self, s, actor, pid, choices, *, kind=None, package_id=None):
        base = self._read_key(s, actor, pid, choices)
        key = (base, kind, package_id) if base is not None else None
        cache = s.info.setdefault("vks_interactive_packages", {})
        if key is None:
            return self._build_packages(s, actor, pid, choices, kind=kind, package_id=package_id)
        if key not in cache:
            cache[key] = tuple(self._build_packages(s, actor, pid, choices, kind=kind, package_id=package_id))
        return [dict(package) for package in cache[key]]

    def _build_packages(self, s, actor, pid, choices, *, kind=None, package_id=None):
        from .materials import COMPOSITION, ROOT, readable, roots
        from .native_documents import complete_selection, package_documents

        installed = roots(s, pid, choices)
        projection = None if installed else self._package_scope(s, actor, pid, choices)
        fallback = None
        if not installed and projection is None:
            fallback = self._rows(s, actor, pid, choices)
        has_composition = (
            projection[0] if projection is not None else any(COMPOSITION in r.metadata_json for _, r in fallback or [])
        )
        if installed or has_composition:
            result = []
            for row in installed:
                if package_id is not None and row.artifact_id != package_id:
                    continue
                package_kind = "prototype" if row.metadata_json[ROOT]["kind"] == "prototype" else "handbook"
                if kind is not None and kind != package_kind:
                    continue
                if not readable(self.service, s, actor, pid, row):
                    continue
                art = s.get(Artifact, row.artifact_id)
                meta = row.metadata_json[ROOT]
                if not art or art.project_id != pid:
                    continue
                if meta["kind"] == "documents" and not complete_selection(
                    self.service, s, actor, pid, choices, package_documents(s, pid, row)
                ):
                    continue
                result.append(
                    {
                        "id": art.id,
                        "kind": "prototype" if meta["kind"] == "prototype" else "handbook",
                        "sha256": art.sha256,
                        "size_bytes": art.size_bytes,
                        "name": row.name,
                        "adapter_version": "material-dom-1",
                        "package_key": row.entity_id,
                        "revision_id": row.id,
                        "format": meta["format"],
                    }
                )
            return result
        snapshots = (
            projection[1]
            if projection is not None
            else {s.get(Artifact, r.artifact_id).snapshot_id for _, r in fallback or [] if r.artifact_id}
        )
        rows = []
        for art in s.scalars(
            select(Artifact).where(Artifact.project_id == pid, Artifact.relative_path == "offline/original.html")
        ):
            if package_id is not None and art.id != package_id:
                continue
            if art.snapshot_id not in snapshots:
                continue
            snapshot = s.get(SourceSnapshot, art.snapshot_id)
            package_kind = {"charge-offline-prototype": "prototype", "charge-offline-handbook": "handbook"}.get(
                snapshot.source_key if snapshot else ""
            )
            if not package_kind or (kind is not None and kind != package_kind):
                continue
            policy = artifact_policy(s, art)
            if policy.get("offline_archive_invalid") or any(
                policy.get(key) and not self.service._can(s, actor, pid, cap)
                for key, cap in (("requires_source_read", "source:read"), ("requires_trace_read", "trace:read"))
            ):
                continue
            if package_kind == "handbook":
                document_ids = list(
                    s.scalars(
                        select(Revision.entity_id)
                        .join(Entity, Revision.entity_id == Entity.id)
                        .join(Artifact, Revision.artifact_id == Artifact.id)
                        .where(
                            Revision.project_id == pid,
                            Entity.project_id == pid,
                            Entity.type == "document",
                            Artifact.snapshot_id == art.snapshot_id,
                            Artifact.project_id == pid,
                        )
                    )
                )
                if not complete_selection(self.service, s, actor, pid, choices, document_ids):
                    continue
            rows.append(
                {
                    "id": art.id,
                    "kind": package_kind,
                    "sha256": art.sha256,
                    "size_bytes": art.size_bytes,
                    "name": "云收费原型" if package_kind == "prototype" else "云收费知识手册",
                    "adapter_version": "charge-dom-1",
                    "format": "charge-prototype" if package_kind == "prototype" else "charge-handbook",
                }
            )
        return rows

    def _package_scope(self, s, actor, pid, choices):
        """Whole-source readers need ownership/selection facts, not every body."""
        if not s.info.get("vks_read_only") or not (
            self.service._can(s, actor, pid, "source:read") and self.service._can(s, actor, pid, "trace:read")
        ):
            return None
        from .materials import COMPOSITION
        from .reading import chunks

        snapshots, composition = set(), False
        for batch in chunks(choices.values()):
            query = (
                select(
                    Revision.id,
                    Revision.entity_id,
                    Revision.project_id,
                    Revision.artifact_id,
                    Revision.metadata_json,
                    Entity.project_id,
                    Entity.type,
                    Artifact.project_id,
                    Artifact.snapshot_id,
                )
                .join(Entity, Entity.id == Revision.entity_id)
                .outerjoin(Artifact, Artifact.id == Revision.artifact_id)
                .where(Revision.id.in_(batch))
            )
            for rid, eid, rpid, artifact_id, metadata, epid, kind, apid, snapshot in s.execute(query):
                if (
                    choices.get(eid) != rid
                    or rpid != pid
                    or epid != pid
                    or kind not in {"page", "action", "document", "section", "business_point"}
                    or (artifact_id and apid != pid)
                ):
                    continue
                composition = composition or COMPOSITION in metadata
                if artifact_id:
                    snapshots.add(snapshot)
        return composition, snapshots

    def _source_fingerprint(self, s, actor, pid, choices, documents, packages):
        """Chat source identity excludes binding counters and never reads chapter bodies."""
        from .reading import chunks

        mapped = {}
        legacy = set()
        for entity, revision in documents:
            ids = revision.metadata_json.get("section_revision_ids")
            if isinstance(ids, list):
                for rid in ids:
                    if isinstance(rid, str):
                        mapped.setdefault(rid, set()).add((revision.artifact_id, entity.stable_key))
            else:
                legacy.add((revision.artifact_id, entity.stable_key))
        chapters = []
        for batch in chunks(choices.values()):
            rows = list(
                s.scalars(
                    select(Revision)
                    .join(Entity, Entity.id == Revision.entity_id)
                    .where(
                        Revision.id.in_(batch),
                        Revision.project_id == pid,
                        Entity.project_id == pid,
                        Entity.type == "section",
                    )
                    .options(
                        load_only(
                            Revision.id,
                            Revision.entity_id,
                            Revision.project_id,
                            Revision.artifact_id,
                            Revision.metadata_json,
                            Revision.content_sha256,
                        )
                    )
                )
            )
            self.reader.reading.preload(s, rows)
            for row in rows:
                membership = (row.artifact_id, row.metadata_json.get("document_key"))
                if (
                    choices.get(row.entity_id) != row.id
                    or membership not in mapped.get(row.id, set()) | legacy
                    or not self.service._visible_revision(s, actor, pid, row)
                ):
                    continue
                # _visible_revision retains the existing complete-reader fast path
                # while still checking entity/artifact ownership for every chapter.
                chapters.append((row.id, row.content_sha256))
        return sha(
            canonical(
                {
                    "documents": sorted(
                        (entity.id, revision.id, revision.content_sha256) for entity, revision in documents
                    ),
                    "chapters": sorted(chapters),
                    "packages": sorted((p["id"], p["kind"], p.get("revision_id", ""), p["sha256"]) for p in packages),
                }
            )
        )

    def catalog(self, actor, pid, aid=None, release_id=None):
        with self.reader.reading.db() as s:
            analysis, release, choices, _ = self.reader.scope(s, actor, pid, aid, release_id)
            rows = self._rows(s, actor, pid, choices, types={"document"})
            from .document_navigation import build_navigation
            from .materials import primary
            from .native_documents import catalog_locations, index_modified

            packages = self._packages(s, actor, pid, choices)
            native_locations = catalog_locations(s, pid, choices, rows, packages)
            modified = {e.id: index_modified(s, pid, r) for e, r in rows if e.id in native_locations}
            for package in packages:
                package["native_index_modified"] = any(
                    modified.get(entity_id, False)
                    for entity_id, package_id in native_locations.items()
                    if package_id == package["id"]
                )

            return {
                "analysis_set_id": analysis.id,
                "presentation": presentation_for_release(s, release)
                if release
                else presentation_for_analysis(s, analysis),
                "primary_package_id": primary(s, pid, choices),
                "release_id": release.id if release else None,
                "selection_version": analysis.selection_version,
                "source_fingerprint": self._source_fingerprint(s, actor, pid, choices, rows, packages),
                "editable": not release
                and analysis.state != "frozen"
                and self.service._can(s, actor, pid, "knowledge:edit"),
                "packages": packages,
                "navigation": build_navigation(
                    self.service, s, actor, pid, choices, [r.id for e, r in rows if e.type == "document"]
                ),
                "documents": sorted(
                    [
                        {
                            "id": e.id,
                            "revision_id": r.id,
                            "name": r.name,
                            "path": r.metadata_json.get("material_path")
                            or s.get(Artifact, r.artifact_id).relative_path,
                            "namespace": r.metadata_json.get("material_package_id")
                            or r.metadata_json.get("logical_source_key")
                            or s.get(Artifact, r.artifact_id).snapshot_id,
                            "package_id": r.metadata_json.get("material_package_id"),
                            "native_package_id": native_locations.get(e.id),
                            "native_index_modified": modified.get(e.id, False),
                        }
                        for e, r in rows
                        if e.type == "document"
                    ],
                    key=lambda x: x["name"],
                ),
            }

    def content(self, actor, pid, package_id, aid=None, release_id=None):
        with self.service.db() as s:
            _, _, choices, _ = self.reader.scope(s, actor, pid, aid, release_id)
            if not any(
                row["id"] == package_id for row in self._packages(s, actor, pid, choices, package_id=package_id)
            ):
                raise missing()
            artifact = s.get(Artifact, package_id)
            raw = (self.service.data_dir / "objects" / artifact.object_key).read_bytes()
            if len(raw) != artifact.size_bytes or sha(raw) != artifact.sha256:
                raise DomainError("VKS_INTEGRITY_ERROR", "原始资料完整性校验失败", 409)
            self.service.audit(s, actor, pid, "interactive.package.read", artifact.id)
            return raw

    def document(self, actor, pid, rid, aid=None, release_id=None, *, native=True):
        with self.reader.reading.db() as s:
            _, _, choices, _ = self.reader.scope(s, actor, pid, aid, release_id)
            entity, row = selected_id(s, self.service, actor, pid, choices, rid, "document")
            chapters = self.documents._sections(s, entity, row)
            from .document_projection import readable_document
            from .native_documents import describe

            visible = {
                c.id
                for c in chapters
                if choices.get(c.entity_id) == c.id and self.service._visible_revision(s, actor, pid, c)
            }
            return {
                "id": entity.id,
                "revision_id": row.id,
                "name": row.name,
                **readable_document(row, chapters, visible),
                "native": describe(
                    self.service,
                    s,
                    actor,
                    pid,
                    choices,
                    row,
                    chapters,
                    self._packages(s, actor, pid, choices, kind="handbook"),
                )
                if native
                else None,
            }

    def bindings(self, actor, pid, aid=None, release_id=None, *, session=None):
        from .reading import chunks

        with nullcontext(session) if session is not None else self.reader.reading.db() as s:
            analysis, release, choices, _ = self.reader.scope(s, actor, pid, aid, release_id)
            relations = [
                edge
                for edge in self.service._relations(s, analysis, release)
                if edge["relation_type"] in {"has_business_point", "supported_by_section"}
            ]
            relation_ids = {rid for edge in relations for rid in (edge["from_revision_id"], edge["to_revision_id"])}
            rows = {r.id: (e, r) for e, r in self._rows(s, actor, pid, choices, revision_ids=relation_ids)}
            document_keys = {r.metadata_json.get("document_key") for e, r in rows.values() if e.type == "section"}
            document_ids = set()
            for batch in chunks(document_keys):
                for eid in s.scalars(
                    select(Entity.id).where(
                        Entity.project_id == pid, Entity.type == "document", Entity.stable_key.in_(batch)
                    )
                ):
                    if eid in choices:
                        document_ids.add(choices[eid])
            rows.update({r.id: (e, r) for e, r in self._rows(s, actor, pid, choices, revision_ids=document_ids)})
            evidence_ids = {eid for edge in relations for eid in edge.get("evidence_ids", [])}
            evidence = []
            retained = s.info.setdefault("vks_prefetched", {})
            for batch in chunks(evidence_ids):
                evidence.extend(s.scalars(select(Evidence).where(Evidence.id.in_(batch))))
            retained.update({(Evidence, row.id): row for row in evidence})
            self.reader.reading.preload(s, evidence=evidence)
            full_access = self.service._can(s, actor, pid, "source:read") and self.service._can(
                s, actor, pid, "trace:read"
            )

            def evidence_visible(eid):
                row = s.get(Evidence, eid)
                if not full_access:
                    return bool(row and self.service._visible_evidence(s, actor, pid, row))
                art = s.get(Artifact, row.artifact_id) if row else None
                return bool(row and row.project_id == pid and art and art.project_id == pid)

            visible = [
                edge
                for edge in relations
                if edge["from_revision_id"] in rows
                and edge["to_revision_id"] in rows
                and (full_access or self.service._visible_relation(s, actor, pid, edge))
                and all(evidence_visible(eid) for eid in edge.get("evidence_ids", []))
            ]
            missing_points = set()
            for edge in relations:
                if (
                    edge["from_revision_id"] in rows
                    and edge["to_revision_id"] not in rows
                    and edge["relation_type"] == "supported_by_section"
                    and not edge["excluded"]
                    and (full_access or self.service._visible_relation(s, actor, pid, edge))
                    and all(evidence_visible(eid) for eid in edge.get("evidence_ids", []))
                    and self.service._visible_revision(s, actor, pid, s.get(Revision, edge["to_revision_id"]))
                ):
                    missing_points.add(edge["from_revision_id"])
            package_ids = {p["id"] for p in self._packages(s, actor, pid, choices, kind="prototype")}
            # Index once rather than scanning all relations and documents for
            # every binding. A selected orphan chapter is not a citation to the
            # current document merely because its logical document key matches.
            section_edges = {}
            for edge in visible:
                if edge["relation_type"] == "supported_by_section" and not edge["excluded"]:
                    section_edges.setdefault(edge["from_revision_id"], []).append(edge)
            documents = self._citation_documents(
                s, rows, {edge["to_revision_id"] for edges in section_edges.values() for edge in edges}
            )
            out = []
            for edge in visible:
                action = rows[edge["from_revision_id"]][1]
                meta = action.metadata_json.get("interactive_anchor")
                if edge["relation_type"] != "has_business_point" or not meta or meta["package_id"] not in package_ids:
                    continue
                point = rows[edge["to_revision_id"]][1]
                citations = []
                missing_citation = point.id in missing_points or not section_edges.get(point.id)
                for citation in section_edges.get(point.id, []):
                    section = rows[citation["to_revision_id"]][1]
                    document = documents.get(section.id)
                    if document:
                        citations.append(
                            {
                                "revision_id": section.id,
                                "name": section.name,
                                "content": section.content,
                                "document_revision_id": document[1].id,
                                "document_name": document[1].name,
                            }
                        )
                    else:
                        missing_citation = True
                out.append(
                    {
                        "id": edge["id"],
                        "binding_version": edge["row_version"],
                        "excluded": edge["excluded"],
                        "validity_status": "outdated"
                        if missing_citation and edge["validity_status"] == "valid"
                        else edge["validity_status"],
                        "point_revision_id": point.id,
                        "title": point.name,
                        "summary": point.content,
                        "package_id": meta["package_id"],
                        "target": meta["target"],
                        "sections": citations,
                        "scope": "module" if meta["target"].get("selector") == "body" else "control",
                        "origin": point.metadata_json.get("origin", "manual"),
                        "model_id": point.metadata_json.get("model_id", ""),
                    }
                )
            return out

    def _citation_documents(self, s, rows, section_ids):
        by_key = {r.metadata_json.get("document_key") for _, r in rows.values() if r.id in section_ids}
        result = {}
        for entity, document in rows.values():
            if entity.type != "document" or entity.stable_key not in by_key:
                continue
            try:
                chapters = self.documents._sections(s, entity, document)
            except DomainError:
                continue
            for chapter in chapters:
                if chapter.id in section_ids:
                    result[chapter.id] = (entity, document)
        return result

    def save(self, actor, pid, form, expected, key, *, generation=None, validate=None):
        service = self.service
        with service.db() as s:
            analysis, choices = draft(s, service, actor, pid, form.analysis_set_id, allow_frozen=True)
            if not any(
                p["id"] == form.package_id and p["kind"] == "prototype" for p in self._packages(s, actor, pid, choices)
            ):
                raise missing()
            art = s.get(Artifact, form.package_id)
            locator = form.target.model_dump()
            signature = {k: v for k, v in locator.items() if k != "open_steps"}
            package = next(p for p in self._packages(s, actor, pid, choices) if p["id"] == art.id)
            action_key = (
                "interactive."
                + sha(canonical({"package": package.get("package_key") or art.id, "target": signature}))[:40]
            )
            existing = next(
                (
                    r
                    for _, r in self._rows(s, actor, pid, choices)
                    if r.metadata_json.get("interactive_anchor", {}).get("package_id") == art.id
                    and {k: v for k, v in r.metadata_json["interactive_anchor"]["target"].items() if k != "open_steps"}
                    == signature
                ),
                None,
            )
            action_entity = s.get(Entity, existing.entity_id) if existing else _identity(s, pid, "action", action_key)
            prior_action = s.get(Revision, choices.get(action_entity.id)) if action_entity else None
            prior_edges = _edges(s, analysis, prior_action.id) if prior_action else []
            binding = next((edge for edge in prior_edges if edge.relation_type == "has_business_point"), None)

            def write():
                if analysis.state == "frozen":
                    raise DomainError("VKS_STATE_CONFLICT", "快照已冻结，请先创建草稿", 409)
                # Internal model writes validate all exact inputs again under
                # this project/transaction lock. Other background links may have
                # advanced the counter without changing those inputs.
                version(
                    analysis.selection_version,
                    analysis.selection_version if generation and expected is None else expected,
                )
                if validate:
                    validate(s, analysis, choices)
                if binding:
                    _require_edges(s, service, actor, pid, [binding])
                    version(binding.id, form.binding_id)
                    version(binding.row_version, form.binding_version)
                    version(binding.to_revision_id, form.point_revision_id)
                elif form.binding_id or form.binding_version or form.point_revision_id:
                    raise missing()
                refs = [
                    selected_id(s, service, actor, pid, choices, rid, "section")[1]
                    for rid in dict.fromkeys(form.section_revision_ids)
                ]
                members = self._citation_documents(
                    s, {r.id: (e, r) for e, r in self._rows(s, actor, pid, choices)}, {r.id for r in refs}
                )
                if any(r.id not in members for r in refs):
                    raise missing()
                action = prior_action
                if not action:
                    action_entity_new = action_entity or _new_entity(s, pid, "action", action_key)
                    action = self.documents._revision(
                        s,
                        action_entity_new,
                        art,
                        form.target.label,
                        "",
                        {"kind": "file", "relative_path": art.relative_path},
                        {
                            "interactive_anchor": {
                                "package_id": art.id,
                                "sha256": art.sha256,
                                "adapter_version": "charge-dom-1",
                                "target": locator,
                            }
                        },
                    )
                    _select(s, analysis, action)
                prior = s.get(Revision, binding.to_revision_id) if binding else None
                point_entity = (
                    s.get(Entity, prior.entity_id)
                    if prior
                    else _new_entity(s, pid, "business_point", action_key + ".knowledge")
                )
                edges = _edges(s, analysis, prior.id) if prior else []
                _require_edges(s, service, actor, pid, edges)
                inputs = (
                    [
                        selected_id(s, service, actor, pid, choices, rid, "section")[1]
                        for rid in generation["input_revision_ids"]
                    ]
                    if generation
                    else []
                )
                point = self.editor._point_revision(
                    s, pid, action, point_entity, prior, form, refs, input_revisions=inputs, generation=generation
                )
                _select(s, analysis, point)
                if prior and prior.id != point.id:
                    _repoint(edges, prior, point, stale=False)
                self.editor._citations(s, analysis, point, edges, refs)
                ev = _evidence(s, analysis, point, s.get(Artifact, point.artifact_id))
                saved = _write_relation(s, analysis, action, point, "has_business_point", ev, binding)
                if generation:
                    saved.manual_lock = False
                    s.get(Evidence, ev).evidence_kind = "inferred"
                    s.get(Evidence, ev).claim = "模型根据原文章节推断的控件知识关联"
                analysis.selection_version += 1
                s.flush()
                service.audit(s, actor, pid, "interactive.binding.save", saved.id)
                return {"binding_id": saved.id, "selection_version": analysis.selection_version}

            result = service.idem(
                s, actor, pid, "interactive.binding.save", key, {"expected": expected, **form.model_dump()}, write
            )
            current = s.get(Relation, result["binding_id"])
            if not current:
                raise missing()
            _require_edges(s, service, actor, pid, [current])
            return result

    def exclude(self, actor, pid, bid, form, expected):
        with self.service.db() as s:
            analysis, _ = draft(s, self.service, actor, pid, form.analysis_set_id)
            version(analysis.selection_version, expected)
            edge = s.get(Relation, bid)
            if (
                not edge
                or edge.analysis_set_id != analysis.id
                or not s.get(Revision, edge.from_revision_id).metadata_json.get("interactive_anchor")
            ):
                raise missing()
            _require_edges(s, self.service, actor, pid, [edge])
            version(edge.row_version, form.binding_version)
            edge.excluded = form.excluded
            analysis.selection_version += 1
            self.service.audit(s, actor, pid, "interactive.binding.exclude", bid)
            return {"selection_version": analysis.selection_version}


def install_interactive(app, service, actor, envelope):
    from .interactive_ai import install_interactive_ai

    install_interactive_ai(app, service, actor, envelope)
    shared = InteractiveKnowledgeService(service)
    prefix = "/api/v3/projects/{pid}/interactive"
    ActorDep = Annotated[Actor, Depends(actor)]

    @app.get(prefix + "/catalog", response_model=PrototypeEnvelope[InteractiveCatalog])
    def catalog(
        pid: str, request: Request, a: ActorDep, analysis_set_id: str | None = None, release_id: str | None = None
    ):
        return envelope(request, shared.catalog(a, pid, analysis_set_id, release_id))

    @app.get(prefix + "/packages/{package_id}/content", response_class=Response)
    def content(
        pid: str, package_id: str, a: ActorDep, analysis_set_id: str | None = None, release_id: str | None = None
    ):
        return Response(
            shared.content(a, pid, package_id, analysis_set_id, release_id),
            media_type="application/octet-stream",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    @app.get(prefix + "/documents/{rid}", response_model=PrototypeEnvelope[InteractiveDocument])
    def document(
        pid: str,
        rid: str,
        request: Request,
        a: ActorDep,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        native: bool = True,
    ):
        return envelope(request, shared.document(a, pid, rid, analysis_set_id, release_id, native=native))

    @app.get(prefix + "/bindings", response_model=PrototypeEnvelope[list[InteractiveBindingOutput]])
    def bindings(
        pid: str, request: Request, a: ActorDep, analysis_set_id: str | None = None, release_id: str | None = None
    ):
        return envelope(request, shared.bindings(a, pid, analysis_set_id, release_id))

    @app.post(prefix + "/bindings", response_model=PrototypeEnvelope[InteractiveSaved])
    def save(
        pid: str,
        payload: InteractiveBinding,
        request: Request,
        a: ActorDep,
        if_match: Annotated[str, Header()],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ):
        return envelope(request, shared.save(a, pid, payload, if_match, idempotency_key))

    @app.post(prefix + "/bindings/{bid}/exclude", response_model=PrototypeEnvelope[InteractiveSelection])
    def exclude(
        pid: str, bid: str, payload: Exclusion, request: Request, a: ActorDep, if_match: Annotated[str, Header()]
    ):
        return envelope(request, shared.exclude(a, pid, bid, payload, if_match))
