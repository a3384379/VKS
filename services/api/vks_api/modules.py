"""Project business modules with independent, permission-filtered memberships."""

from __future__ import annotations

from typing import ClassVar

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .knowledge import SourceConnector, SourceSnapshot
from .models import Artifact, Base, Identified, Revision, uid


def fail(code, message, status=422):
    from .service import DomainError

    raise DomainError(code, message, status)


class BusinessModule(Identified, Base):
    __tablename__ = "business_module"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("business_module.id"), nullable=True)
    key: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__: ClassVar[dict] = {"version_id_col": row_version}


class ModuleEntity(Base):
    __tablename__ = "module_entity"
    module_id: Mapped[str] = mapped_column(ForeignKey("business_module.id"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entity.id"), primary_key=True)


class ModuleSource(Base):
    __tablename__ = "module_source"
    module_id: Mapped[str] = mapped_column(ForeignKey("business_module.id"), primary_key=True)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshot.id"), primary_key=True)


class ModuleConnector(Base):
    __tablename__ = "module_connector"
    module_id: Mapped[str] = mapped_column(ForeignKey("business_module.id"), primary_key=True)
    connector_id: Mapped[str] = mapped_column(ForeignKey("source_connector.id"), primary_key=True)


class ModuleService:
    def __init__(self, service):
        self.service = service

    def _workspace(self, actor, pid, analysis_set_id=None, release_id=None):
        from .reading import ReadingService

        return ReadingService(self.service).selection_workspace(actor, pid, analysis_set_id, release_id)

    def _module(self, s, pid, mid):
        module = s.get(BusinessModule, mid)
        if not module or module.project_id != pid:
            fail("VKS_NOT_FOUND", "未找到业务模块", 404)
        return module

    def _parent(self, s, pid, mid, parent_id):
        visited = {mid}
        while parent_id:
            if parent_id in visited:
                fail("VKS_INVALID_INPUT", "模块树不能形成循环")
            visited.add(parent_id)
            parent_id = self._module(s, pid, parent_id).parent_id

    def _context(self, s, actor, pid, workspace):
        entities = {row["entity_id"]: row for row in workspace["entities"]}
        snapshots = {}
        if self.service._can(s, actor, pid, "source:read") and self.service._can(s, actor, pid, "trace:read"):
            snapshots.update({row["source_snapshot_id"]: set() for row in workspace["sources"]})
        for row in entities.values():
            revision = s.get(Revision, row["entity_revision_id"])
            artifact = s.get(Artifact, revision.artifact_id) if revision.artifact_id else None
            if artifact:
                snapshots.setdefault(artifact.snapshot_id, set()).add(revision.entity_id)
        connectors = (
            {row.id: row for row in s.scalars(select(SourceConnector).where(SourceConnector.project_id == pid))}
            if self.service._can(s, actor, pid, "project:manage")
            else {}
        )
        return entities, snapshots, connectors

    def _membership(self, s, module, context):
        entities, snapshots, connectors = context
        direct = set(s.scalars(select(ModuleEntity.entity_id).where(ModuleEntity.module_id == module.id)))
        sources = set(s.scalars(select(ModuleSource.source_snapshot_id).where(ModuleSource.module_id == module.id)))
        connections = set(s.scalars(select(ModuleConnector.connector_id).where(ModuleConnector.module_id == module.id)))
        visible_direct = direct & entities.keys()
        visible_sources = sources & snapshots.keys()
        visible_connectors = connections & connectors.keys()
        expanded = set(visible_direct)
        for snapshot_id in sources:
            expanded.update(snapshots.get(snapshot_id, set()))
        # Connector membership follows its logical source key across snapshots;
        # snapshot membership remains fixed to that immutable source version.
        for snapshot_id, members in snapshots.items():
            snapshot = s.get(SourceSnapshot, snapshot_id)
            if snapshot and any(s.get(SourceConnector, cid).key == snapshot.source_key for cid in connections):
                expanded.update(members)
        return {
            "entity_ids": sorted(visible_direct),
            "source_snapshot_ids": sorted(visible_sources),
            "connector_ids": sorted(visible_connectors),
            "expanded": expanded,
            "has_members": bool(direct or sources or connections),
            "visible": bool(expanded or visible_sources or visible_connectors),
        }

    @staticmethod
    def dto(module, count=0, children=None):
        return {
            "id": module.id,
            "parent_id": module.parent_id,
            "key": module.key,
            "name": module.name,
            "description": module.description,
            "sort_order": module.sort_order,
            "row_version": module.row_version,
            "entity_count": count,
            "children": children or [],
        }

    def tree(self, actor, pid, analysis_set_id=None, release_id=None):
        workspace = self._workspace(actor, pid, analysis_set_id, release_id)
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            context = self._context(s, actor, pid, workspace)
            rows = list(
                s.scalars(
                    select(BusinessModule)
                    .where(BusinessModule.project_id == pid)
                    .order_by(BusinessModule.sort_order, BusinessModule.name, BusinessModule.id)
                )
            )
            by_parent = {}
            for module in rows:
                by_parent.setdefault(module.parent_id, []).append(module)

            def branch(parent_id):
                output = []
                for module in by_parent.get(parent_id, []):
                    members = self._membership(s, module, context)
                    children = branch(module.id)
                    if members["has_members"] and not members["visible"] and not children:
                        continue
                    output.append(self.dto(module, len(members["expanded"]), children))
                return output

            return branch(None)

    def context(self, actor, pid, release_id):
        rows = []

        def append(nodes):
            for node in nodes:
                rows.append({key: value for key, value in node.items() if key != "children"})
                append(node["children"])

        append(self.tree(actor, pid, release_id=release_id))
        return rows

    def create(self, actor, pid, payload):
        workspace = self._workspace(actor, pid)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            self._parent(s, pid, None, payload.get("parent_id"))
            if payload.get("parent_id"):
                parent = self._module(s, pid, payload["parent_id"])
                membership = self._membership(s, parent, self._context(s, actor, pid, workspace))
                if membership["has_members"] and not membership["visible"]:
                    fail("VKS_NOT_FOUND", "未找到业务模块", 404)
            if s.scalar(
                select(BusinessModule).where(BusinessModule.project_id == pid, BusinessModule.key == payload["key"])
            ):
                fail("VKS_STATE_CONFLICT", "模块 key 已存在", 409)
            module = BusinessModule(
                id=uid(),
                project_id=pid,
                key=payload["key"],
                name=payload["name"],
                parent_id=payload.get("parent_id"),
                description=payload.get("description", ""),
                sort_order=payload.get("sort_order", 0),
            )
            s.add(module)
            s.flush()
            self.service.audit(s, actor, pid, "module.create", module.id)
            return self.dto(module)

    def update(self, actor, pid, mid, payload, version):
        workspace = self._workspace(actor, pid)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            module = self._module(s, pid, mid)
            context = self._context(s, actor, pid, workspace)
            for current in (module, self._module(s, pid, payload["parent_id"]) if payload.get("parent_id") else None):
                membership = self._membership(s, current, context) if current else None
                if membership and membership["has_members"] and not membership["visible"]:
                    fail("VKS_NOT_FOUND", "未找到业务模块", 404)
            self.service._version(module.row_version, version)
            self._parent(s, pid, mid, payload.get("parent_id", module.parent_id))
            for key in ("name", "description", "parent_id", "sort_order"):
                if key in payload:
                    setattr(module, key, payload[key])
            module.row_version += 1
            self.service.audit(s, actor, pid, "module.update", module.id)
            s.flush()
            return self.dto(module)

    def delete(self, actor, pid, mid, version):
        workspace = self._workspace(actor, pid)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            module = self._module(s, pid, mid)
            context = self._context(s, actor, pid, workspace)
            membership = self._membership(s, module, context)
            if membership["has_members"] and not membership["visible"]:
                fail("VKS_NOT_FOUND", "未找到业务模块", 404)
            self.service._version(module.row_version, version)
            if s.scalar(select(BusinessModule).where(BusinessModule.parent_id == mid)):
                fail("VKS_STATE_CONFLICT", "请先移动或删除下级模块", 409)
            for model in (ModuleEntity, ModuleSource, ModuleConnector):
                for binding in s.scalars(select(model).where(model.module_id == mid)):
                    if (
                        isinstance(binding, ModuleEntity)
                        and binding.entity_id not in context[0]
                        or isinstance(binding, ModuleSource)
                        and binding.source_snapshot_id not in context[1]
                        or isinstance(binding, ModuleConnector)
                        and binding.connector_id not in context[2]
                    ):
                        fail("VKS_FORBIDDEN", "删除整个模块需要读取其所有归属对象", 403)
                    s.delete(binding)
            s.flush()
            s.delete(module)
            self.service.audit(s, actor, pid, "module.delete", mid)
            return {"deleted": True}

    def members(self, actor, pid, mid, analysis_set_id=None, release_id=None):
        workspace = self._workspace(actor, pid, analysis_set_id, release_id)
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            module = self._module(s, pid, mid)
            context = self._context(s, actor, pid, workspace)
            members = self._membership(s, module, context)
            if members["has_members"] and not members["visible"]:
                fail("VKS_NOT_FOUND", "未找到业务模块", 404)
            return self._members_dto(module, members, context)

    @staticmethod
    def _members_dto(module, members, context):
        return {
            "module_id": module.id,
            "row_version": module.row_version,
            **{key: members[key] for key in ("entity_ids", "source_snapshot_ids", "connector_ids")},
            "entities": [context[0][eid] for eid in sorted(members["expanded"])],
        }

    def set_members(self, actor, pid, mid, payload, version):
        workspace = self._workspace(actor, pid, payload.get("analysis_set_id"), payload.get("release_id"))
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            module = self._module(s, pid, mid)
            self.service._version(module.row_version, version)
            context = self._context(s, actor, pid, workspace)
            before = self._membership(s, module, context)
            if before["has_members"] and not before["visible"]:
                fail("VKS_NOT_FOUND", "未找到业务模块", 404)
            for model, key, column, visible in (
                (ModuleEntity, "entity_ids", "entity_id", set(context[0])),
                (ModuleSource, "source_snapshot_ids", "source_snapshot_id", set(context[1])),
                (ModuleConnector, "connector_ids", "connector_id", set(context[2])),
            ):
                requested = set(payload.get(key, []))
                if requested - visible:
                    fail("VKS_NOT_FOUND", "绑定对象不属于当前可访问基线", 404)
                old = {getattr(row, column): row for row in s.scalars(select(model).where(model.module_id == mid))}
                # Replacing the visible subset must never delete bindings this
                # actor cannot see, including those outside this baseline.
                for identifier in (old.keys() & visible) - requested:
                    s.delete(old[identifier])
                for identifier in requested - old.keys():
                    s.add(model(module_id=mid, **{column: identifier}))
            module.row_version += 1
            self.service.audit(s, actor, pid, "module.members", mid)
            s.flush()
            return self._members_dto(module, self._membership(s, module, context), context)

    def search(self, actor, pid, payload):
        from .reading import ReadingService

        return ReadingService(self.service).search(actor, pid, payload)


class ModuleCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    parent_id: str | None = None
    description: str = Field(default="", max_length=8000)
    sort_order: int = 0


class ModuleUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=160)
    parent_id: str | None = None
    description: str | None = Field(default=None, max_length=8000)
    sort_order: int | None = None


class ModuleMembersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str | None = None
    release_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list, max_length=5000)
    source_snapshot_ids: list[str] = Field(default_factory=list, max_length=200)
    connector_ids: list[str] = Field(default_factory=list, max_length=200)


