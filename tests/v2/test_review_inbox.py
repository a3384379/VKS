from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_journeys import journey, store
from vks_api.documents import DocumentService, DocumentReview
from vks_api.journeys import JourneyService
from vks_api.models import Analysis, Artifact, Audit, Relation, Review
from vks_api.review_inbox import ReviewInboxService
from vks_api.service import DomainError


def prepared(store):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    created = JourneyService(service).create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "inbox-flow")
    return service, actor, pid, aid, w, created, ReviewInboxService(service)


def action(item, decision, **extra):
    return dict(
        kind=item["kind"], subject_id=item["subject_id"], review_token=item["review_token"], decision=decision, **extra
    )


def row(items, kind):
    return next(item for item in items if item["kind"] == kind)


def test_atomic_mixed_review_is_exact_idempotent_and_keeps_axes_separate(store):
    service, actor, pid, aid, w, created, inbox = prepared(store)
    listing = inbox.list(actor, pid, aid)
    relation = next(
        item for item in listing["items"] if item["kind"] == "relation" and "approve" in item["batch_actions"]
    )
    flow = row(listing["items"], "journey")
    payload = dict(
        analysis_set_id=aid,
        items=[action(relation, "approve"), action(flow, "approve")],
        reason="Checked exact source and declarations",
    )
    result = inbox.batch(actor, pid, payload, listing["selection_version"], "mixed")
    assert result["count"] == 2 and result["selection_version"] > listing["selection_version"]
    assert inbox.batch(actor, pid, payload, listing["selection_version"], "mixed") == result
    with service.db() as s:
        saved = s.get(Relation, relation["subject_id"])
        assert saved.review_status == "confirmed" and saved.causal_basis == relation["data"]["causal_basis"]
        assert saved.validity_status == relation["data"]["validity_status"]
        assert s.scalar(select(func.count()).select_from(Review).where(Review.subject_id == saved.id)) == 1
        assert (
            s.scalar(
                select(func.count())
                .select_from(Audit)
                .where(Audit.resource_id == created["revision_id"], Audit.action == "journey.approve")
            )
            == 1
        )


def test_bad_later_evidence_rolls_back_earlier_human_flow_decision(store):
    service, actor, pid, aid, _, created, inbox = prepared(store)
    listing = inbox.list(actor, pid, aid)
    relation = row(listing["items"], "relation")
    payload = dict(
        analysis_set_id=aid,
        items=[
            action(row(listing["items"], "journey"), "approve"),
            action(relation, "reject", evidence_ids=["unprovided-evidence"]),
        ],
        reason="Must remain atomic",
    )
    with pytest.raises(DomainError) as error:
        inbox.batch(actor, pid, payload, listing["selection_version"], "bad-evidence")
    assert error.value.code == "VKS_INVALID_EVIDENCE"
    with service.db() as s:
        assert not s.scalar(
            select(Audit.id).where(Audit.resource_id == created["revision_id"], Audit.action == "journey.approve")
        )
        assert s.get(Analysis, aid).selection_version == listing["selection_version"]


def test_document_submission_and_review_are_distinct_exact_version_operations(store):
    service, actor, pid, aid, w, _, inbox = prepared(store)
    first = inbox.list(actor, pid, aid, kind="document")
    assert first["items"] and all(item["actions"] == ["submit"] for item in first["items"])
    payload = dict(
        analysis_set_id=aid,
        items=[action(item, "submit") for item in first["items"]],
        reason="Submit imported revisions",
    )
    submitted = inbox.batch(actor, pid, payload, first["selection_version"], "submit-docs")
    listing = inbox.list(actor, pid, aid, kind="document")
    assert all(item["status"] == "pending" and item["actions"] == ["approve", "reject"] for item in listing["items"])
    approved = dict(
        analysis_set_id=aid,
        items=[action(item, "approve") for item in listing["items"]],
        reason="Reviewed frozen bytes",
    )
    result = inbox.batch(actor, pid, approved, submitted["selection_version"], "approve-docs")
    assert result["count"] == len(first["items"])
    assert not inbox.list(actor, pid, aid, kind="document")["items"]
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(DocumentReview)) == 2 * len(first["items"])
    assert all(
        DocumentService(service).workflow(actor, doc["entity_id"], aid)["state"] == "approved" for doc in w["documents"]
    )


