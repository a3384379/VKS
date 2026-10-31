"""Independent product-design regressions; data is task-local and synthetic."""

from test_journeys import store as store  # pytest fixture registration

from sqlalchemy import select

from vks_api.models import Relation, uid
from vks_api.workbench_v2 import coverage, explanation
from vks_api.onboarding_report import report
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput


def test_action_technical_path_does_not_cross_page_membership_to_sibling_action(store):
    service, actor, pid, workspace, _, _ = store
    aid = workspace["selected_analysis_set"]["id"]
    page = workspace["pages"][0]
    seed = next(row for row in workspace["actions"] if row["stable_key"] == "payments.invoice.open")
    sibling = next(row for row in workspace["actions"] if row["stable_key"] == "payments.records.query")
    with service.db() as db:
        # Existing UI artifacts prove membership; the sibling's API association
        # is independently reviewed, but there is no seed -> sibling/API link.
        for action in (seed, sibling):
            evidence = next(
                row
                for row in workspace["evidence"]
                if row["artifact_id"] == action["artifact_id"] and action["stable_key"] in row["excerpt"]
            )
            db.add(
                Relation(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=aid,
                    from_revision_id=page["entity_revision_id"],
                    to_revision_id=action["entity_revision_id"],
                    relation_type="contains",
                    evidence_ids=[evidence["id"]],
                    reason="Synthetic static page membership",
                    excluded=False,
                    validity_status="valid",
                    causal_basis="not_established",
                )
            )
        for row in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            row.excluded = False
    result = explanation(service, actor, pid, seed["entity_id"], aid)
    forbidden = [path for path in result["paths"] if sibling["entity_revision_id"] in path["revision_ids"]]
    assert not forbidden, "A page's sibling action must not become a technical path of the selected action"


def test_page_membership_alone_does_not_clear_action_knowledge_gaps(store):
    service, actor, pid, workspace, _, _ = store
    aid = workspace["selected_analysis_set"]["id"]
    page = workspace["pages"][0]
    with service.db() as db:
        for row in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            row.excluded = True
        for action in workspace["actions"]:
            evidence = next(
                row
                for row in workspace["evidence"]
                if row["artifact_id"] == action["artifact_id"] and action["stable_key"] in row["excerpt"]
            )
            db.add(
                Relation(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=aid,
                    from_revision_id=page["entity_revision_id"],
                    to_revision_id=action["entity_revision_id"],
                    relation_type="contains",
                    evidence_ids=[evidence["id"]],
                    reason="Synthetic static page membership",
                    excluded=False,
                    validity_status="valid",
                    causal_basis="not_established",
                )
            )
    knowledge = coverage(service, actor, pid, aid)
    onboarding = report(service, actor, pid, aid)
    assert knowledge["actions"]["linked"] == 0, "Page membership proves a UI anchor, not action knowledge coverage"
    gap = next(row for row in onboarding["gaps"] if row["code"] == "ACTION_EVIDENCE_GAP")
    assert gap["count"] == gap["denominator"] == len(workspace["actions"])
    task = KnowledgeTaskService(service).create(
        actor, pid, TaskInput(analysis_set_id=aid, task_kind="evidence_gaps").model_dump(), "membership-gaps"
    )
    service.process_job(task["id"])
    result = KnowledgeTaskService(service).get(actor, task["id"])
    assert {row["revision_id"] for row in result["report"]["uncovered_actions"]} == {
        row["entity_revision_id"] for row in workspace["actions"]
    }


def test_technical_path_follows_api_to_handler_and_never_bridges_shared_document(store):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    action = next(row for row in w["actions"] if row["stable_key"] == "payments.invoice.confirm")
    sibling = next(row for row in w["actions"] if row["stable_key"] == "payments.records.query")
    doc = next(row for row in w["entities"] if row["stable_key"] == "docs.invoice.confirm")
    with service.db() as db:
        for row in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            row.excluded = False
        evidence = next(row for row in w["evidence"] if row["artifact_id"] == sibling["artifact_id"])
        db.add(
            Relation(
                id=uid(),
                project_id=pid,
                analysis_set_id=aid,
                from_revision_id=sibling["entity_revision_id"],
                to_revision_id=doc["entity_revision_id"],
                relation_type="documents",
                evidence_ids=[evidence["id"]],
                reason="Shared documentation context",
                excluded=False,
                validity_status="valid",
                causal_basis="not_established",
            )
        )
    result = explanation(service, actor, pid, action["entity_id"], aid)
    paths = result["paths"]
    assert not any(sibling["entity_revision_id"] in row["revision_ids"] for row in paths)
    kinds = {row["id"]: row["relation_type"] for row in result["relations"]}
    assert any([kinds[key] for key in row["relation_ids"]] == ["may_call", "handler_of", "may_call"] for row in paths)
    assert any(row["revision_ids"][-1] == doc["entity_revision_id"] for row in paths)
    knowledge = coverage(service, actor, pid, aid)
    assert knowledge["actions"]["linked"] == len(w["actions"])
