"""Permission-filtered review items and atomic, exact-version human batches."""

from typing import Any, Literal

from fastapi import Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .documents import DocumentService, DocumentWorkflow
from .journeys import JourneyService
from .models import Analysis, Audit, Entity, Project, Relation, Revision
from .reading import ReadingService, chunks
from .relationships import DETERMINISTIC
from .service import DomainError, canonical, sha
from .versions import UpdateProposal, VersionService
from .v2_contracts import Envelope

Kind = Literal["relation", "journey", "migration", "document"]
Decision = Literal["approve", "reject", "submit", "reopen"]


class ReviewItem(BaseModel):
    kind: Kind
    subject_id: str
    title: str
    status: str
    revision_id: str | None
    version: str
    review_token: str
    actions: list[Decision]
    batch_actions: list[Decision]
    data: dict[str, Any]


class InboxOutput(BaseModel):
    project_id: str
    analysis_set_id: str
    selection_version: int
    scope: str
    items: list[ReviewItem]
    total_visible: int
    counts: dict[str, int]
    offset: int
    limit: int
    next_offset: int | None


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    subject_id: str = Field(min_length=1, max_length=36)
    review_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Decision
    evidence_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    items: list[ReviewAction] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)


class BatchOutput(BaseModel):
    analysis_set_id: str
    selection_version: int
    count: int
    decisions: list[dict[str, str]]


