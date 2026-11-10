"""Transactional edits for prototype states and their business knowledge."""

from copy import deepcopy
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from packages.analyzers.prototype_manifest import _check_image, _intersects
from packages.analyzers.vks_ingest import BundleError

from .documents import DocumentService
from .models import Analysis, Artifact, Entity, Evidence, Project, PrototypeSettings, Relation, Revision, Selection, uid
from .prototype_read import find_selected, missing, page_state, selected_id
from .reading import revision_policy
from .service import DomainError, canonical, sha

Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9._-]{0,95}$")]
Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
Ref = Annotated[str, Field(min_length=1, max_length=96)]
Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Viewport(Input):
    width: int = Field(ge=1, le=7680, strict=True)
    height: int = Field(ge=1, le=7680, strict=True)
    devicePixelRatio: float = Field(ge=0.5, le=4, allow_inf_nan=False)


class Anchor(Input):
    anchorKey: Key
    actionKey: Key
    label: Label
    rect: tuple[Coordinate, Coordinate, Coordinate, Coordinate]
    targetStateKey: Key | None = None


class AnchorEdit(Input):
    analysis_set_id: Ref
    page_revision_id: Ref
    state_key: Key
    name: Label
    anchors: list[Anchor] = Field(max_length=128)
    viewport: Viewport | None = None
    upload_artifact_id: Ref | None = None
    clear_surface: bool = False


class SettingsEdit(Input):
    analysis_set_id: Ref | None = None
    release_id: Ref | None = None
    entry_page_key: Key | None = None
    menu_order: list[Key] = Field(default_factory=list, max_length=512)


class PointText(Input):
    title: Label
    summary: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    applicability: str = Field(default="", max_length=2000)
    outcome: str = Field(default="", max_length=2000)
    exceptions: str = Field(default="", max_length=2000)


class BindingEdit(Input):
    analysis_set_id: Ref
    action_revision_id: Ref
    page_revision_id: Ref
    point_key: Key
    point_revision_id: Ref | None = None
    point: PointText | None = None
    section_revision_ids: list[Ref] | None = Field(default=None, max_length=20)
    state_keys: list[Key] = Field(default_factory=list, max_length=32)
    priority: int = Field(default=0, ge=0, le=1000)
    binding_version: int | None = Field(default=None, ge=1)
    excluded: bool = False


def draft(s, service, actor, pid, aid, capability="knowledge:edit"):
    service.require(s, actor, pid, capability)
    s.scalar(select(Project.id).where(Project.id == pid).with_for_update())
    analysis = s.scalar(select(Analysis).where(Analysis.project_id == pid, Analysis.id == aid).with_for_update())
    if not analysis:
        raise missing()
    if analysis.state == "frozen":
        raise DomainError("VKS_STATE_CONFLICT", "快照已冻结，请先创建草稿", 409)
    choices = dict(
        s.execute(
            select(Selection.entity_id, Selection.entity_revision_id).where(Selection.analysis_set_id == aid)
        ).all()
    )
    return analysis, choices


def version(actual, supplied, message="草稿或目标已变化，请刷新"):
    if str(actual) != str(supplied).strip('"'):
        raise DomainError("VKS_VERSION_CONFLICT", message, 409, {"current_version": actual})


def _artifact(s, service, pid, snapshot, prefix, raw, kind, media, metadata):
    digest = sha(raw)
    fingerprint = sha(canonical({"sha256": digest, "metadata": metadata}))
    path = f"{prefix}/{fingerprint}.{'png' if kind == 'screenshot' else 'json'}"
    artifact = s.scalar(
        select(Artifact).where(
            Artifact.project_id == pid, Artifact.snapshot_id == snapshot, Artifact.relative_path == path
        )
    )
    if not artifact:
        artifact = Artifact(
            id=uid(),
            project_id=pid,
            snapshot_id=snapshot,
            relative_path=path,
            sha256=digest,
            size_bytes=len(raw),
            kind=kind,
            media_type=media,
            sensitivity="internal",
            object_key=service._put_object(raw),
            metadata_json=metadata,
        )
        s.add(artifact)
        s.flush()
    return artifact