def test_same_selection_version_but_changed_document_decision_invalidates_token(store):
    service, actor, pid, aid, _, _, inbox = prepared(store)
    listing = inbox.list(actor, pid, aid, kind="document")
    item = listing["items"][0]
    DocumentService(service).submit(
        actor,
        item["subject_id"],
        dict(analysis_set_id=aid, revision_id=item["revision_id"], reason="Other editor submitted"),
        int(item["version"]),
    )
    with pytest.raises(DomainError) as error:
        inbox.batch(
            actor,
            pid,
            dict(analysis_set_id=aid, items=[action(item, "submit")], reason="Stale UI"),
            listing["selection_version"],
            "stale-token",
        )
    assert error.value.status == 412


def test_current_source_classification_filters_names_counts_and_batch_then_idempotent_replay(store):
    service, actor, pid, aid, w, _, inbox = prepared(store)
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    old = inbox.list(limited, pid, aid, kind="document")
    item = old["items"][0]
    payload = dict(analysis_set_id=aid, items=[action(item, "submit")], reason="Public document")
    inbox.batch(limited, pid, payload, old["selection_version"], "replay-private")
    document = next(doc for doc in w["documents"] if doc["entity_id"] == item["subject_id"])
    with service.db() as s:
        s.get(Artifact, document["artifact_id"]).sensitivity = "confidential"
    current = inbox.list(limited, pid, aid, kind="document")
    assert not current["items"] and current["total_visible"] == 0
    assert document["name"] not in str(current)
    with pytest.raises(DomainError) as error:
        inbox.batch(limited, pid, payload, old["selection_version"], "replay-private")
    assert error.value.status == 404


def test_semantic_or_runtime_relations_still_require_individual_review(store):
    service, actor, pid, aid, _, _, inbox = prepared(store)
    listing = inbox.list(actor, pid, aid)
    semantic = next(
        item for item in listing["items"] if item["kind"] == "relation" and "approve" not in item["batch_actions"]
    )
    flow = row(listing["items"], "journey")
    with pytest.raises(DomainError) as error:
        inbox.batch(
            actor,
            pid,
            dict(
                analysis_set_id=aid,
                items=[action(semantic, "approve"), action(flow, "approve")],
                reason="Bulk cannot invent certainty",
            ),
            listing["selection_version"],
            "semantic-bulk",
        )
    assert error.value.code == "VKS_INVALID_INPUT"
    result = inbox.batch(
        actor,
        pid,
        dict(analysis_set_id=aid, items=[action(semantic, "approve")], reason="Individually checked evidence"),
        listing["selection_version"],
        "semantic-single",
    )
    assert result["count"] == 1


def test_duplicate_cross_project_machine_and_stale_baseline_are_rejected(store):
    service, actor, pid, aid, _, _, inbox = prepared(store)
    listing = inbox.list(actor, pid, aid)
    item = action(row(listing["items"], "journey"), "approve")
    payload = dict(analysis_set_id=aid, items=[item, item], reason="Duplicate")
    with pytest.raises(DomainError):
        inbox.batch(actor, pid, payload, listing["selection_version"], "duplicate")
    payload["items"] = [item]
    with pytest.raises(DomainError) as error:
        inbox.batch(replace(actor, human=False), pid, payload, listing["selection_version"], "machine")
    assert error.value.status == 403
    with pytest.raises(DomainError) as error:
        inbox.batch(actor, pid, payload, listing["selection_version"] - 1, "stale-head")
    assert error.value.status == 412
    other = service.create_project(actor, dict(key="other-review", name="Other"), "other-review")
    with pytest.raises(DomainError) as error:
        inbox.batch(actor, other["id"], payload, listing["selection_version"], "cross-project")
    assert error.value.status == 404


