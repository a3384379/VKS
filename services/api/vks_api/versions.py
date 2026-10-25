"""Version comparison, evidence-preserving migration proposals, and revalidation."""

from __future__ import annotations

import json
from collections import deque
from hashlib import sha256
from typing import ClassVar, Literal

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .documents import body_fingerprint
from .models import (
    Analysis,
    Artifact,
    Base,
    Entity,
    Evidence,
    Identified,
    Relation,
    Release,
    Review,
    Revision,
    now,
    uid,
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(code, message, status=422, details=None):
    from .service import DomainError

    raise DomainError(code, message, status, details)


class UpdateProposal(Identified, Base):
    __tablename__ = "update_proposal"
    __table_args__ = (UniqueConstraint("analysis_set_id", "fingerprint"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), index=True)
    from_release_id: Mapped[str] = mapped_column(ForeignKey("knowledge_release.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entity.id"), nullable=True)
    source_relation_id: Mapped[str | None] = mapped_column(ForeignKey("relation.id"), nullable=True)
    target_relation_id: Mapped[str | None] = mapped_column(ForeignKey("relation.id"), nullable=True)
    old_revision_id: Mapped[str | None] = mapped_column(ForeignKey("entity_revision.id"), nullable=True)
    new_revision_id: Mapped[str | None] = mapped_column(ForeignKey("entity_revision.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    __mapper_args__: ClassVar[dict] = {"version_id_col": row_version}


class VersionService:
    def __init__(self, service):
        self.service = service

    def _baselines(self, s, pid, release_id, analysis_id):
        old, new = s.get(Release, release_id), s.get(Analysis, analysis_id)
        if not old or not new or old.project_id != pid or new.project_id != pid:
            fail("VKS_NOT_FOUND", "未找到版本基线", 404)
        return old, new

    def compare(self, s, pid, from_release_id, to_analysis_set_id):
        old, new = self._baselines(s, pid, from_release_id, to_analysis_set_id)
        before = {x["entity_id"]: s.get(Revision, x["entity_revision_id"]) for x in old.manifest_json["selections"]}
        after = {r.entity_id: r for r in self.service._revisions(s, new.id)}
        changes = []
        for eid in sorted(set(before) | set(after)):
            a, b = before.get(eid), after.get(eid)
            if a and b and a.id == b.id:
                continue
            row = {"entity_id": eid, "change_kind": "added" if not a else "removed" if not b else "modified"}
            if a:
                row["old_revision_id"] = a.id
            if b:
                row["new_revision_id"] = b.id
            if a and b:
                same_body = (
                    a.semantic_content_sha256 == b.semantic_content_sha256
                    or s.get(Entity, eid).type == "section"
                    and body_fingerprint(a) == body_fingerprint(b)
                )
                if a.name != b.name and same_body:
                    row["change_kind"] = "renamed"
                elif same_body and (
                    a.locator != b.locator
                    or a.artifact_id
                    and b.artifact_id
                    and s.get(Artifact, a.artifact_id).relative_path != s.get(Artifact, b.artifact_id).relative_path
                ):
                    row["change_kind"] = "moved"
            changes.append(row)
        changed = {c["entity_id"] for c in changes}
        relations = list(s.scalars(select(Relation).where(Relation.analysis_set_id == new.id)))
        impacted = [
            self.service.relation_dto(r)
            for r in relations
            if any(s.get(Revision, rid).entity_id in changed for rid in (r.from_revision_id, r.to_revision_id))
        ]
        return {
            "from_release_id": from_release_id,
            "to_analysis_set_id": to_analysis_set_id,
            "changes": changes,
            "impacted_relations": impacted,
            "impact_paths": self.impact_paths(s, changed, relations),
            "unknowns": ["材料或定位变化不等于业务语义已改变；重新确认不产生运行因果证据"],
        }

    def impact_paths(self, s, seeds, relations, depth=3, max_nodes=200):
        adjacency = {}
        for relation in relations:
            source = s.get(Revision, relation.from_revision_id).entity_id
            target = s.get(Revision, relation.to_revision_id).entity_id
            adjacency.setdefault(source, []).append((target, relation.id))
            adjacency.setdefault(target, []).append((source, relation.id))
        output = []
        for seed in sorted(seeds):
            queue = deque([(seed, [seed], [])])
            seen = {seed}
            while queue and len(seen) <= max_nodes:
                current, nodes, edges = queue.popleft()
                if len(edges) >= depth:
                    continue
                for target, relation in adjacency.get(current, []):
                    if target in seen or len(seen) >= max_nodes:
                        continue
                    seen.add(target)
                    path = [*nodes, target]
                    links = [*edges, relation]
                    output.append(
                        {
                            "changed_entity_id": seed,
                            "impacted_entity_id": target,
                            "entity_ids": path,
                            "relation_ids": links,
                            "depth": len(links),
                        }
                    )
                    queue.append((target, path, links))
        return output

    def _copy_evidence(self, s, analysis, ids):
        output = []
        for eid in ids:
            evidence = s.get(Evidence, eid)
            if not evidence or evidence.project_id != analysis.project_id:
                continue
            key = "drift-copy." + eid
            existing = s.scalar(
                select(Evidence).where(Evidence.analysis_set_id == analysis.id, Evidence.stable_key == key)
            )
            if not existing:
                existing = Evidence(
                    id=uid(),
                    project_id=analysis.project_id,
                    analysis_set_id=analysis.id,
                    artifact_id=evidence.artifact_id,
                    stable_key=key,
                    evidence_kind=evidence.evidence_kind,
                    content_sha256=evidence.content_sha256,
                    locator=evidence.locator,
                    excerpt=evidence.excerpt,
                    claim=evidence.claim,
                )
                s.add(existing)
                s.flush()
            output.append(existing.id)
        return output

    def detect_drift(self, s, pid, from_release_id, to_analysis_set_id, actor_id=None):
        old, new = self._baselines(s, pid, from_release_id, to_analysis_set_id)
        if new.state == "frozen":
            fail("VKS_STATE_CONFLICT", "只能向未发布草稿生成更新提案", 409)
        s.refresh(new, with_for_update=True)
        after = {r.entity_id: r for r in self.service._revisions(s, new.id)}
        changed = False
        proposals = []
        historical_relations = list(old.manifest_json["relations"])
        published_ids = {row["id"] for row in historical_relations}
        # Rejected decisions are intentionally absent from the public release
        # graph, but remain part of the frozen baseline's review history.
        historical_relations.extend(
            self.service.relation_dto(relation)
            for relation in s.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == old.analysis_set_id, Relation.review_status == "rejected"
                )
            )
            if relation.id not in published_ids
        )
        for old_relation in historical_relations:
            old_source = s.get(Revision, old_relation["from_revision_id"])
            old_target = s.get(Revision, old_relation["to_revision_id"])
            source, target = after.get(old_source.entity_id), after.get(old_target.entity_id)
            if source and target and source.id == old_source.id and target.id == old_target.id:
                continue
            source_id = source.id if source else old_source.id
            target_id = target.id if target else old_target.id
            fingerprint = sha256(
                canonical([old.id, old_relation["id"], source_id, target_id, old_relation["relation_type"]]).encode()
            ).hexdigest()
            proposal = s.scalar(
                select(UpdateProposal).where(
                    UpdateProposal.analysis_set_id == new.id, UpdateProposal.fingerprint == fingerprint
                )
            )
            if proposal:
                proposals.append(proposal)
                continue
            current = s.scalar(
                select(Relation).where(
                    Relation.analysis_set_id == new.id,
                    Relation.from_revision_id == source_id,
                    Relation.to_revision_id == target_id,
                    Relation.relation_type == old_relation["relation_type"],
                )
            )
            if not current:
                current = s.scalar(
                    select(Relation).where(
                        Relation.analysis_set_id == new.id,
                        Relation.from_revision_id == old_source.id,
                        Relation.to_revision_id == old_target.id,
                        Relation.relation_type == old_relation["relation_type"],
                    )
                )
            old_evidence = self._copy_evidence(s, new, old_relation["evidence_ids"])
            original = s.get(Relation, old_relation["id"])
            prior_lock = original.manual_lock if original else bool(old_relation.get("manual_lock", False))
            if not current:
                # A missing endpoint is retained as an explicit historical
                # reference in this new draft, never patched into a guessed ID.
                current = Relation(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=new.id,
                    from_revision_id=source_id,
                    to_revision_id=target_id,
                    relation_type=old_relation["relation_type"],
                    review_status=old_relation["review_status"],
                    validity_status="broken" if not source or not target else "stale",
                    causal_basis="not_established",
                    evidence_ids=old_evidence,
                    reason=old_relation.get("reason", ""),
                    row_version=1,
                    manual_lock=prior_lock,
                )
                s.add(current)
                s.flush()
            else:
                # Do not overwrite a newer manual decision in the target draft.
                reviewed = s.scalar(select(Review).where(Review.subject_id == current.id))
                current.from_revision_id = source_id
                current.to_revision_id = target_id
                current.validity_status = "broken" if not source or not target else "stale"
                if current.causal_basis == "correlated_runtime":
                    current.causal_basis = "not_established"
                if not reviewed:
                    current.review_status = old_relation["review_status"]
                    current.manual_lock = prior_lock
                    current.evidence_ids = list(dict.fromkeys([*current.evidence_ids, *old_evidence]))
                current.row_version += 1
            proposal = UpdateProposal(
                id=uid(),
                project_id=pid,
                analysis_set_id=new.id,
                from_release_id=old.id,
                fingerprint=fingerprint,
                kind="relation_migration",
                source_relation_id=old_relation["id"],
                target_relation_id=current.id,
                old_revision_id=old_target.id,
                new_revision_id=target.id if target else None,
                metadata_json={
                    "from_entity_id": old_source.entity_id,
                    "to_entity_id": old_target.entity_id,
                    "source_revision_id": old_source.id,
                    "target_revision_id": old_target.id,
                    "new_source_revision_id": source.id if source else None,
                    "new_target_revision_id": target.id if target else None,
                    "previous_causal_basis": old_relation["causal_basis"],
                    "boundary": "旧审核结论保留；证据变化必须单独复核有效性",
                },
            )
            s.add(proposal)
            s.flush()
            proposals.append(proposal)
            changed = True
        report = self.compare(s, pid, old.id, new.id)
        for change in report["changes"]:
            fingerprint = sha256(canonical([old.id, "entity", change]).encode()).hexdigest()
            proposal = s.scalar(
                select(UpdateProposal).where(
                    UpdateProposal.analysis_set_id == new.id, UpdateProposal.fingerprint == fingerprint
                )
            )
            if not proposal:
                proposal = UpdateProposal(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=new.id,
                    from_release_id=old.id,
                    fingerprint=fingerprint,
                    kind="entity_change",
                    entity_id=change["entity_id"],
                    old_revision_id=change.get("old_revision_id"),
                    new_revision_id=change.get("new_revision_id"),
                    metadata_json=change,
                )
                s.add(proposal)
                s.flush()
                changed = True
            proposals.append(proposal)
        if changed:
            new.selection_version += 1
            self.service.audit(
                s,
                actor_id or new.created_by,
                pid,
                "version.detect_drift",
                new.id,
                metadata={"from_release_id": old.id, "proposal_count": len(proposals)},
            )
        report["proposal_ids"] = [p.id for p in proposals]
        return report

    def _visible_proposal(self, s, actor, proposal):
        ids = {
            rid
            for rid in (
                proposal.old_revision_id,
                proposal.new_revision_id,
                *[
                    proposal.metadata_json.get(k)
                    for k in (
                        "source_revision_id",
                        "target_revision_id",
                        "new_source_revision_id",
                        "new_target_revision_id",
                    )
                ],
            )
            if rid
        }
        visible = bool(ids) and all(
            (rev := s.get(Revision, rid)) and self.service._visible_revision(s, actor, proposal.project_id, rev)
            for rid in ids
        )
        relation = s.get(Relation, proposal.target_relation_id) if proposal.target_relation_id else None
        return visible and (
            not relation
            or all(
                (evidence := s.get(Evidence, eid))
                and self.service._visible_evidence(s, actor, proposal.project_id, evidence)
                for eid in relation.evidence_ids
            )
        )

    def proposal_dto(self, s, proposal):
        relation = s.get(Relation, proposal.target_relation_id) if proposal.target_relation_id else None
        return {
            "id": proposal.id,
            "analysis_set_id": proposal.analysis_set_id,
            "from_release_id": proposal.from_release_id,
            "kind": proposal.kind,
            "status": proposal.status,
            "row_version": proposal.row_version,
            "entity_id": proposal.entity_id,
            "old_revision_id": proposal.old_revision_id,
            "new_revision_id": proposal.new_revision_id,
            "source_relation_id": proposal.source_relation_id,
            "target_relation_id": proposal.target_relation_id,
            "relation": self.service.relation_dto(relation) if relation else None,
            "details": proposal.metadata_json,
            "reason": proposal.reason,
            "decided_by": proposal.decided_by,
            "updated_at": proposal.updated_at,
        }

    def list_proposals(self, actor, pid, aid):
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:edit")
            analysis = s.get(Analysis, aid)
            if not analysis or analysis.project_id != pid:
                fail("VKS_NOT_FOUND", "未找到分析集合", 404)
            return [
                self.proposal_dto(s, p)
                for p in s.scalars(
                    select(UpdateProposal)
                    .where(UpdateProposal.analysis_set_id == aid)
                    .order_by(UpdateProposal.created_at, UpdateProposal.id)
                )
                if self._visible_proposal(s, actor, p)
            ]

    def public_report(self, actor, pid, result):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            self.service._selected(s, actor, pid, result["to_analysis_set_id"], None)
            report = self.service._filter_diff(
                s, actor, pid, {k: v for k, v in result.items() if k not in ("impact_paths", "proposal_ids")}
            )
            old, new = self._baselines(s, pid, result["from_release_id"], result["to_analysis_set_id"])
            selected_ids = {row["entity_revision_id"] for row in old.manifest_json["selections"]}
            selected_ids.update(r.id for r in self.service._revisions(s, new.id))
            visible_relations = {row["id"] for row in report["impacted_relations"]}
            # Impact traversal can pass through unchanged relations too. Filter
            # every edge and both baseline revisions before returning a path.
            for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == new.id)):
                if all(
                    (revision := s.get(Revision, rid)) and self.service._visible_revision(s, actor, pid, revision)
                    for rid in (relation.from_revision_id, relation.to_revision_id)
                ) and all(
                    (evidence := s.get(Evidence, eid)) and self.service._visible_evidence(s, actor, pid, evidence)
                    for eid in relation.evidence_ids
                ):
                    visible_relations.add(relation.id)
            visible = set()
            for path in result.get("impact_paths", []):
                revisions = list(
                    s.scalars(
                        select(Revision).where(
                            Revision.entity_id.in_(path["entity_ids"]), Revision.id.in_(selected_ids)
                        )
                    )
                )
                per_entity = {eid: [] for eid in path["entity_ids"]}
                for revision in revisions:
                    per_entity[revision.entity_id].append(revision)
                if all(
                    rows and all(self.service._visible_revision(s, actor, pid, r) for r in rows)
                    for rows in per_entity.values()
                ) and all(rid in visible_relations for rid in path["relation_ids"]):
                    visible.add(canonical(path))
            report["impact_paths"] = [p for p in result.get("impact_paths", []) if canonical(p) in visible]
            report["proposal_ids"] = [
                identifier
                for identifier in result.get("proposal_ids", [])
                if (p := s.get(UpdateProposal, identifier))
                and p.project_id == pid
                and self._visible_proposal(s, actor, p)
            ]
            return report

    def decide(self, actor, proposal_id, payload, version):
        with self.service.db() as s:
            proposal = s.get(UpdateProposal, proposal_id)
            if not proposal:
                fail("VKS_NOT_FOUND", "未找到更新提案", 404)
            self.service.require(s, actor, proposal.project_id, "links:review")
            if not actor.human:
                fail("VKS_FORBIDDEN", "更新提案审核需要人员会话", 403)
            if not self._visible_proposal(s, actor, proposal):
                fail("VKS_NOT_FOUND", "未找到更新提案", 404)
            self.service._version(proposal.row_version, version)
            analysis = s.get(Analysis, proposal.analysis_set_id)
            s.refresh(analysis, with_for_update=True)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布提案不可修改", 409)
            if proposal.status != "pending":
                fail("VKS_STATE_CONFLICT", "提案已处理", 409)
            relation = s.get(Relation, proposal.target_relation_id) if proposal.target_relation_id else None
            if relation and payload["decision"] == "reject":
                relation.review_status = "rejected"
                relation.validity_status = "obsolete"
                relation.row_version += 1
                s.add(
                    Review(
                        project_id=proposal.project_id,
                        subject_id=relation.id,
                        actor_id=actor.id,
                        decision="reject",
                        reason=payload["reason"],
                        base_version=relation.row_version - 1,
                        evidence_ids=relation.evidence_ids,
                    )
                )
            proposal.status = "accepted" if payload["decision"] == "accept" else "rejected"
            proposal.reason = payload["reason"]
            proposal.decided_by = actor.id
            proposal.updated_at = now()
            proposal.row_version += 1
            analysis.selection_version += 1
            self.service.audit(
                s,
                actor,
                proposal.project_id,
                "version.proposal_review",
                proposal.id,
                metadata={"decision": payload["decision"]},
            )
            s.flush()
            return self.proposal_dto(s, proposal)

    def revalidate(self, actor, rid, payload, version):
        with self.service.db() as s:
            relation = s.get(Relation, rid)
            if not relation:
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            self.service.require(s, actor, relation.project_id, "links:review")
            if not actor.human:
                fail("VKS_FORBIDDEN", "有效性复核需要人员会话", 403)
            if relation.analysis_set_id != payload["analysis_set_id"]:
                fail("VKS_NOT_FOUND", "未找到当前基线关系", 404)
            analysis = s.get(Analysis, relation.analysis_set_id)
            s.refresh(analysis, with_for_update=True)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布关系不可原地复核", 409)
            self.service._version(relation.row_version, version)
            selected = {r.id: r for r in self.service._revisions(s, analysis.id)}
            endpoints = [
                selected.get(identifier) for identifier in (relation.from_revision_id, relation.to_revision_id)
            ]
            if not all(endpoints):
                fail("VKS_INVALID_EVIDENCE", "断链端点尚未恢复，不得以复核清除")
            if not all(self.service._visible_revision(s, actor, analysis.project_id, r) for r in endpoints):
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            snapshots = {row["source_snapshot_id"] for row in analysis.sources}
            evidence = []
            for identifier in payload["evidence_ids"]:
                ev = s.get(Evidence, identifier)
                artifact = s.get(Artifact, ev.artifact_id) if ev else None
                if (
                    not ev
                    or ev.project_id != analysis.project_id
                    or ev.analysis_set_id != analysis.id
                    or not artifact
                    or artifact.snapshot_id not in snapshots
                    or not self.service._visible_evidence(s, actor, analysis.project_id, ev)
                ):
                    fail("VKS_INVALID_EVIDENCE", "复核证据不属于当前基线与可访问来源")
                evidence.append(ev)
            if not {r.artifact_id for r in endpoints if r.artifact_id}.issubset({e.artifact_id for e in evidence}):
                fail("VKS_INVALID_EVIDENCE", "复核证据未覆盖当前两个端点的来源版本")
            relation.evidence_ids = list(dict.fromkeys(payload["evidence_ids"]))
            relation.validity_status = "valid"
            relation.row_version += 1
            # Evidence validity and human review remain independent axes.
            self.service._verify_relation(s, relation)
            s.add(
                Review(
                    project_id=analysis.project_id,
                    subject_id=relation.id,
                    actor_id=actor.id,
                    decision="revalidate",
                    reason=payload["reason"],
                    base_version=relation.row_version - 1,
                    evidence_ids=relation.evidence_ids,
                )
            )
            analysis.selection_version += 1
            self.service.audit(s, actor, analysis.project_id, "relation.revalidate", relation.id)
            s.flush()
            return self.service.relation_dto(relation)

    @staticmethod
    def assert_publishable(s, analysis):
        blockers = []
        for proposal in s.scalars(
            select(UpdateProposal).where(
                UpdateProposal.analysis_set_id == analysis.id,
                UpdateProposal.kind == "relation_migration",
                UpdateProposal.status == "pending",
            )
        ):
            relation = s.get(Relation, proposal.target_relation_id)
            if relation and relation.review_status == "confirmed" and relation.validity_status != "valid":
                blockers.append("更新迁移提案尚未复核：" + proposal.id)
        return blockers


class ProposalReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=2000)


class RevalidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)


def install_versions(app, service, actor, envelope, API="/api/v1"):
    versions = VersionService(service)
    actor_dependency = Depends(actor)

    @app.get(API + "/projects/{pid}/change-proposals")
    def proposals(pid: str, analysis_set_id: str, request: Request, a=actor_dependency):
        return envelope(request, versions.list_proposals(a, pid, analysis_set_id))

    @app.post(API + "/change-proposals/{proposal_id}/review")
    def review(
        proposal_id: str,
        payload: ProposalReviewInput,
        request: Request,
        a=actor_dependency,
        if_match: str = Header(...),
    ):
        return envelope(request, versions.decide(a, proposal_id, payload.model_dump(), if_match))

    @app.post(API + "/relations/{rid}/revalidate")
    def revalidate(
        rid: str, payload: RevalidateInput, request: Request, a=actor_dependency, if_match: str = Header(...)
    ):
        return envelope(request, versions.revalidate(a, rid, payload.model_dump(), if_match))