def _select(s, analysis, revision):
    choice = s.get(Selection, (analysis.id, revision.entity_id))
    if choice:
        choice.entity_revision_id = revision.id
    else:
        s.add(Selection(analysis_set_id=analysis.id, entity_id=revision.entity_id, entity_revision_id=revision.id))


def _identity(s, pid, kind, key):
    return s.scalar(select(Entity).where(Entity.project_id == pid, Entity.type == kind, Entity.stable_key == key))


def _new_entity(s, pid, kind, key):
    entity = Entity(id=uid(), project_id=pid, type=kind, stable_key=key)
    s.add(entity)
    s.flush()
    return entity


def _edges(s, analysis, rid):
    return list(
        s.scalars(
            select(Relation).where(
                Relation.analysis_set_id == analysis.id,
                or_(Relation.from_revision_id == rid, Relation.to_revision_id == rid),
            )
        )
    )


def _repoint(edges, previous, updated, *, stale):
    for edge in edges:
        if edge.from_revision_id == previous.id:
            edge.from_revision_id = updated.id
        if edge.to_revision_id == previous.id:
            edge.to_revision_id = updated.id
        if stale and not edge.excluded:
            edge.validity_status = "stale"
            if edge.causal_basis == "correlated_runtime":
                edge.causal_basis = "not_established"


def _require_edges(s, service, actor, pid, edges):
    for edge in edges:
        if (
            not service._visible_relation(s, actor, pid, edge)
            or not all(
                service._visible_revision(s, actor, pid, s.get(Revision, rid))
                for rid in (edge.from_revision_id, edge.to_revision_id)
            )
            or not all(
                (ev := s.get(Evidence, eid)) and service._visible_evidence(s, actor, pid, ev)
                for eid in edge.evidence_ids
            )
        ):
            raise missing()


def _evidence(s, analysis, revision, artifact):
    key = "prototype." + revision.id
    current = s.scalar(
        select(Evidence).where(
            Evidence.analysis_set_id == analysis.id,
            Evidence.stable_key == key,
            Evidence.artifact_id == artifact.id,
            Evidence.content_sha256 == artifact.sha256,
        )
    )
    if current:
        return current.id
    row = Evidence(
        id=uid(),
        project_id=analysis.project_id,
        analysis_set_id=analysis.id,
        artifact_id=artifact.id,
        stable_key=key,
        evidence_kind="manual",
        content_sha256=artifact.sha256,
        locator={
            "relative_path": artifact.relative_path,
            "revision_id": revision.id,
            **{
                key: revision.locator[field]
                for key, field in (("line_start", "startLine"), ("line_end", "endLine"))
                if field in revision.locator
            },
        },
        excerpt=revision.content[:8000],
        claim="当前修订的维护说明或原文章节",
    )
    s.add(row)
    s.flush()
    return row.id


def _write_relation(s, analysis, source, target, kind, evidence, current=None, **metadata):
    edge = current or Relation(
        id=uid(),
        project_id=analysis.project_id,
        analysis_set_id=analysis.id,
        from_revision_id=source.id,
        to_revision_id=target.id,
        relation_type=kind,
        excluded=False,
        causal_basis="not_established",
    )
    edge.validity_status = "valid"
    edge.manual_lock = True
    edge.evidence_ids = [evidence]
    edge.metadata_json = {**edge.metadata_json, **metadata} if edge.metadata_json else metadata
    s.add(edge)
    s.flush()
    return edge


