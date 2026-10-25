"""Scenario revisions, explicit capture approvals and redacted observed evidence."""

from __future__ import annotations

import json
import os
import re
import secrets

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from packages.scenario_dsl import ScenarioError, digest, validate_dsl
from services.browser_runner.network import NetworkDenied, NetworkPolicy, origin
from services.browser_runner.runner import CaptureError, run_capture

from .models import (
    Analysis,
    Artifact,
    Base,
    Entity,
    Evidence,
    Identified,
    Job,
    Project,
    Relation,
    Revision,
    Selection,
    now,
    uid,
)
from .service import Actor, DomainError, canonical, future, sha


class ScenarioRevision(Identified, Base):
    __tablename__ = "scenario_revision"
    __table_args__ = (UniqueConstraint("scenario_id", "revision_no"),)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("entity.id"), index=True)
    entity_revision_id: Mapped[str] = mapped_column(ForeignKey("entity_revision.id"), unique=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    dsl: Mapped[dict] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(36))


class CaptureEnvironment(Identified, Base):
    __tablename__ = "capture_environment"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    key: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20))
    policy: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(36))


class CaptureDataset(Identified, Base):
    __tablename__ = "dataset_fixture"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    key: Mapped[str] = mapped_column(String(96))
    name: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(36))


class CaptureApproval(Identified, Base):
    __tablename__ = "capture_approval"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"))
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    environment_id: Mapped[str] = mapped_column(ForeignKey("capture_environment.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_fixture.id"))
    requestor_actor_id: Mapped[str] = mapped_column(String(36))
    approved_by: Mapped[str] = mapped_column(String(36))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(40))
    used_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CaptureRecord(Identified, Base):
    __tablename__ = "capture_session"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revision.id"))
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    environment_id: Mapped[str] = mapped_column(ForeignKey("capture_environment.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_fixture.id"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("capture_approval.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    redaction_version: Mapped[str] = mapped_column(String(64), default="vks-runner-redact-1")
    source_alignment: Mapped[str] = mapped_column(String(20), default="unknown")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScenarioInput(Input):
    analysis_set_id: str
    dsl: dict


class ViewportInput(Input):
    width: int = Field(default=1440, ge=320, le=3840)
    height: int = Field(default=900, ge=240, le=2160)


class EnvironmentInput(Input):
    key: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(pattern=r"^(mock|test)$")
    base_origin: str = Field(max_length=2048)
    allowed_origins: list[str] = Field(min_length=1, max_length=20)
    allowed_cidrs: list[str] = Field(min_length=1, max_length=20)
    allow_loopback: bool = False
    allowed_write_paths: list[str] = Field(default_factory=list, max_length=50)
    auth_ref: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    service_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    viewport: ViewportInput = Field(default_factory=ViewportInput)


class DatasetInput(Input):
    key: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,95}$")
    name: str = Field(min_length=1, max_length=160)
    data: dict
    synthetic: bool = True


class ApprovalInput(Input):
    analysis_set_id: str
    scenario_revision_id: str
    requestor_actor_id: str | None = None
    expires_in_seconds: int = Field(default=300, ge=30, le=900)
    reason: str = Field(min_length=1, max_length=2000)


class CaptureInput(Input):
    scenario_revision_id: str
    environment_ref: str
    approval_ref: str | None = None


class ScenarioService:
    def __init__(self, service):
        self.service = service

    def _analysis(self, s, actor, pid, aid, write=False):
        self.service.require(s, actor, pid, "knowledge:edit" if write else "knowledge:read")
        a, _ = self.service._selected(s, actor, pid, aid, None)
        if not a:
            raise DomainError("VKS_NOT_FOUND", "未找到分析集合", 404)
        if write and a.state == "frozen":
            raise DomainError("VKS_STATE_CONFLICT", "发布版场景不可修改，请创建新草稿", 409)
        return a

    def _baseline_digest(self, s, analysis):
        # Scenario graph edits can reference earlier immutable captures while
        # every source and non-scenario knowledge revision stays fixed.
        revisions = [
            {"entity_id": r.entity_id, "sha256": r.content_sha256}
            for r in self.service._revisions(s, analysis.id)
            if s.get(Entity, r.entity_id).type != "scenario"
        ]
        return digest({"sources": analysis.sources, "selections": sorted(revisions, key=lambda x: x["entity_id"])})

    def _validate(self, s, actor, pid, a, dsl):
        entities = {s.get(Entity, r.entity_id).stable_key: r for r in self.service._revisions(s, a.id)}
        action_keys = {k for k, r in entities.items() if s.get(Entity, r.entity_id).type == "action"}
        action_ids = {r.entity_id for k, r in entities.items() if k in action_keys}

        def capture_valid(item):
            if actor is not None and not self.service._can(s, actor, pid, "trace:read"):
                return False
            c = s.get(CaptureRecord, item.get("captureRef"))
            sr = s.get(ScenarioRevision, item.get("scenarioRevisionRef"))
            if not c or not sr or c.project_id != pid or c.scenario_revision_id != sr.id or c.state != "succeeded":
                return False
            if sr.dsl["scenario_key"] != dsl["scenario_key"] or c.result.get(
                "baseline_sha256"
            ) != self._baseline_digest(s, a):
                return False
            if item.get("evidenceRefs"):
                for eid in item["evidenceRefs"]:
                    e = s.get(Evidence, eid)
                    if (
                        not e
                        or e.project_id != pid
                        or e.analysis_set_id != c.analysis_set_id
                        or e.evidence_kind != "observed"
                        or eid not in c.result.get("evidence_ids", [])
                    ):
                        return False
            return True

        try:
            validate_dsl(
                dsl,
                project_key=s.get(Project, pid).key,
                baseline_key=a.key,
                action_keys=action_keys,
                action_ids=action_ids,
                capture_validator=capture_valid,
            )
        except ScenarioError as e:
            raise DomainError(
                "VKS_INVALID_EVIDENCE" if e.code == "VKS_INVALID_EVIDENCE" else "VKS_INVALID_INPUT", e.message
            ) from e

    def assert_publishable(self, s, analysis):
        blockers = []
        for revision in self.service._revisions(s, analysis.id):
            if s.get(Entity, revision.entity_id).type != "scenario":
                continue
            scenario = s.scalar(select(ScenarioRevision).where(ScenarioRevision.entity_revision_id == revision.id))
            try:
                if not scenario or scenario.project_id != analysis.project_id:
                    raise DomainError("VKS_INVALID_EVIDENCE", "场景修订缺少固定 DSL")
                self._validate(s, None, analysis.project_id, analysis, scenario.dsl)
            except DomainError:
                blockers.append("场景的版本或运行观察依赖需要复核：" + revision.entity_id)
        return blockers

    def clone_for_analysis(self, s, actor, source, target):
        # The new baseline is an explicit new combination of the exact source
        # vector. Carry captures only while that vector remains byte-for-byte
        # identical. Capture approval is single-use and never inherited.
        changed = {}
        for revision in list(self.service._revisions(s, source.id)):
            entity = s.get(Entity, revision.entity_id)
            if entity.type != "scenario":
                continue
            old = s.scalar(select(ScenarioRevision).where(ScenarioRevision.entity_revision_id == revision.id))
            if not old:
                raise DomainError("VKS_INVALID_EVIDENCE", "来源场景修订不可用", 409)
            dsl = json.loads(canonical(old.dsl))
            if "explanation_graph" in dsl:
                dsl["explanation_graph"]["baselineKey"] = target.key
            if "approval_ref" in dsl:
                # An unissued reference satisfies the immutable DSL contract;
                # the server must issue a new approval before any capture run.
                dsl["approval_ref"] = uid()
            self._validate(s, None, target.project_id, target, dsl)
            saved = self._new_revision(s, actor, target, entity, dsl)
            changed[revision.id] = saved["entity_revision_id"]
        s.flush()
        for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == target.id)):
            if relation.from_revision_id in changed or relation.to_revision_id in changed:
                relation.from_revision_id = changed.get(relation.from_revision_id, relation.from_revision_id)
                relation.to_revision_id = changed.get(relation.to_revision_id, relation.to_revision_id)
                relation.validity_status = "stale"
                relation.row_version += 1
        return changed

    def _dto(self, r):
        return {
            "scenario_id": r.scenario_id,
            "scenario_revision_id": r.id,
            "entity_revision_id": r.entity_revision_id,
            "revision_no": r.revision_no,
            "content_sha256": r.content_sha256,
            "created_at": r.created_at,
            "dsl": r.dsl,
        }

    def create(self, actor, pid, payload, key):
        with self.service.db() as s:
            a = self._analysis(s, actor, pid, payload["analysis_set_id"], True)
            self._validate(s, actor, pid, a, payload["dsl"])
            dsl = payload["dsl"]

            def save():
                entity = s.scalar(
                    select(Entity).where(
                        Entity.project_id == pid, Entity.type == "scenario", Entity.stable_key == dsl["scenario_key"]
                    )
                )
                if entity and s.get(Selection, (a.id, entity.id)):
                    raise DomainError("VKS_STATE_CONFLICT", "当前版本已存在此场景，请编辑其草稿", 409)
                if not entity:
                    entity = Entity(id=uid(), project_id=pid, type="scenario", stable_key=dsl["scenario_key"])
                    s.add(entity)
                    s.flush()
                return self._new_revision(s, actor, a, entity, dsl)

            return self.service.idem(s, actor, pid, "scenario.create", key, payload, save)

    def _new_revision(self, s, actor, a, entity, dsl):
        previous = s.scalar(
            select(ScenarioRevision)
            .where(ScenarioRevision.scenario_id == entity.id)
            .order_by(ScenarioRevision.revision_no.desc())
        )
        number = previous.revision_no + 1 if previous else 1
        srid = uid()
        metadata = {
            "dsl": dsl,
            "scenario_revision_id": srid,
            "revision_no": number,
            "row_version": number,
            "evidence_origin": "declared",
        }
        content = canonical(dsl)
        content_hash = digest(
            {
                "content": content,
                "metadata": metadata,
                "artifact_id": None,
                "locator": {},
                "schema_version": "1.0",
                "name": dsl["name"],
            }
        )
        rev = Revision(
            id=uid(),
            entity_id=entity.id,
            project_id=a.project_id,
            artifact_id=None,
            name=dsl["name"],
            content=content,
            content_sha256=content_hash,
            semantic_content_sha256=sha(content),
            locator={},
            metadata_json=metadata,
        )
        s.add(rev)
        s.flush()
        sr = ScenarioRevision(
            id=srid,
            scenario_id=entity.id,
            entity_revision_id=rev.id,
            project_id=a.project_id,
            revision_no=number,
            dsl=dsl,
            content_sha256=content_hash,
            created_by=actor.id,
        )
        s.add(sr)
        s.flush()
        selection = s.get(Selection, (a.id, entity.id))
        if selection:
            selection.entity_revision_id = rev.id
        else:
            s.add(Selection(analysis_set_id=a.id, entity_id=entity.id, entity_revision_id=rev.id))
        a.selection_version += 1
        self.service.audit(s, actor, a.project_id, "scenario.save", sr.id, metadata={"revision_no": number})
        return self._dto(sr)

    def update(self, actor, sid, payload, version):
        with self.service.db() as s:
            entity = s.get(Entity, sid)
            if not entity or entity.type != "scenario":
                raise DomainError("VKS_NOT_FOUND", "未找到场景", 404)
            a = self._analysis(s, actor, entity.project_id, payload["analysis_set_id"], True)
            selection = s.get(Selection, (a.id, sid))
            previous = (
                s.scalar(
                    select(ScenarioRevision).where(ScenarioRevision.entity_revision_id == selection.entity_revision_id)
                )
                if selection
                else None
            )
            if not previous:
                raise DomainError("VKS_NOT_FOUND", "未找到场景草稿", 404)
            self.service._version(previous.revision_no, version)
            if payload["dsl"].get("scenario_key") != entity.stable_key:
                raise DomainError("VKS_INVALID_INPUT", "场景逻辑键不可通过编辑改变")
            self._validate(s, actor, entity.project_id, a, payload["dsl"])
            return self._new_revision(s, actor, a, entity, payload["dsl"])

    def list(self, actor, pid, analysis_set_id=None, release_id=None):
        w = self.service.workspace(actor, pid, analysis_set_id, release_id)
        revision_ids = [e["entity_revision_id"] for e in w["scenarios"]]
        with self.service.db() as s:
            return [
                self._dto(r)
                for r in s.scalars(
                    select(ScenarioRevision).where(ScenarioRevision.entity_revision_id.in_(revision_ids))
                )
            ]

    def get(self, actor, pid, sid, rid):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            r = s.get(ScenarioRevision, rid) or s.scalar(
                select(ScenarioRevision).where(ScenarioRevision.entity_revision_id == rid)
            )
            if not r or r.project_id != pid or r.scenario_id != sid:
                raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            entity_revision = s.get(Revision, r.entity_revision_id)
            if not self.service._visible_revision(s, actor, pid, entity_revision):
                raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            if not self.service._can(s, actor, pid, "knowledge:edit"):
                from .models import Release

                if not any(
                    any(x["entity_revision_id"] == r.entity_revision_id for x in rel.manifest_json["selections"])
                    for rel in s.scalars(select(Release).where(Release.project_id == pid))
                ):
                    raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            return self._dto(r)

    def _resolve_auth(self, policy, *, check_binding=True):
        from services.browser_runner.auth_registry import AuthRegistryError, resolve_auth

        if not policy.get("auth_ref"):
            return None
        key = self.service._private_setting("runner-auth-binding-key", lambda: secrets.token_urlsafe(48))
        try:
            material = resolve_auth(
                policy["auth_ref"], policy["base_origin"], key, project_key=policy.get("auth_project_key")
            )
            if check_binding and any(policy.get(k) != v for k, v in material.binding.items()):
                raise AuthRegistryError("认证引用已更新")
            return material
        except AuthRegistryError:
            raise DomainError(
                "VKS_APPROVAL_REQUIRED", "实例测试认证引用不可用或已轮换，请登记新的环境版本并重新审批", 403
            ) from None

    def create_environment(self, actor, pid, payload):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "环境只能由人员管理员登记", 403)
            if payload.get("allow_loopback") and os.getenv("VKS_RUNNER_ALLOW_LOOPBACK") != "1":
                raise DomainError("VKS_FORBIDDEN", "实例未启用本地合成测试环境", 403)
            from urllib.parse import urlsplit

            if urlsplit(payload["base_origin"]).path not in ("", "/") or urlsplit(payload["base_origin"]).query:
                raise DomainError("VKS_INVALID_INPUT", "环境只能登记基础来源，路径在 DSL 中指定")
            for path in payload.get("allowed_write_paths", []):
                if not path.startswith("/") or path.startswith("//") or "?" in path or "\\" in path:
                    raise DomainError("VKS_INVALID_INPUT", "写路径必须是精确的站内路径")
            try:
                policy = NetworkPolicy(
                    payload["base_origin"],
                    payload["allowed_origins"],
                    payload["allowed_cidrs"],
                    allow_loopback=payload.get("allow_loopback", False),
                    write_paths=payload.get("allowed_write_paths", []),
                )
                for allowed in payload["allowed_origins"]:
                    policy.resolve(allowed)
            except NetworkDenied as e:
                raise DomainError("VKS_INVALID_INPUT", str(e)) from e
            viewport = ViewportInput(**payload.get("viewport", {})).model_dump()
            bound_policy = {k: v for k, v in payload.items() if k not in ("key", "name")}
            bound_policy["viewport"] = viewport
            if bound_policy.get("auth_ref"):
                bound_policy["auth_project_key"] = s.get(Project, pid).key
            authentication = self._resolve_auth(bound_policy, check_binding=False)
            if authentication:
                if authentication.allowed_project_keys is None and actor.id != self.service.owner_id:
                    raise DomainError("VKS_FORBIDDEN", "未限定项目的实例认证引用只能由实例管理员登记", 403)
                bound_policy.update(authentication.binding)
            obj = CaptureEnvironment(
                id=uid(),
                project_id=pid,
                key=payload["key"],
                name=payload["name"],
                kind=payload["kind"],
                policy=bound_policy,
                policy_sha256=digest({"key": payload["key"], "name": payload["name"], "policy": bound_policy}),
                created_by=actor.id,
                enabled=True,
            )
            s.add(obj)
            s.flush()
            self.service.audit(s, actor, pid, "environment.create", obj.id)
            return self._environment_dto(obj)

    def _environment_dto(self, e):
        return {
            "id": e.id,
            "key": e.key,
            "name": e.name,
            "kind": e.kind,
            "enabled": e.enabled,
            "policy": {k: value for k, value in e.policy.items() if k != "auth_binding_hmac"},
            "policy_sha256": e.policy_sha256,
        }

    def environments(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "capture:run")
            return [
                self._environment_dto(e)
                for e in s.scalars(select(CaptureEnvironment).where(CaptureEnvironment.project_id == pid))
            ]

    def create_dataset(self, actor, pid, payload):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "project:manage")
            if not actor.human or not payload["synthetic"]:
                raise DomainError("VKS_FORBIDDEN", "仅允许人员管理员登记合成数据集", 403)
            serialized = canonical(payload["data"])
            if len(serialized) > 100_000 or re.search(
                r'(?i)"[^"\n]*(?:password|secret|token|authorization|cookie|api.?key)[^"\n]*"\s*:', serialized
            ):
                raise DomainError("VKS_INVALID_INPUT", "数据集过大或包含凭据字段")
            obj = CaptureDataset(
                id=uid(),
                project_id=pid,
                key=payload["key"],
                name=payload["name"],
                data=payload["data"],
                content_sha256=digest(payload["data"]),
                synthetic=True,
                created_by=actor.id,
            )
            s.add(obj)
            s.flush()
            self.service.audit(s, actor, pid, "dataset.create", obj.id)
            return {
                "id": obj.id,
                "key": obj.key,
                "name": obj.name,
                "synthetic": True,
                "content_sha256": obj.content_sha256,
            }

    def datasets(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "capture:run")
            return [
                {"id": d.id, "key": d.key, "name": d.name, "synthetic": d.synthetic, "content_sha256": d.content_sha256}
                for d in s.scalars(select(CaptureDataset).where(CaptureDataset.project_id == pid))
            ]

    def _capture_parts(self, s, actor, pid, aid, rid):
        a = self._analysis(s, actor, pid, aid, True)
        r = s.get(ScenarioRevision, rid)
        selection = s.get(Selection, (a.id, r.scenario_id)) if r else None
        if not r or r.project_id != pid or not selection or selection.entity_revision_id != r.entity_revision_id:
            raise DomainError("VKS_NOT_FOUND", "场景修订不属于当前草稿基线", 404)
        env = s.scalar(
            select(CaptureEnvironment).where(
                CaptureEnvironment.project_id == pid,
                CaptureEnvironment.key == r.dsl["environment_ref"],
                CaptureEnvironment.enabled.is_(True),
            )
        )
        data = s.scalar(
            select(CaptureDataset).where(CaptureDataset.project_id == pid, CaptureDataset.key == r.dsl["dataset_ref"])
        )
        if not env or not data:
            raise DomainError("VKS_APPROVAL_REQUIRED", "请先登记场景的测试环境与合成数据集", 403)
        if not data.synthetic or env.kind not in ("mock", "test"):
            raise DomainError("VKS_FORBIDDEN", "禁止生产环境或非合成数据采集", 403)
        self._resolve_auth(env.policy)
        manifest = digest(
            {
                "scenario": r.content_sha256,
                "analysis_set_id": a.id,
                "selection_version": a.selection_version,
                "environment": env.policy_sha256,
                "dataset": data.content_sha256,
            }
        )
        return a, r, env, data, manifest

    def approve(self, actor, sid, payload):
        with self.service.db() as s:
            entity = s.get(Entity, sid)
            if not entity or entity.type != "scenario":
                raise DomainError("VKS_NOT_FOUND", "未找到场景", 404)
            pid = entity.project_id
            self.service.require(s, actor, pid, "project:manage")
            self.service.require(s, actor, pid, "capture:run")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "采集审批只能由人员管理员签发", 403)
            a, r, env, data, h = self._capture_parts(
                s, actor, pid, payload["analysis_set_id"], payload["scenario_revision_id"]
            )
            approval_id = uid()
            if r.dsl["risk_level"] == "test_write":
                approved_dsl = json.loads(canonical(r.dsl))
                approved_dsl["approval_ref"] = approval_id
                new_revision = self._new_revision(s, actor, a, entity, approved_dsl)
                a, r, env, data, h = self._capture_parts(s, actor, pid, a.id, new_revision["scenario_revision_id"])
            obj = CaptureApproval(
                id=approval_id,
                project_id=pid,
                scenario_revision_id=r.id,
                analysis_set_id=a.id,
                environment_id=env.id,
                dataset_id=data.id,
                requestor_actor_id=payload.get("requestor_actor_id") or actor.id,
                approved_by=actor.id,
                manifest_sha256=h,
                expires_at=future(payload.get("expires_in_seconds", 300)),
            )
            s.add(obj)
            s.flush()
            self.service.audit(
                s, actor, pid, "capture.approve", obj.id, metadata={"reason": payload["reason"], "manifest_sha256": h}
            )
            return {
                "approval_ref": obj.id,
                "scenario_revision_id": r.id,
                "manifest_sha256": h,
                "expires_at": obj.expires_at,
                "requestor_actor_id": obj.requestor_actor_id,
            }

    def request_capture(self, actor, sid, payload, key):
        with self.service.db() as s:
            r = s.get(ScenarioRevision, payload["scenario_revision_id"])
            if not r:
                raise DomainError("VKS_NOT_FOUND", "未找到场景修订", 404)
            pid = r.project_id
            if sid not in (r.id, r.scenario_id, r.entity_revision_id):
                raise DomainError("VKS_NOT_FOUND", "场景与修订不匹配", 404)
            self.service.require(s, actor, pid, "capture:run")
            approval = s.get(CaptureApproval, payload.get("approval_ref")) if payload.get("approval_ref") else None
            if not approval or approval.project_id != pid or approval.requestor_actor_id != actor.id:
                return {
                    "decision": "approval_required",
                    "approval_url": f"/?project={pid}&page=settings",
                    "reason": "需要绑定人员、场景修订和环境版本的有效采集授权",
                }
            a, r, env, data, h = self._capture_parts(s, actor, pid, approval.analysis_set_id, r.id)
            if (
                payload["environment_ref"] != env.key
                or approval.scenario_revision_id != r.id
                or approval.expires_at <= now()
                or approval.manifest_sha256 != h
                or (r.dsl["risk_level"] == "test_write" and r.dsl.get("approval_ref") != approval.id)
            ):
                raise DomainError("VKS_APPROVAL_REQUIRED", "采集授权已过期或素材发生变化，需要重新审批", 403)
            if os.getenv("VKS_RUNNER_ENABLED") != "1":
                raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "采集 Runner 未启用；任务不会入队", 503)

            def enqueue():
                if approval.used_job_id:
                    raise DomainError("VKS_APPROVAL_REQUIRED", "此授权已用于其他任务，请重新审批", 403)
                j = self.service._new_job(
                    s,
                    actor,
                    pid,
                    "capture",
                    {
                        "scenario_revision_id": r.id,
                        "analysis_set_id": a.id,
                        "approval_ref": approval.id,
                        "risk_level": r.dsl["risk_level"],
                    },
                )
                approval.used_job_id = j.id
                capture = CaptureRecord(
                    id=uid(),
                    project_id=pid,
                    scenario_revision_id=r.id,
                    analysis_set_id=a.id,
                    environment_id=env.id,
                    dataset_id=data.id,
                    approval_id=approval.id,
                    job_id=j.id,
                    state="queued",
                    result={},
                )
                s.add(capture)
                s.flush()
                self.service.audit(s, actor, pid, "capture.request", capture.id)
                return {"decision": "accepted", "job": self.service.job_dto(j)}

            return self.service.idem(s, actor, pid, "capture.request", key, payload, enqueue)

    def execute_capture(self, job_id):
        with self.service.db() as s:
            job = s.get(Job, job_id)
            capture = s.scalar(select(CaptureRecord).where(CaptureRecord.job_id == job_id))
            if not job or not capture:
                raise DomainError("VKS_NOT_FOUND", "未找到采集任务", 404)
            from .jobs import current_actor

            self.service.require(
                s, current_actor(self.service, job.created_by, job.project_id, session=s), job.project_id, "capture:run"
            )
            approval = s.get(CaptureApproval, capture.approval_id)
            sr = s.get(ScenarioRevision, capture.scenario_revision_id)
            env = s.get(CaptureEnvironment, capture.environment_id)
            data = s.get(CaptureDataset, capture.dataset_id)
            a = s.get(Analysis, capture.analysis_set_id)
            if approval.expires_at <= now() or not env.enabled or a.state == "frozen":
                raise DomainError("VKS_APPROVAL_REQUIRED", "执行前采集授权已失效", 403)
            expected = digest(
                {
                    "scenario": sr.content_sha256,
                    "analysis_set_id": a.id,
                    "selection_version": a.selection_version,
                    "environment": env.policy_sha256,
                    "dataset": data.content_sha256,
                }
            )
            if approval.manifest_sha256 != expected:
                raise DomainError("VKS_APPROVAL_REQUIRED", "执行前版本发生变化", 403)
            dsl = json.loads(canonical(sr.dsl))
            authentication = self._resolve_auth(env.policy)
            environment = dict(env.policy, kind=env.kind)
            dataset = {"data": data.data, "synthetic": data.synthetic}

        authorization_failure = []

        def cancelled():
            with self.service.db() as s:
                current = s.get(Job, job_id)
                if not current or current.cancel_requested or current.state not in ("running", "cancelling"):
                    return True
                try:
                    self.service.require(
                        s,
                        current_actor(self.service, current.created_by, current.project_id, session=s),
                        current.project_id,
                        "capture:run",
                    )
                    live_environment = s.get(CaptureEnvironment, capture.environment_id)
                    live_approval = s.get(CaptureApproval, capture.approval_id)
                    live_analysis = s.get(Analysis, capture.analysis_set_id)
                    if (
                        not live_environment.enabled
                        or live_approval.expires_at <= now()
                        or live_analysis.selection_version != a.selection_version
                        or live_analysis.state == "frozen"
                    ):
                        return True
                    self._resolve_auth(live_environment.policy)
                except DomainError as error:
                    if not authorization_failure:
                        authorization_failure.append(error)
                    return True
                return False

        try:
            return run_capture(dsl, environment, dataset, cancelled=cancelled, auth=authentication)
        except CaptureError as e:
            if authorization_failure:
                raise authorization_failure[0]
            raise DomainError(
                "VKS_STATE_CONFLICT" if e.code == "VKS_CANCELLED" else "VKS_INVALID_INPUT", e.message
            ) from e
        except Exception as e:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "采集运行时不可用；请检查浏览器安装与受控环境", 503) from e

    def _network_evidence(self, s, capture, analysis, scenario, environment, requests):
        if not requests:
            return [], [], []
        raw = canonical(
            {"source_kind": "runtime_capture", "source_alignment": "unknown", "requests": requests}
        ).encode()
        h = self.service._put_object(raw)
        artifact = Artifact(
            id=uid(),
            project_id=capture.project_id,
            snapshot_id=capture.id,
            relative_path=f"captures/{capture.id}/requests.json",
            sha256=h,
            size_bytes=len(raw),
            kind="capture",
            media_type="application/json",
            sensitivity="internal",
            object_key=h,
            metadata_json={
                "capture_id": capture.id,
                "source_kind": "runtime_capture",
                "redaction_version": capture.redaction_version,
            },
        )
        s.add(artifact)
        s.flush()
        selected = {r.entity_id: r for r in self.service._revisions(s, analysis.id)}
        apis = [r for r in selected.values() if s.get(Entity, r.entity_id).type == "api"]
        steps = {step["step_key"]: step for step in scenario.dsl["steps"]}
        evidence_ids, resolutions = [], []
        changed = False
        for index, request in enumerate(requests):
            request_id = request.get("request_id", "request-" + str(index + 1))
            step_key = request.get("step_key", "")
            evidence = Evidence(
                id=uid(),
                project_id=capture.project_id,
                analysis_set_id=analysis.id,
                artifact_id=artifact.id,
                stable_key=f"capture.{capture.id}.request.{index}",
                evidence_kind="observed",
                content_sha256=h,
                locator={
                    "relative_path": artifact.relative_path,
                    "source_kind": "runtime_capture",
                    "step_key": step_key,
                    "request_id": request_id,
                },
                excerpt=canonical(request),
                claim="受控测试会话中观察到该请求；时间同窗可能包含轮询，不能证明动作触发或生产源码因果",
            )
            s.add(evidence)
            s.flush()
            evidence_ids.append(evidence.id)
            action_id = steps.get(step_key, {}).get("action_entity_id")
            action = selected.get(action_id)
            matches = []
            for api in apis:
                if request.get("origin") != origin(environment.policy["base_origin"]):
                    continue
                metadata = api.metadata_json
                if (
                    metadata.get("method") != request.get("method")
                    or metadata.get("resolution_state", "resolved") != "resolved"
                ):
                    continue
                if (
                    environment.policy.get("service_key")
                    and metadata.get("service_key") != environment.policy["service_key"]
                ):
                    continue
                route = metadata.get("normalized_route", "")
                if not route or request.get("path_truncated"):
                    continue
                pattern = "".join(
                    "[^/]+" if part.startswith("{") else re.escape(part) for part in re.split(r"(\{[^/{}]+\})", route)
                )
                if re.fullmatch(pattern, request.get("path", "")):
                    matches.append(api)
            state, reason = "unresolved", "ACTION_OR_API_NOT_UNIQUE"
            if action and s.get(Entity, action.entity_id).type == "action" and len(matches) == 1:
                api = matches[0]
                conditions = api.metadata_json.get("conditions", {})
                condition_hash = api.metadata_json.get("condition_fingerprint")
                if conditions or condition_hash not in (None, digest({})):
                    reason = "REQUEST_CONDITIONS_UNVERIFIED"
                else:
                    existing = s.scalar(
                        select(Relation).where(
                            Relation.analysis_set_id == analysis.id,
                            Relation.from_revision_id == action.id,
                            Relation.to_revision_id == api.id,
                            Relation.relation_type == "observed_during",
                        )
                    )
                    if existing:
                        state, reason = "preserved", "EXISTING_REVIEW_UNCHANGED"
                    else:
                        relation = Relation(
                            id=uid(),
                            project_id=capture.project_id,
                            analysis_set_id=analysis.id,
                            from_revision_id=action.id,
                            to_revision_id=api.id,
                            relation_type="observed_during",
                            review_status="candidate",
                            validity_status="valid",
                            causal_basis="temporal_association",
                            evidence_ids=[evidence.id],
                            reason="同一步骤窗口观察到唯一方法/路径/服务声明匹配；轮询也可能同窗，需人工复核，未建立 confirmed_trigger",
                            manual_lock=False,
                        )
                        s.add(relation)
                        s.flush()
                        self.service._verify_relation(s, relation)
                        changed = True
                        state, reason = "candidate", "TEMPORAL_ASSOCIATION_ONLY"
            resolutions.append({"request_id": request_id, "state": state, "reason_code": reason})
        if changed:
            analysis.selection_version += 1
        return [artifact.id], evidence_ids, resolutions

    def finalize_capture(self, s, job, output):
        from .jobs import current_actor

        self.service.require(
            s, current_actor(self.service, job.created_by, job.project_id, session=s), job.project_id, "capture:run"
        )
        capture = s.scalar(select(CaptureRecord).where(CaptureRecord.job_id == job.id))
        if not capture or capture.state == "succeeded":
            raise DomainError("VKS_STATE_CONFLICT", "采集已经完成或不可用", 409)
        approval = s.get(CaptureApproval, capture.approval_id)
        sr = s.get(ScenarioRevision, capture.scenario_revision_id)
        env = s.get(CaptureEnvironment, capture.environment_id)
        data = s.get(CaptureDataset, capture.dataset_id)
        analysis = s.get(Analysis, capture.analysis_set_id)
        selected = s.get(Selection, (analysis.id, sr.scenario_id))
        self._resolve_auth(env.policy)
        expected = digest(
            {
                "scenario": sr.content_sha256,
                "analysis_set_id": analysis.id,
                "selection_version": analysis.selection_version,
                "environment": env.policy_sha256,
                "dataset": data.content_sha256,
            }
        )
        if (
            analysis.state == "frozen"
            or approval.expires_at <= now()
            or not env.enabled
            or not selected
            or selected.entity_revision_id != sr.entity_revision_id
            or approval.manifest_sha256 != expected
        ):
            raise DomainError("VKS_VERSION_CONFLICT", "采集期间素材或授权策略发生变化，结果未保存", 412)
        artifacts = []
        evidence_ids = []
        snapshots = []
        for index, snapshot in enumerate(output.snapshots):
            item = {
                "step_key": snapshot["step_key"],
                "name": snapshot["name"],
                "viewport": snapshot.get("viewport", {"width": 1440, "height": 900}),
                "captured_ms": snapshot.get("captured_ms", 0),
                "dataset_ref": data.key,
                "role_label": sr.dsl.get("role"),
            }
            for kind, extension, media in (("dom", "html", "text/html"), ("png", "png", "image/png")):
                raw = snapshot[kind]
                h = self.service._put_object(raw)
                artifact = Artifact(
                    id=uid(),
                    project_id=job.project_id,
                    snapshot_id=capture.id,
                    relative_path=f"captures/{capture.id}/{index}.{extension}",
                    sha256=h,
                    size_bytes=len(raw),
                    kind="capture",
                    media_type=media,
                    sensitivity="internal",
                    object_key=h,
                    metadata_json={
                        "redaction_version": capture.redaction_version,
                        "capture_id": capture.id,
                        "step_key": snapshot["step_key"],
                    },
                )
                s.add(artifact)
                s.flush()
                artifacts.append(artifact.id)
                item[kind + "_artifact_id"] = artifact.id
                ev = Evidence(
                    id=uid(),
                    project_id=job.project_id,
                    analysis_set_id=capture.analysis_set_id,
                    artifact_id=artifact.id,
                    stable_key=f"capture.{capture.id}.{index}.{kind}",
                    evidence_kind="observed",
                    content_sha256=h,
                    locator={
                        "relative_path": artifact.relative_path,
                        "step_key": snapshot["step_key"],
                        "source_kind": "runtime_capture",
                    },
                    excerpt=raw.decode("utf-8")[:8000] if kind == "dom" else "[已脱敏截图]",
                    claim="在登记测试环境中观察到页面状态；未建立接口运行因果",
                )
                s.add(ev)
                s.flush()
                evidence_ids.append(ev.id)
            snapshots.append(item)
        network_artifacts, network_evidence, network_resolution = self._network_evidence(
            s, capture, analysis, sr, env, output.requests
        )
        artifacts.extend(network_artifacts)
        evidence_ids.extend(network_evidence)
        capture.state = "succeeded"
        capture.result = {
            "snapshots": snapshots,
            "baseline_sha256": self._baseline_digest(s, analysis),
            "environment": {
                "key": env.key,
                "policy_sha256": env.policy_sha256,
                "auth_ref": env.policy.get("auth_ref"),
                "auth_revision": env.policy.get("auth_revision"),
                "viewport": env.policy.get("viewport", {"width": 1440, "height": 900}),
            },
            "steps": output.steps,
            "requests": output.requests,
            "blocked": output.blocked,
            "network_resolution": network_resolution,
            "artifact_ids": artifacts,
            "evidence_ids": evidence_ids,
        }
        return {
            "result_refs": [{"kind": "scenario", "id": sr.scenario_id}],
            "capture_id": capture.id,
            "scenario_revision_id": capture.scenario_revision_id,
            "redaction_version": capture.redaction_version,
            "source_alignment": "unknown",
            "snapshot_count": len(snapshots),
            "issues": (
                [{"code": "NETWORK_POLICY_BLOCKED", "message": "部分越界或未授权请求已阻断"}] if output.blocked else []
            ),
        }

    def captures(self, actor, pid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "trace:read")
            return [
                self._capture_dto(s, c, actor)
                for c in s.scalars(
                    select(CaptureRecord)
                    .where(CaptureRecord.project_id == pid)
                    .order_by(CaptureRecord.created_at.desc())
                )
                if self._capture_visible(s, actor, c)
            ]

    def _capture_visible(self, s, actor, capture):
        if self.service._can(s, actor, capture.project_id, "knowledge:edit"):
            return True
        from .models import Release

        revision = s.get(ScenarioRevision, capture.scenario_revision_id)
        for release in s.scalars(select(Release).where(Release.project_id == capture.project_id)):
            analysis = s.get(Analysis, release.analysis_set_id)
            if capture.result.get("baseline_sha256") != self._baseline_digest(s, analysis):
                continue
            for choice in release.manifest_json["selections"]:
                if choice["entity_revision_id"] == revision.entity_revision_id:
                    return True
                selected = s.get(Revision, choice["entity_revision_id"])
                if not selected or s.get(Entity, selected.entity_id).type != "scenario":
                    continue
                graph = selected.metadata_json.get("dsl", {}).get("explanation_graph", {})
                if any(
                    item.get("captureRef") == capture.id
                    and item.get("scenarioRevisionRef") == revision.id
                    and item.get("evidenceOrigin") == "observed"
                    for item in [*graph.get("states", []), *graph.get("transitions", [])]
                ):
                    return True
        return False

    def _capture_dto(self, s, c, actor):
        job = s.get(Job, c.job_id)
        result = dict(c.result)
        if not self.service._can(s, actor, c.project_id, "source:read"):
            result.pop("network_resolution", None)
        return {
            "id": c.id,
            "scenario_revision_id": c.scenario_revision_id,
            "analysis_set_id": c.analysis_set_id,
            "job_id": c.job_id,
            "state": job.state,
            "redaction_version": c.redaction_version,
            "source_alignment": c.source_alignment,
            "result": result if job.state == "succeeded" else {},
        }


