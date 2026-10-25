"""Human relation correction and atomic batch review; proposals never self-approve."""

from typing import Literal
from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, JSON, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Identified, Analysis, Relation, Revision, Evidence, Review, uid


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


class BatchInput(Input):
    analysis_set_id: str
    relation_ids: list[str] = Field(min_length=1, max_length=100)
    expected_versions: dict[str, int] = Field(min_length=1, max_length=100)
    decision: Literal["confirm", "reject"]
    reason: str = Field(min_length=1, max_length=2000)


DETERMINISTIC = {"contains", "renders", "handler_of", "implements", "maps_to_sql", "reads_table", "writes_table"}


def fail(code, message, status=422):
    from .service import DomainError

    raise DomainError(code, message, status)


class RelationshipService:
    def __init__(self, service):
        self.service = service

    def _visible(self, s, actor, relation):
        pid = relation.project_id
        if not all(
            self.service._visible_revision(s, actor, pid, s.get(Revision, x))
            for x in (relation.from_revision_id, relation.to_revision_id)
        ) or not all(
            (e := s.get(Evidence, x)) and self.service._visible_evidence(s, actor, pid, e)
            for x in relation.evidence_ids
        ):
            fail("VKS_NOT_FOUND", "未找到关系", 404)

    def _review(self, s, actor, r, decision, reason):
        s.add(
            Review(
                id=uid(),
                project_id=r.project_id,
                subject_id=r.id,
                actor_id=actor.id,
                decision=decision,
                reason=reason,
                base_version=r.row_version,
                evidence_ids=r.evidence_ids,
            )
        )
        r.review_status = "confirmed" if decision == "confirm" else "rejected"
        r.manual_lock = decision == "confirm"
        r.row_version += 1
        self.service.audit(
            s, actor, r.project_id, "relation.review", r.id, metadata={"decision": decision, "reason": reason}
        )

    def batch(self, actor, pid, payload):
        if not actor.human:
            fail("VKS_FORBIDDEN", "批量审核需要人员会话", 403)
        with self.service.db() as s:
            self.service.require(s, actor, pid, "links:review")
            analysis = s.scalar(select(Analysis).where(Analysis.id == payload["analysis_set_id"]).with_for_update())
            if not analysis or analysis.project_id != pid:
                fail("VKS_NOT_FOUND", "未找到分析集合", 404)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "发布版不可审核", 409)
            ids = payload["relation_ids"]
            if len(ids) != len(set(ids)):
                fail("VKS_INVALID_INPUT", "批量审核不允许重复关系")
            relations = []
            for rid in sorted(ids):
                r = s.scalar(select(Relation).where(Relation.id == rid).with_for_update())
                if not r or r.analysis_set_id != analysis.id:
                    fail("VKS_NOT_FOUND", "未找到同基线关系", 404)
                self._visible(s, actor, r)
                self.service._version(r.row_version, payload["expected_versions"].get(rid))
                self.service._verify_relation(s, r)
                if r.validity_status != "valid":
                    fail("VKS_STATE_CONFLICT", "请先逐条复核失效证据", 409)
                relations.append(r)
            if len({r.relation_type for r in relations}) != 1:
                fail("VKS_INVALID_INPUT", "只能批量审核同类关系")
            if payload["decision"] == "confirm" and (
                relations[0].relation_type not in DETERMINISTIC
                or any(r.causal_basis == "correlated_runtime" for r in relations)
            ):
                fail("VKS_INVALID_INPUT", "业务解释或运行因果需逐条人员审核")
            for r in relations:
                self._review(s, actor, r, payload["decision"], payload["reason"])
            analysis.selection_version += 1
            return {"relations": [self.service.relation_dto(r) for r in relations], "count": len(relations)}

    def correct(self, actor, rid, payload, version):
        if not actor.human:
            fail("VKS_FORBIDDEN", "纠错需要人员会话", 403)
        with self.service.db() as s:
            r = s.scalar(select(Relation).where(Relation.id == rid).with_for_update())
            if not r:
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            self.service.require(s, actor, r.project_id)
            self._visible(s, actor, r)
            self.service._version(r.row_version, version)
            if r.analysis_set_id != payload["analysis_set_id"]:
                fail("VKS_INVALID_INPUT", "纠错必须固定相同基线")
            analysis = s.scalar(select(Analysis).where(Analysis.id == r.analysis_set_id).with_for_update())
            target_id = payload.get("correct_target_revision_id")
            if target_id:
                revision = s.get(Revision, target_id)
                if (
                    not revision
                    or not self.service._visible_revision(s, actor, r.project_id, revision)
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
            self.service.require(s, actor, r.project_id, "links:review")
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "发布版纠错请先创建草稿", 409)
            operation = payload["operation"]
            targets = payload.get("targets", [])
            if (operation in ("retarget", "merge") and len(targets) != 1) or (
                operation == "split" and len(targets) < 2
            ):
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
            for target in targets:
                revision = s.get(Revision, target["to_revision_id"])
                if not revision or not self.service._visible_revision(s, actor, r.project_id, revision):
                    fail("VKS_NOT_FOUND", "未找到目标", 404)
                candidate = Relation(
                    id=uid(),
                    project_id=r.project_id,
                    analysis_set_id=analysis.id,
                    from_revision_id=r.from_revision_id,
                    to_revision_id=revision.id,
                    relation_type=r.relation_type,
                    review_status="candidate",
                    validity_status="valid",
                    # A changed endpoint has not been established by the old
                    # parser/capture, even when that original edge was static.
                    causal_basis="not_established",
                    evidence_ids=target["evidence_ids"],
                    reason=payload["reason"],
                    row_version=1,
                )
                self.service._verify_relation(s, candidate)
                self._visible(s, actor, candidate)
                new.append(candidate)
            for original in originals:
                self._review(s, actor, original, "reject", payload["reason"])
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
                    s.add(
                        Review(
                            id=uid(),
                            project_id=r.project_id,
                            subject_id=existing.id,
                            actor_id=actor.id,
                            decision="reopen",
                            reason=payload["reason"],
                            base_version=existing.row_version,
                            evidence_ids=candidate.evidence_ids,
                        )
                    )
                    existing.review_status = "candidate"
                    existing.manual_lock = False
                    existing.validity_status = "valid"
                    existing.evidence_ids = candidate.evidence_ids
                    existing.causal_basis = candidate.causal_basis
                    existing.reason = candidate.reason
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

    @app.post(API + "/projects/{pid}/relations/batch-review")
    def batch(pid: str, payload: BatchInput, request: Request, a=Depends(actor)):
        return envelope(request, relations.batch(a, pid, payload.model_dump()))

    @app.post(API + "/relations/{rid}/corrections")
    def correct(rid: str, payload: CorrectionInput, request: Request, a=Depends(actor), if_match: str = Header(...)):
        return envelope(request, relations.correct(a, rid, payload.model_dump(), if_match))