class PrototypeEditor:
    def __init__(self, service):
        self.service = service
        self.documents = DocumentService(service)

    def save_settings(self, actor, pid, form, expected):
        from .prototype_read import PrototypeReader

        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            s.scalar(select(Project.id).where(Project.id == pid).with_for_update())
            _, _, choices, _ = PrototypeReader(self.service).scope(s, actor, pid, form.analysis_set_id, form.release_id)
            row = s.get(PrototypeSettings, pid)
            version(row.row_version if row else 0, expected, "项目导览设置已更新")
            keys = set(form.menu_order)
            if len(keys) != len(form.menu_order):
                raise DomainError("VKS_INVALID_INPUT", "快速菜单页面不可重复", 422)
            for key in keys | ({form.entry_page_key} if form.entry_page_key else set()):
                _, page = find_selected(s, self.service, actor, pid, choices, "page", key)
                manifest = page.metadata_json.get("prototype_manifest")
                if not manifest or (key == form.entry_page_key and not page_state(page).get("surface")):
                    raise DomainError("VKS_INVALID_INPUT", "入口需要当前版本中的可读画面", 422)
            if not row:
                row = PrototypeSettings(project_id=pid)
                s.add(row)
            row.entry_page_key = form.entry_page_key
            row.menu_order = list(form.menu_order)
            s.flush()
            self.service.audit(s, actor, pid, "prototype.settings.save", pid)
            return {"row_version": row.row_version, "entry_page_key": row.entry_page_key, "menu_order": row.menu_order}

    def save_anchors(self, actor, pid, page_key, form, expected, key):
        service = self.service
        payload = form.model_dump(mode="json", exclude_none=True)
        with service.db() as s:
            analysis, choices = draft(s, service, actor, pid, form.analysis_set_id)
            entity, page = find_selected(s, service, actor, pid, choices, "page", page_key)
            if not page.metadata_json.get("prototype_manifest"):
                raise missing()

            def save():
                version(analysis.selection_version, expected)
                version(page.id, form.page_revision_id)
                manifest = deepcopy(page.metadata_json["prototype_manifest"])
                old = next((row for row in manifest["states"] if row["stateKey"] == form.state_key), None)
                known = {row["stateKey"] for row in manifest["states"]} | {form.state_key}
                if len(known) > 32:
                    raise DomainError("VKS_INVALID_INPUT", "一个原型页最多支持 32 个状态", 422)
                action_keys, anchor_keys = set(), set()
                for i, anchor in enumerate(form.anchors):
                    x, y, w, h = anchor.rect
                    if (
                        anchor.actionKey in action_keys
                        or anchor.anchorKey in anchor_keys
                        or (anchor.targetStateKey and anchor.targetStateKey not in known)
                        or w <= 0
                        or h <= 0
                        or x + w > 1 + 1e-9
                        or y + h > 1 + 1e-9
                        or any(_intersects(anchor.rect, other.rect) for other in form.anchors[:i])
                    ):
                        raise DomainError("VKS_LOCATOR_INVALID", "热区重叠、越界或引用无效", 422)
                    action_keys.add(anchor.actionKey)
                    anchor_keys.add(anchor.anchorKey)
                origin = s.get(Artifact, page.artifact_id)
                if not origin:
                    raise missing()
                state = dict(
                    old or {},
                    stateKey=form.state_key,
                    name=form.name,
                    origin="declared",
                    anchors=[
                        anchor.model_dump(mode="json", exclude={"label"}, exclude_none=True) for anchor in form.anchors
                    ],
                )
                if (form.viewport is None) != (form.upload_artifact_id is None) or (
                    form.clear_surface and form.viewport
                ):
                    raise DomainError("VKS_INVALID_INPUT", "补录画面需要同时提供 PNG 和准确视口", 422)
                surfaces = {
                    s.get(Artifact, aid).relative_path: s.get(Artifact, aid)
                    for aid in origin.metadata_json.get("prototype_surface_artifact_ids", [])
                    if s.get(Artifact, aid)
                }
                if form.clear_surface:
                    state.pop("surface", None)
                    state.pop("viewport", None)
                if form.upload_artifact_id:
                    service.require(s, actor, pid, "source:import")
                    raw, upload = service._upload_data(s, actor, pid, form.upload_artifact_id)
                    if len(raw) > 32 * 1024 * 1024:
                        raise DomainError("VKS_ARTIFACT_TOO_LARGE", "状态 PNG 超过 32 MiB", 413)
                    viewport = form.viewport.model_dump(mode="json")
                    surface = {"kind": "raster", "artifactPath": "upload.png", "sha256": upload.sha256}
                    try:
                        _check_image(
                            {"viewport": viewport, "surface": surface},
                            {
                                "upload.png": {
                                    "source_id": "upload",
                                    "kind": "screenshot",
                                    "media_type": "image/png",
                                    "sha256": upload.sha256,
                                    "content": raw,
                                }
                            },
                            "upload",
                        )
                    except BundleError as exc:
                        raise DomainError(exc.code, exc.message, 422) from exc
                    art = _artifact(
                        s, service, pid, origin.snapshot_id, "prototype-surfaces", raw, "screenshot", "image/png", {}
                    )
                    surfaces[art.relative_path] = art
                    state.update(
                        origin="prototype_export",
                        viewport=viewport,
                        surface={**surface, "artifactPath": art.relative_path},
                    )
                elif not form.clear_surface and old:
                    state["origin"] = old["origin"]
                manifest["states"] = [state if row["stateKey"] == form.state_key else row for row in manifest["states"]]
                if not old:
                    manifest["states"].append(state)
                used = {row["surface"]["artifactPath"] for row in manifest["states"] if row.get("surface")}
                if not used <= surfaces.keys():
                    raise DomainError("VKS_INVALID_EVIDENCE", "已有画面引用缺失", 409)
                base = origin.metadata_json.get("original_artifact_id") or origin.id
                new_artifact = _artifact(
                    s,
                    service,
                    pid,
                    origin.snapshot_id,
                    "prototype-pages",
                    canonical(manifest).encode(),
                    "fixture",
                    "application/json",
                    {
                        "original_artifact_id": base,
                        "prototype_surface_artifact_ids": sorted(surfaces[path].id for path in used),
                    },
                )
                updated = self.documents._revision(
                    s,
                    entity,
                    new_artifact,
                    page.name,
                    page.content,
                    page.locator,
                    {
                        **page.metadata_json,
                        "prototype_manifest": manifest,
                        "row_version": page.metadata_json.get("row_version", 1) + 1,
                    },
                )
                _select(s, analysis, updated)
                for anchor in form.anchors:
                    action = _identity(s, pid, "action", anchor.actionKey)
                    rid = choices.get(action.id) if action else None
                    prior = s.get(Revision, rid) if rid else None
                    if action and not prior:
                        historical = s.scalars(select(Revision).where(Revision.entity_id == action.id))
                        if not all(
                            row.metadata_json.get("page_key") == page_key
                            and row.metadata_json.get("prototype_action")
                            and service._visible_revision(s, actor, pid, row)
                            for row in historical
                        ):
                            raise missing()
                    if prior and (
                        prior.metadata_json.get("page_key") != page_key
                        or not prior.metadata_json.get("prototype_action")
                        or not service._visible_revision(s, actor, pid, prior)
                    ):
                        raise missing()
                    if prior and prior.name == anchor.label:
                        continue
                    action = action or _new_entity(s, pid, "action", anchor.actionKey)
                    action_artifact = new_artifact
                    metadata = {
                        "page_key": page_key,
                        "prototype_action": True,
                        "event": "click",
                        "evidence_origin": "manual",
                        **(
                            {"source_scope": page.metadata_json["source_scope"]}
                            if page.metadata_json.get("source_scope")
                            else {}
                        ),
                    }
                    new_action = self.documents._revision(
                        s,
                        action,
                        action_artifact,
                        anchor.label,
                        "",
                        {"kind": "file", "relative_path": action_artifact.relative_path},
                        metadata,
                    )
                    _select(s, analysis, new_action)
                    if prior and prior.id != new_action.id:
                        edges = _edges(s, analysis, prior.id)
                        _require_edges(s, service, actor, pid, edges)
                        _repoint(edges, prior, new_action, stale=True)
                if old:
                    remaining = {anchor["actionKey"] for row in manifest["states"] for anchor in row["anchors"]}
                    removed = {anchor["actionKey"] for anchor in old["anchors"]} - action_keys
                    for action_key in removed:
                        _, old_action = find_selected(s, service, actor, pid, choices, "action", action_key)
                        edges = [
                            edge
                            for edge in _edges(s, analysis, old_action.id)
                            if edge.relation_type == "has_business_point" and not edge.excluded
                        ]
                        affected = [
                            edge
                            for edge in edges
                            if action_key not in remaining
                            or form.state_key in edge.metadata_json.get("prototype_scope", {}).get("states", [])
                        ]
                        _require_edges(s, service, actor, pid, affected)
                        for edge in affected:
                            edge.validity_status = "stale" if action_key in remaining else "broken"
                analysis.selection_version += 1
                service.audit(s, actor, pid, "prototype.anchors.save", entity.id)
                return {"page_revision_id": updated.id, "selection_version": analysis.selection_version}

            result = service.idem(
                s,
                actor,
                pid,
                "prototype.anchors.save",
                key,
                {"page_key": page_key, "expected": expected, **payload},
                save,
            )
            if not service._visible_revision(s, actor, pid, s.get(Revision, result["page_revision_id"])):
                raise missing()
            return result

    def save_binding(self, actor, pid, action_key, form, expected, key):
        service = self.service
        payload = form.model_dump(mode="json", exclude_none=True)
        with service.db() as s:
            analysis, choices = draft(s, service, actor, pid, form.analysis_set_id)
            _, action = find_selected(s, service, actor, pid, choices, "action", action_key)
            page_key = action.metadata_json.get("page_key")
            if not page_key or action.metadata_json.get("status") == "removed":
                raise missing()
            _, page = find_selected(s, service, actor, pid, choices, "page", page_key)
            applied = {
                row["stateKey"]
                for row in page.metadata_json.get("prototype_manifest", {}).get("states", [])
                if any(anchor["actionKey"] == action_key for anchor in row["anchors"])
            }

            def save():
                version(analysis.selection_version, expected)
                version(page.id, form.page_revision_id)
                version(action.id, form.action_revision_id)
                if (
                    not applied
                    or not set(form.state_keys) <= applied
                    or len(set(form.state_keys)) != len(form.state_keys)
                ):
                    raise DomainError("VKS_INVALID_INPUT", "适用状态必须包含该动作的准确热区", 422)
                point = _identity(s, pid, "business_point", form.point_key)
                prior = s.get(Revision, choices[point.id]) if point and point.id in choices else None
                if prior and not service._visible_revision(s, actor, pid, prior):
                    raise missing()
                if point and not prior:
                    raise DomainError("VKS_STATE_CONFLICT", "业务点键已占用，请选择当前草稿中的业务点", 409)
                if prior or form.point_revision_id:
                    version(prior.id if prior else "", form.point_revision_id)
                if not prior and not form.point:
                    raise DomainError("VKS_INVALID_INPUT", "新建业务点需要标题和说明", 422)
                if form.excluded and (not prior or form.point or form.section_revision_ids is not None):
                    raise DomainError("VKS_INVALID_INPUT", "移除关联时只需提交当前业务点和绑定版本", 422)
                if form.section_revision_ids is not None and len(set(form.section_revision_ids)) != len(
                    form.section_revision_ids
                ):
                    raise DomainError("VKS_INVALID_INPUT", "章节修订重复", 422)

                edges = _edges(s, analysis, prior.id) if prior else []
                binding = next(
                    (
                        row
                        for row in edges
                        if row.from_revision_id == action.id and row.relation_type == "has_business_point"
                    ),
                    None,
                )
                if binding or form.binding_version is not None:
                    version(binding.row_version if binding else 0, form.binding_version)
                if binding:
                    _require_edges(s, service, actor, pid, [binding])
                if form.excluded:
                    if not binding:
                        raise missing()
                    binding.excluded = True
                    saved = prior
                else:
                    refs = [
                        selected_id(s, service, actor, pid, choices, rid, "section")[1]
                        for rid in (form.section_revision_ids or [])
                    ]
                    saved = prior
                    if form.point or form.section_revision_ids is not None:
                        _require_edges(s, service, actor, pid, edges)
                        point = point or _new_entity(s, pid, "business_point", form.point_key)
                        saved = self._point_revision(s, pid, action, point, prior, form, refs)
                        _select(s, analysis, saved)
                        if prior and prior.id != saved.id:
                            _repoint(edges, prior, saved, stale=False)
                            point_evidence = _evidence(s, analysis, saved, s.get(Artifact, saved.artifact_id))
                            for edge in edges:
                                if edge.relation_type == "has_business_point":
                                    edge.evidence_ids = [point_evidence]
                        if form.section_revision_ids is not None:
                            self._citations(s, analysis, saved, edges, refs)
                    ev = _evidence(s, analysis, saved, s.get(Artifact, saved.artifact_id))
                    binding = _write_relation(
                        s,
                        analysis,
                        action,
                        saved,
                        "has_business_point",
                        ev,
                        binding,
                        prototype_scope={"states": sorted(form.state_keys), "priority": form.priority},
                    )
                    binding.excluded = False
                analysis.selection_version += 1
                service.audit(s, actor, pid, "prototype.binding.save", binding.id)
                s.flush()
                return {
                    "point_key": form.point_key,
                    "point_revision_id": saved.id,
                    "binding_id": binding.id,
                    "binding_version": binding.row_version,
                    "selection_version": analysis.selection_version,
                }

            result = service.idem(
                s,
                actor,
                pid,
                "prototype.binding.save",
                key,
                {"action_key": action_key, "expected": expected, **payload},
                save,
            )
            if not service._visible_revision(s, actor, pid, s.get(Revision, result["point_revision_id"])):
                raise missing()
            return result

    def _point_revision(self, s, pid, action, entity, prior, form, refs):
        old = s.get(Artifact, prior.artifact_id) if prior else None
        original = s.get(Artifact, action.artifact_id)
        provenance = {
            row["revision_id"]: row for row in (old.metadata_json.get("input_revision_provenance", []) if old else [])
        }
        for row in refs:
            provenance[row.id] = {
                "revision_id": row.id,
                "entity_id": row.entity_id,
                "sha256": row.content_sha256,
                "artifact_id": row.artifact_id,
                **revision_policy(s, row),
            }
        if len(provenance) > 256:
            raise DomainError("VKS_STATE_CONFLICT", "业务点的历史来源已达到上限", 409)
        wording = (
            form.point.model_dump(mode="json")
            if form.point
            else {
                "title": prior.name,
                "summary": prior.content,
                **{key: prior.metadata_json.get(key, "") for key in ("applicability", "outcome", "exceptions")},
            }
        )
        # The first authored point keeps the exact permissions of its action
        # material. Its provenance does not narrow when a citation is replaced.
        meta = {
            "original_artifact_id": (old.metadata_json.get("original_artifact_id") or old.id) if old else original.id,
            "input_revision_provenance": [provenance[key] for key in sorted(provenance)],
        }
        art = _artifact(
            s,
            self.service,
            pid,
            (old or original).snapshot_id,
            "prototype-points",
            canonical(wording).encode(),
            "fixture",
            "application/json",
            meta,
        )
        return self.documents._revision(
            s,
            entity,
            art,
            wording["title"],
            wording["summary"],
            {"kind": "file", "relative_path": art.relative_path},
            {key: wording[key] for key in ("applicability", "outcome", "exceptions")},
        )

    @staticmethod
    def _citations(s, analysis, point, edges, refs):
        prior = {row.to_revision_id: row for row in edges if row.relation_type == "supported_by_section"}
        selected = {row.id for row in refs}
        for rid, edge in prior.items():
            if rid not in selected:
                edge.excluded = True
        for section in refs:
            ev = _evidence(s, analysis, section, s.get(Artifact, section.artifact_id))
            edge = _write_relation(s, analysis, point, section, "supported_by_section", ev, prior.get(section.id))
            edge.excluded = False