def install_scenarios(app, service, actor, envelope, API="/api/v1"):
    scenarios = ScenarioService(service)
    app.state.scenarios = scenarios

    @app.post(API + "/projects/{pid}/scenarios", status_code=201)
    def create(
        pid: str,
        payload: ScenarioInput,
        request: Request,
        a: Actor = Depends(actor),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, scenarios.create(a, pid, payload.model_dump(), idempotency_key))

    @app.get(API + "/projects/{pid}/scenarios")
    def listing(
        pid: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a: Actor = Depends(actor),
    ):
        return envelope(request, scenarios.list(a, pid, analysis_set_id, release_id))

    @app.put(API + "/scenarios/{sid}/draft")
    def update(
        sid: str, payload: ScenarioInput, request: Request, a: Actor = Depends(actor), if_match: str = Header(...)
    ):
        return envelope(request, scenarios.update(a, sid, payload.model_dump(), if_match))

    @app.get(API + "/scenarios/{sid}/revisions/{rid}")
    def revision(sid: str, rid: str, project_id: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.get(a, project_id, sid, rid))

    @app.post(API + "/projects/{pid}/capture-environments", status_code=201)
    def environment(pid: str, payload: EnvironmentInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.create_environment(a, pid, payload.model_dump()))

    @app.get(API + "/projects/{pid}/capture-environments")
    def environments(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.environments(a, pid))

    @app.post(API + "/projects/{pid}/datasets", status_code=201)
    def dataset(pid: str, payload: DatasetInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.create_dataset(a, pid, payload.model_dump()))

    @app.get(API + "/projects/{pid}/datasets")
    def datasets(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.datasets(a, pid))

    @app.post(API + "/scenarios/{sid}/capture-approvals", status_code=201)
    def approval(sid: str, payload: ApprovalInput, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.approve(a, sid, payload.model_dump()))

    @app.post(API + "/scenarios/{sid}/capture", status_code=202)
    def capture(
        sid: str, payload: CaptureInput, request: Request, a: Actor = Depends(actor), idempotency_key: str = Header(...)
    ):
        return envelope(request, scenarios.request_capture(a, sid, payload.model_dump(), idempotency_key))

    @app.get(API + "/projects/{pid}/captures")
    def captures(pid: str, request: Request, a: Actor = Depends(actor)):
        return envelope(request, scenarios.captures(a, pid))

    @app.get(API + "/captures/{cid}/artifacts/{aid}")
    def capture_artifact(cid: str, aid: str, request: Request, a: Actor = Depends(actor)):
        with service.db() as s:
            c = s.get(CaptureRecord, cid)
            if not c:
                raise DomainError("VKS_NOT_FOUND", "未找到采集", 404)
            service.require(s, a, c.project_id, "trace:read")
            if not scenarios._capture_visible(s, a, c):
                raise DomainError("VKS_NOT_FOUND", "未找到采集", 404)
            art = s.get(Artifact, aid)
            if not art or art.project_id != c.project_id or art.snapshot_id != cid:
                raise DomainError("VKS_NOT_FOUND", "未找到采集制品", 404)
            raw = (service.data_dir / "objects" / art.object_key).read_bytes()
            return Response(
                raw,
                media_type=art.media_type,
                headers={
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

    return scenarios
