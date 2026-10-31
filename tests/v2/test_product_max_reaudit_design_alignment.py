"""Independent second-pass regressions across every action explanation entry."""

from test_journeys import store as store  # pytest fixture registration

from sqlalchemy import select
from fastapi.testclient import TestClient

from vks_api.models import Relation, uid
from vks_api.reading import ReadingService


def test_workbench_api_context_does_not_bridge_shared_document(store):
    service, actor, pid, workspace, _, _ = store
    aid = workspace["selected_analysis_set"]["id"]
    action = next(row for row in workspace["actions"] if row["stable_key"] == "payments.invoice.open")
    document = next(row for row in workspace["entities"] if row["stable_key"] == "docs.invoice.open")
    unrelated_api = next(row for row in workspace["entities"] if row["type"] == "api")
    evidence = next(row for row in workspace["evidence"] if row["artifact_id"] == document["artifact_id"])
    with service.db() as db:
        for row in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            row.excluded = False
        db.add(
            Relation(
                id=uid(),
                project_id=pid,
                analysis_set_id=aid,
                from_revision_id=unrelated_api["entity_revision_id"],
                to_revision_id=document["entity_revision_id"],
                relation_type="documents",
                evidence_ids=[evidence["id"]],
                reason="This independently documented API shares the same reference section",
                excluded=False,
                validity_status="valid",
                causal_basis="not_established",
            )
        )
    context = ReadingService(service).action_context(actor, pid, action["entity_id"], aid)
    assert not context["api_endpoints"], (
        "The six-tab Workbench must not treat a shared document as an action-to-API link"
    )
    assert document["entity_revision_id"] in {row["entity_revision_id"] for row in context["entities"]}


def test_reverse_api_lookup_obeys_handler_direction_and_cannot_cross_shared_documents(store):
    service, actor, pid, workspace, app, token = store
    aid = workspace["selected_analysis_set"]["id"]
    action = next(row for row in workspace["actions"] if row["stable_key"] == "payments.records.query")
    sibling = next(row for row in workspace["actions"] if row["stable_key"] == "payments.invoice.open")
    document = next(row for row in workspace["entities"] if row["stable_key"] == "docs.invoice.open")
    api = next(row for row in workspace["entities"] if row["type"] == "api" and "records" in row["name"])
    handler_relation = next(
        row
        for row in workspace["relations"]
        if row["relation_type"] == "handler_of" and row["to_revision_id"] == api["entity_revision_id"]
    )
    handler = next(
        row for row in workspace["entities"] if row["entity_revision_id"] == handler_relation["from_revision_id"]
    )
    evidence = next(row for row in workspace["evidence"] if row["artifact_id"] == document["artifact_id"])
    with service.db() as db:
        for row in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            row.excluded = False
        db.add(
            Relation(
                id=uid(),
                project_id=pid,
                analysis_set_id=aid,
                from_revision_id=api["entity_revision_id"],
                to_revision_id=document["entity_revision_id"],
                relation_type="documents",
                evidence_ids=[evidence["id"]],
                reason="Shared documentation context only",
                excluded=False,
                validity_status="valid",
                causal_basis="not_established",
            )
        )
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        response = client.get(
            f"/api/v2/entities/{handler['entity_id']}/business-references",
            params={"project_id": pid, "analysis_set_id": aid},
        )
        assert response.status_code == 200, response.text
        ids = {row["entity_id"] for row in response.json()["data"]["actions"]}
    assert action["entity_id"] in ids, "A handler must reverse-resolve its API's declared action"
    assert sibling["entity_id"] not in ids, "Shared documentation must not reverse-bridge unrelated actions"