def install_modules(app, service, actor, envelope, API="/api/v1"):
    modules = ModuleService(service)
    actor_dependency = Depends(actor)

    @app.get(API + "/projects/{pid}/modules")
    def tree(
        pid: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a=actor_dependency,
    ):
        return envelope(request, modules.tree(a, pid, analysis_set_id, release_id))

    @app.post(API + "/projects/{pid}/modules", status_code=201)
    def create(pid: str, payload: ModuleCreateInput, request: Request, a=actor_dependency):
        return envelope(request, modules.create(a, pid, payload.model_dump()))

    @app.patch(API + "/projects/{pid}/modules/{mid}")
    def update(
        pid: str,
        mid: str,
        payload: ModuleUpdateInput,
        request: Request,
        a=actor_dependency,
        if_match: str = Header(...),
    ):
        data = payload.model_dump(exclude_unset=True)
        if any(value is None for key, value in data.items() if key != "parent_id"):
            fail("VKS_INVALID_INPUT", "仅 parent_id 支持空值")
        return envelope(request, modules.update(a, pid, mid, data, if_match))

    @app.delete(API + "/projects/{pid}/modules/{mid}")
    def delete(pid: str, mid: str, request: Request, a=actor_dependency, if_match: str = Header(...)):
        return envelope(request, modules.delete(a, pid, mid, if_match))

    @app.get(API + "/projects/{pid}/modules/{mid}/members")
    def members(
        pid: str,
        mid: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a=actor_dependency,
    ):
        return envelope(request, modules.members(a, pid, mid, analysis_set_id, release_id))

    @app.put(API + "/projects/{pid}/modules/{mid}/members")
    def set_members(
        pid: str,
        mid: str,
        payload: ModuleMembersInput,
        request: Request,
        a=actor_dependency,
        if_match: str = Header(...),
    ):
        return envelope(request, modules.set_members(a, pid, mid, payload.model_dump(exclude_none=True), if_match))

    return modules