class ReviewInboxService:
    def __init__(self, service):
        self.service = service

    def _items(self, s, actor, analysis):
        pid = analysis.project_id
        reading = ReadingService(self.service)
        choices, _ = reading.selections(s, analysis, None)
        visible = reading.visible_ids(s, actor, pid, choices)
        revisions = set(visible.values())
        evidence = reading.visible_evidence(s, actor, pid, analysis.id)
        items = []
        titles = {}
        for batch in chunks(revisions):
            titles.update(s.execute(select(Revision.id, Revision.name).where(Revision.id.in_(batch))).all())

        def add(kind, subject, title, status, revision, version, data, actions, batch_actions=None):
            token = sha(
                canonical(
                    dict(
                        analysis_set_id=analysis.id,
                        selection_version=analysis.selection_version,
                        kind=kind,
                        subject_id=subject,
                        status=status,
                        revision_id=revision,
                        version=str(version),
                        data=data,
                    )
                )
            )
            items.append(
                dict(
                    kind=kind,
                    subject_id=subject,
                    title=title,
                    status=status,
                    revision_id=revision,
                    version=str(version),
                    review_token=token,
                    actions=actions,
                    batch_actions=actions if batch_actions is None else batch_actions,
                    data=data,
                )
            )

        for relation in s.scalars(
            select(Relation).where(Relation.analysis_set_id == analysis.id).order_by(Relation.id)
        ):
            refs = [eid for eid in relation.evidence_ids if eid in evidence]
            if relation.from_revision_id not in revisions or relation.to_revision_id not in revisions or not refs:
                continue
            data = dict(self.service.relation_dto(relation), evidence_ids=refs)
            status = (
                "rejected"
                if relation.review_status == "rejected"
                else "approved"
                if relation.review_status == "confirmed" and relation.validity_status == "valid"
                else "pending"
            )
            actions = ["approve", "reject", "reopen"]
            batch_actions = ["reject", "reopen"]
            if (
                relation.validity_status == "valid"
                and relation.relation_type in DETERMINISTIC
                and relation.causal_basis != "correlated_runtime"
            ):
                batch_actions.insert(0, "approve")
            add(
                "relation",
                relation.id,
                f"{titles[relation.from_revision_id]} → {titles[relation.to_revision_id]} · {relation.relation_type}",
                status,
                None,
                relation.row_version,
                data,
                actions,
                batch_actions,
            )

        typed = []
        for batch in chunks(revisions):
            typed.extend(
                s.scalars(
                    select(Revision)
                    .join(Entity, Revision.entity_id == Entity.id)
                    .where(Revision.id.in_(batch), Entity.type.in_(["document", "journey"]))
                )
            )
        for row in typed:
            kind = s.get(Entity, row.entity_id).type
            if kind == "journey":
                decision = s.scalar(
                    select(Audit)
                    .where(
                        Audit.project_id == pid,
                        Audit.resource_id == row.id,
                        Audit.action.in_(["journey.approve", "journey.reject"]),
                    )
                    .order_by(Audit.created_at.desc(), Audit.id.desc())
                )
                status = "pending"
                if decision and decision.metadata_json.get("sha256") == row.content_sha256:
                    status = "approved" if decision.action == "journey.approve" else "rejected"
                data = dict(
                    JourneyService(self.service)._dto(s, row), latest_decision_id=decision.id if decision else None
                )
                add(
                    "journey",
                    row.entity_id,
                    row.name,
                    status,
                    row.id,
                    row.id,
                    data,
                    ["reject"] if status == "approved" else ["approve", "reject"],
                )
            else:
                workflow = s.scalar(
                    select(DocumentWorkflow).where(
                        DocumentWorkflow.analysis_set_id == analysis.id, DocumentWorkflow.document_id == row.entity_id
                    )
                )
                data = (
                    DocumentService.workflow_dto(workflow)
                    if workflow
                    else dict(document_id=row.entity_id, revision_id=row.id, state="imported", row_version=1)
                )
                data = dict(
                    data,
                    selected_revision_id=row.id,
                    content_sha256=row.content_sha256,
                    document_row_version=row.metadata_json.get("row_version", 1),
                )
                current = workflow and workflow.revision_id == row.id
                state = workflow.state if current else "imported"
                status = (
                    "approved"
                    if state == "approved"
                    else "rejected"
                    if state == "changes_requested"
                    else "pending"
                    if state == "submitted"
                    else "unsubmitted"
                )
                actions = [] if state == "approved" else ["approve", "reject"] if state == "submitted" else ["submit"]
                version = workflow.row_version if state == "submitted" else row.metadata_json.get("row_version", 1)
                add("document", row.entity_id, row.name, status, row.id, version, data, actions)

        versions = VersionService(self.service)
        for proposal in s.scalars(
            select(UpdateProposal).where(UpdateProposal.analysis_set_id == analysis.id).order_by(UpdateProposal.id)
        ):
            if not versions._visible_proposal(s, actor, proposal):
                continue
            data = versions.proposal_dto(s, proposal)
            status = (
                "pending"
                if proposal.status == "pending"
                else "approved"
                if proposal.status == "accepted"
                else "rejected"
            )
            add(
                "migration",
                proposal.id,
                proposal.kind,
                status,
                proposal.new_revision_id,
                proposal.row_version,
                data,
                ["approve", "reject"] if status == "pending" else [],
            )
        return sorted(items, key=lambda row: (row["kind"], row["subject_id"]))

    def list(self, actor, pid, aid, kind="all", status="pending", offset=0, limit=50):
        reading = ReadingService(self.service)
        with reading.db() as s:
            self.service.require(s, actor, pid, "links:review")
            analysis, _ = reading.baseline(s, actor, pid, aid)
            if analysis.state == "frozen":
                raise DomainError("VKS_STATE_CONFLICT", "发布版没有可变审核收件箱，请选择草稿", 409)
            items = self._items(s, actor, analysis)
            if status != "all":
                items = [
                    row
                    for row in items
                    if row["status"] == status or status == "pending" and row["status"] == "unsubmitted"
                ]
            counts = {
                name: sum(row["kind"] == name for row in items)
                for name in ("relation", "journey", "migration", "document")
            }
            if kind != "all":
                items = [row for row in items if row["kind"] == kind]
            return dict(
                project_id=pid,
                analysis_set_id=aid,
                selection_version=analysis.selection_version,
                scope="当前身份可见、当前草稿修订；计数先过滤权限；批量确认不升级有效性或运行因果",
                items=items[offset : offset + limit],
                total_visible=len(items),
                counts=counts,
                offset=offset,
                limit=limit,
                next_offset=offset + limit if offset + limit < len(items) else None,
            )

    def batch(self, actor, pid, payload, version, key):
        if not actor.human:
            raise DomainError("VKS_FORBIDDEN", "统一审核需要人员会话", 403)
        if not payload["reason"].strip():
            raise DomainError("VKS_INVALID_INPUT", "审核原因不能为空")
        identifiers = [(row["kind"], row["subject_id"]) for row in payload["items"]]
        if len(identifiers) != len(set(identifiers)):
            raise DomainError("VKS_INVALID_INPUT", "同一对象不能在同一批次重复审核")
        with self.service.db() as s:
            self.service.require(s, actor, pid, "links:review")
            s.scalar(select(Project).where(Project.id == pid).with_for_update())
            analysis = s.scalar(
                select(Analysis)
                .where(Analysis.id == payload["analysis_set_id"], Analysis.project_id == pid)
                .with_for_update()
            )
            if not analysis:
                raise DomainError("VKS_NOT_FOUND", "未找到草稿", 404)
            self.service.require(s, actor, pid, "knowledge:edit")
            current = {(row["kind"], row["subject_id"]): row for row in self._items(s, actor, analysis)}
            if any(identifier not in current for identifier in identifiers):
                raise DomainError("VKS_NOT_FOUND", "未找到当前可见审核对象", 404)

            def apply():
                if analysis.state == "frozen":
                    raise DomainError("VKS_STATE_CONFLICT", "发布版不可审核", 409)
                self.service._version(analysis.selection_version, version)
                before_version = analysis.selection_version
                explicit_relations = {subject for kind, subject in identifiers if kind == "relation"}
                if any(
                    current[identifier]["kind"] == "migration"
                    and current[identifier]["data"].get("target_relation_id") in explicit_relations
                    for identifier in identifiers
                ):
                    raise DomainError("VKS_INVALID_INPUT", "同批包含迁移提案及其目标关联，请分开核对；整批未提交")
                for request, identifier in zip(payload["items"], identifiers):
                    row = current[identifier]
                    if request["review_token"] != row["review_token"]:
                        raise DomainError("VKS_VERSION_CONFLICT", "审核对象或人员决定已变化，请刷新后重新核对", 412)
                    decision = request["decision"]
                    if decision not in row["actions"]:
                        raise DomainError("VKS_STATE_CONFLICT", "此对象当前不支持该审核操作", 409)
                    if len(identifiers) > 1 and decision not in row["batch_actions"]:
                        raise DomainError("VKS_INVALID_INPUT", "业务解释、运行因果或失效证据需要逐条核对")
                results = []
                for index, (request, identifier) in enumerate(zip(payload["items"], identifiers)):
                    row, decision = current[identifier], request["decision"]
                    common = dict(analysis_set_id=analysis.id, reason=payload["reason"])
                    if row["kind"] == "relation":
                        refs = request.get("evidence_ids") or row["data"]["evidence_ids"]
                        if set(refs) - set(row["data"]["evidence_ids"]):
                            raise DomainError("VKS_INVALID_EVIDENCE", "审核证据必须来自当前可见关联")
                        self.service._review_relation(
                            s,
                            actor,
                            row["subject_id"],
                            dict(
                                decision="confirm" if decision == "approve" else decision,
                                reason=payload["reason"],
                                evidence_ids=refs,
                            ),
                            row["version"],
                        )
                    elif row["kind"] == "journey":
                        JourneyService(self.service)._review(
                            s,
                            actor,
                            row["subject_id"],
                            dict(common, revision_id=row["revision_id"], decision=decision),
                            row["version"],
                            sha(canonical(dict(batch=key, index=index))),
                        )
                    elif row["kind"] == "document":
                        documents = DocumentService(self.service)
                        value = dict(common, revision_id=row["revision_id"], decision=decision)
                        (documents._submit if decision == "submit" else documents._review)(
                            s, actor, row["subject_id"], value, row["version"]
                        )
                    else:
                        VersionService(self.service)._decide(
                            s,
                            actor,
                            row["subject_id"],
                            dict(common, decision="accept" if decision == "approve" else decision),
                            row["version"],
                        )
                    results.append(dict(kind=row["kind"], subject_id=row["subject_id"], decision=decision))
                analysis.selection_version = max(analysis.selection_version, before_version + 1)
                s.flush()
                return dict(
                    analysis_set_id=analysis.id,
                    selection_version=analysis.selection_version,
                    count=len(results),
                    decisions=results,
                )

            return self.service.idem(
                s, actor, pid, "review_inbox.batch", key, dict(payload, expected_selection_version=version), apply
            )


def install_review_inbox(app, service, actor, envelope):
    reviews = ReviewInboxService(service)

    @app.get("/api/v2/projects/{pid}/review-items", response_model=Envelope[InboxOutput])
    def items(
        pid: str,
        request: Request,
        analysis_set_id: str,
        kind: Literal["all", "relation", "journey", "migration", "document"] = "all",
        status: Literal["all", "pending", "rejected", "approved"] = "pending",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        a=Depends(actor),
    ):
        return envelope(request, reviews.list(a, pid, analysis_set_id, kind, status, offset, limit))

    @app.post("/api/v2/projects/{pid}/review-batches", response_model=Envelope[BatchOutput])
    def batch(
        pid: str,
        payload: BatchInput,
        request: Request,
        if_match: str = Header(...),
        idempotency_key: str = Header(...),
        a=Depends(actor),
    ):
        return envelope(request, reviews.batch(a, pid, payload.model_dump(), if_match, idempotency_key))
