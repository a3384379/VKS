"""User relation corrections and atomic operations with version checks."""

from typing import Literal

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Analysis, Base, Evidence, Identified, Relation, Revision, uid


class RelationshipFeedback(Identified, Base):
    __tablename__ = "relationship_feedback"
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"))
    relation_id: Mapped[str] = mapped_column(ForeignKey("relation.id"))
    actor_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text)
    correct_target_revision_id: Mapped[str | None] = mapped_column(ForeignKey("entity_revision.id"), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelationStateInput(Input):
    excluded: bool


class Target(Input):
    to_revision_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class CorrectionInput(Input):
    analysis_set_id: str
    operation: Literal["retarget", "split", "merge", "feedback"]
    reason: str = Field(min_length=1, max_length=2000)
    targets: list[Target] = Field(default_factory=list, max_length=20)
    merge_relation_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_versions: dict[str, int] = Field(default_factory=dict, max_length=20)
    correct_target_revision_id: str | None = None


DETERMINISTIC = {"contains", "renders", "handler_of", "implements", "maps_to_sql", "reads_table", "writes_table"}


def fail(code, message, status=422):
    from .service import DomainError

    raise DomainError(code, message, status)


class RelationshipService:
    def __init__(self, service):
        self.service = service

    def _visible(self, s, actor, relation):
        pid = relation.project_id
        if (
            not self.service._visible_relation(s, actor, pid, relation)
            or not all(
                self.service._visible_revision(s, actor, pid, s.get(Revision, x), relation.analysis_set_id)
                for x in (relation.from_revision_id, relation.to_revision_id)
            )
            or not all(
                (e := s.get(Evidence, x)) and self.service._visible_evidence(s, actor, pid, e)
                for x in relation.evidence_ids
            )
        ):
            fail("VKS_NOT_FOUND", "未找到关系", 404)

    def _lock_for_actor(self, s, actor, rid, scope):
        relation = s.get(Relation, rid)
        if not relation:
            fail("VKS_NOT_FOUND", "未找到关系", 404)
        self.service.require(s, actor, relation.project_id, scope)
        s.refresh(relation, with_for_update=True)
        self._visible(s, actor, relation)
        return relation

    def set_state(self, actor, rid, payload, version):
        with self.service.db() as s:
            relation = self._lock_for_actor(s, actor, rid, "knowledge:edit")
            analysis = s.get(Analysis, relation.analysis_set_id)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "固定版本不可修改，请创建草稿", 409)
            self.service._version(relation.row_version, version)
            if not payload["excluded"]:
                self.service._verify_relation(s, relation)
            relation.excluded = payload["excluded"]
            relation.manual_lock = True
            relation.row_version += 1
            analysis.selection_version += 1
            self.service.audit(
                s, actor, relation.project_id, "relation.exclude" if payload["excluded"] else "relation.restore", rid
            )
            return {"relation": self.service.relation_dto(relation)}

    def correct(self, actor, rid, payload, version):
        with self.service.db() as s:
            return self._correct(s, actor, rid, payload, version)

    def _correct(self, s, actor, rid, payload, version):
        """Share the caller's transaction, including proposal resolution and policy."""
        r = self._lock_for_actor(s, actor, rid, "knowledge:read")
        self.service._version(r.row_version, version)
        if r.analysis_set_id != payload["analysis_set_id"]:
            fail("VKS_INVALID_INPUT", "纠错必须固定相同基线")
        analysis = s.scalar(select(Analysis).where(Analysis.id == r.analysis_set_id).with_for_update())
        target_id = payload.get("correct_target_revision_id")
        if target_id:
            revision = s.get(Revision, target_id)
            if (
                not revision
                or not self.service._visible_revision(s, actor, r.project_id, revision, analysis.id)
                or revision.id not in {x.id for x in self.service._revisions(s, analysis.id)}
            ):
                fail("VKS_NOT_FOUND", "未找到当前基线目标", 404)
        if payload["operation"] == "feedback":
            feedback = RelationshipFeedback(
                id=uid(),
                project_id=r.project_id,
                analysis_set_id=analysis.id,
                relation_id=r.id,
                actor_id=actor.id,
                reason=payload["reason"],
                correct_target_revision_id=target_id,
                details={"base_version": r.row_version, "evaluation_only": True},
            )
            s.add(feedback)
            self.service.audit(s, actor, r.project_id, "relation.feedback", feedback.id)
            return {
                "feedback_id": feedback.id,
                "original_relations": [self.service.relation_dto(r)],
                "new_relations": [],
            }
        self.service.require(s, actor, r.project_id, "knowledge:edit")
        if analysis.state == "frozen":
            fail("VKS_STATE_CONFLICT", "发布版纠错请先创建草稿", 409)
        if r.relation_type in ("has_business_point", "supported_by_section"):
            fail("VKS_STATE_CONFLICT", "请在页面与动作维护中更新业务点或重新定位原文", 409)
        operation = payload["operation"]
        targets = payload.get("targets", [])
        if (operation in ("retarget", "merge") and len(targets) != 1) or (operation == "split" and len(targets) < 2):
            fail("VKS_INVALID_INPUT", "改选/合并需1个目标，拆分需2–20个目标")
        if len({t["to_revision_id"] for t in targets}) != len(targets):
            fail("VKS_INVALID_INPUT", "目标不可重复")
        originals = [r]
        if operation == "merge":
            ids = set(payload["merge_relation_ids"]) | {rid}
            if not 2 <= len(ids) <= 20:
                fail("VKS_INVALID_INPUT", "合并需要2–20条关系")
            for other_id in sorted(ids - {rid}):
                other = s.scalar(select(Relation).where(Relation.id == other_id).with_for_update())
                if (
                    not other
                    or other.analysis_set_id != analysis.id
                    or other.from_revision_id != r.from_revision_id
                    or other.relation_type != r.relation_type
                ):
                    fail("VKS_INVALID_INPUT", "合并关系必须具有相同基线、来源和类型")
                self._visible(s, actor, other)
                self.service._version(other.row_version, payload["expected_versions"].get(other_id))
                originals.append(other)
        elif payload.get("merge_relation_ids"):
            fail("VKS_INVALID_INPUT", "仅合并操作接受其他关系")
        new = []
        provenance = dict(r.metadata_json or {})
        provenance_ids = {
            aid
            for original in originals
            for aid in (original.metadata_json or {}).get("model_provenance_artifact_ids", [])
        }
        if provenance_ids or any(
            (original.metadata_json or {}).get("generation_kind") == "model"
            or original.reason.startswith("AI 语义候选：")
            for original in originals
        ):
            provenance.update(generation_kind="model", model_provenance_artifact_ids=sorted(provenance_ids))
        for target in targets:
            revision = s.get(Revision, target["to_revision_id"])
            if not revision or not self.service._visible_revision(s, actor, r.project_id, revision, analysis.id):
                fail("VKS_NOT_FOUND", "未找到目标", 404)
            candidate = Relation(
                id=uid(),
                project_id=r.project_id,
                analysis_set_id=analysis.id,
                from_revision_id=r.from_revision_id,
                to_revision_id=revision.id,
                relation_type=r.relation_type,
                excluded=False,
                validity_status="valid",
                # A changed endpoint has not been established by the old
                # parser/capture, even when that original edge was static.
                causal_basis="not_established",
                evidence_ids=target["evidence_ids"],
                reason=payload["reason"],
                metadata_json=provenance,
                row_version=1,
            )
            self.service._verify_relation(s, candidate)
            self._visible(s, actor, candidate)
            new.append(candidate)
        for original in originals:
            original.excluded = True
            original.manual_lock = True
            original.row_version += 1
        resolved = []
        for candidate in new:
            existing = s.scalar(
                select(Relation).where(
                    Relation.analysis_set_id == analysis.id,
                    Relation.from_revision_id == candidate.from_revision_id,
                    Relation.to_revision_id == candidate.to_revision_id,
                    Relation.relation_type == candidate.relation_type,
                )
            )
            if existing:
                if existing.id not in {x.id for x in originals}:
                    fail("VKS_STATE_CONFLICT", "目标已存在关系，请选中该关系合并", 409)
                existing.excluded = False
                existing.manual_lock = False
                existing.validity_status = "valid"
                existing.evidence_ids = candidate.evidence_ids
                existing.causal_basis = candidate.causal_basis
                existing.reason = candidate.reason
                existing.metadata_json = candidate.metadata_json
                existing.row_version += 1
                resolved.append(existing)
            else:
                s.add(candidate)
                resolved.append(candidate)
        new = resolved
        analysis.selection_version += 1
        self.service.audit(
            s,
            actor,
            r.project_id,
            "relation." + operation,
            r.id,
            metadata={"new_relation_ids": [x.id for x in new]},
        )
        return {
            "original_relations": [self.service.relation_dto(x) for x in originals],
            "new_relations": [self.service.relation_dto(x) for x in new],
        }


def install_relationships(app, service, actor, envelope, API="/api/v1"):
    relations = RelationshipService(service)

    @app.patch(API + "/relations/{rid}")
    def state(rid: str, payload: RelationStateInput, request: Request, a=Depends(actor), if_match: str = Header(...)):
        return envelope(request, relations.set_state(a, rid, payload.model_dump(), if_match)["relation"])

    @app.post(API + "/relations/{rid}/corrections")
    def correct(rid: str, payload: CorrectionInput, request: Request, a=Depends(actor), if_match: str = Header(...)):
        return envelope(request, relations.correct(a, rid, payload.model_dump(), if_match))