def test_http_pagination_contract_and_atomic_missing_match(store):
    service, actor, pid, aid, _, _, _ = prepared(store)
    app, token = store[4:]
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        url = f"/api/v2/projects/{pid}/review-items"
        first = client.get(url, params=dict(analysis_set_id=aid, limit=1)).json()["data"]
        assert len(first["items"]) == 1 and first["next_offset"] == 1 and first["total_visible"] > 1
        next_page = client.get(url, params=dict(analysis_set_id=aid, limit=1, offset=1)).json()["data"]
        assert first["items"][0]["subject_id"] != next_page["items"][0]["subject_id"]
        payload = dict(analysis_set_id=aid, items=[action(first["items"][0], "submit")], reason="HTTP exact CAS")
        headers = {"X-CSRF-Token": actor.csrf_token, "Idempotency-Key": "http-batch"}
        assert client.post(f"/api/v2/projects/{pid}/review-batches", json=payload, headers=headers).status_code == 422
        headers["If-Match"] = str(first["selection_version"])
        response = client.post(f"/api/v2/projects/{pid}/review-batches", json=payload, headers=headers)
        assert response.status_code == 200, response.text


def migrations(store):
    from test_document_diff import draft, save
    from vks_api.versions import VersionService

    service, actor, pid, release, aid, doc = draft(store)
    save(service, actor, aid, doc, doc["content"] + "\n## Updated by source\nNew reviewed material\n")
    job = service.create_analysis_job(
        actor, aid, dict(kind="detect_drift", from_release_id=release["id"]), "inbox-drift"
    )
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    w = service.workspace(actor, pid, aid)
    flow = JourneyService(service).create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "migration-flow")
    return service, actor, pid, aid, release, doc, flow, ReviewInboxService(service), VersionService(service)


def test_actual_migration_and_journey_mixed_batch_preserves_frozen_history(store):
    service, actor, pid, aid, release, doc, flow, inbox, versions = migrations(store)
    before = deepcopy(DocumentService(service).read_revision(actor, doc["entity_id"], doc["entity_revision_id"]))
    listed = inbox.list(actor, pid, aid, limit=100)
    proposal = next(
        row
        for row in listed["items"]
        if row["kind"] == "migration"
        and row["data"]["kind"] == "entity_change"
        and row["data"]["entity_id"] == doc["entity_id"]
    )
    flow_item = row(listed["items"], "journey")
    outcome = inbox.batch(
        actor,
        pid,
        dict(
            analysis_set_id=aid,
            items=[action(proposal, "approve"), action(flow_item, "approve")],
            reason="Exact source diff and flow reviewed",
        ),
        listed["selection_version"],
        "migration-mixed",
    )
    assert outcome["count"] == 2
    assert (
        next(p for p in versions.list_proposals(actor, pid, aid) if p["id"] == proposal["subject_id"])["status"]
        == "accepted"
    )
    assert DocumentService(service).read_revision(actor, doc["entity_id"], doc["entity_revision_id"]) == before
    assert service.workspace(actor, pid, release_id=release["id"])["selected_release"]["id"] == release["id"]


def test_old_relation_migration_cannot_override_newer_human_decision_or_partially_approve_flow(store):
    service, actor, pid, aid, _, _, flow, inbox, versions = migrations(store)
    proposal = next(p for p in versions.list_proposals(actor, pid, aid) if p["kind"] == "relation_migration")
    relation = next(
        r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == proposal["target_relation_id"]
    )
    service.review_relation(
        actor,
        relation["id"],
        dict(decision="reject", reason="Newer explicit human decision", evidence_ids=relation["evidence_ids"]),
        relation["row_version"],
    )
    before = next(r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == relation["id"])
    listing = inbox.list(actor, pid, aid, limit=100)
    target = next(
        item for item in listing["items"] if item["kind"] == "migration" and item["subject_id"] == proposal["id"]
    )
    with pytest.raises(DomainError) as error:
        inbox.batch(
            actor,
            pid,
            dict(
                analysis_set_id=aid,
                items=[action(row(listing["items"], "journey"), "approve"), action(target, "reject")],
                reason="Stale proposal must roll back the full batch",
            ),
            listing["selection_version"],
            "stale-migration",
        )
    assert error.value.status == 412
    assert next(r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == relation["id"]) == before
    with service.db() as s:
        assert not s.scalar(
            select(Audit.id).where(Audit.action == "journey.approve", Audit.resource_id == flow["revision_id"])
        )
