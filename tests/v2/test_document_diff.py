from copy import deepcopy
from dataclasses import replace
from test_journeys import store
from v2_support import published
import pytest
from vks_api.document_diff import DocumentDiffService
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact
from vks_api.service import DomainError
from vks_api.versions import VersionService


def draft(store):
    service, actor, pid, w, app, token = store
    release, w = published(service, actor, {"id": pid})
    cloned = KnowledgeService(service).clone(actor, pid, w["selected_analysis_set"]["id"], "Diff draft", "clone")
    doc = service.workspace(actor, pid, cloned["id"])["documents"][0]
    return service, actor, pid, release, cloned["id"], doc


def save(service, actor, aid, doc, text):
    current = DocumentService(service).read_revision(actor, doc["entity_id"], doc["entity_revision_id"])
    return DocumentService(service).save_draft(
        actor,
        doc["entity_id"],
        dict(analysis_set_id=aid, base_revision_id=current["entity_revision_id"], markdown=text, locked_section_ids=[]),
        current["row_version"],
    )


def test_section_diff_uses_frozen_ordered_mappings_preserves_history_and_denominators(store):
    service, actor, pid, release, aid, doc = draft(store)
    docs = DocumentService(service)
    before = deepcopy(docs.read_revision(actor, doc["entity_id"], doc["entity_revision_id"]))
    saved = save(service, actor, aid, doc, doc["content"] + "\n## New deterministic section\nChanged material\n")
    report = DocumentDiffService(service).get(
        actor, pid, doc["entity_id"], release["id"], aid, saved["entity_revision_id"]
    )
    assert report["before"]["revision_id"] == doc["entity_revision_id"]
    assert report["after"]["revision_id"] == saved["entity_revision_id"]
    assert report["denominator"]["after_sections"] > report["denominator"]["before_sections"]
    assert any("New deterministic section" in row["patch"]["text"] for row in report["sections"])
    assert report["requires_document_review"] and not report["is_truncated"]
    assert docs.read_revision(actor, doc["entity_id"], doc["entity_revision_id"]) == before
    assert report == DocumentDiffService(service).get(
        actor, pid, doc["entity_id"], release["id"], aid, saved["entity_revision_id"]
    )
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    with service.db() as s:
        s.get(Artifact, doc["artifact_id"]).sensitivity = "confidential"
    with pytest.raises(DomainError) as error:
        DocumentDiffService(service).get(limited, pid, doc["entity_id"], release["id"], aid)
    assert error.value.status == 404


def test_outdated_entity_diff_ack_is_rejected_and_newer_relation_decision_is_preserved(store):
    service, actor, pid, release, aid, doc = draft(store)
    first = save(service, actor, aid, doc, doc["content"] + "\n## First change\nA new fact\n")
    job = service.create_analysis_job(actor, aid, {"kind": "detect_drift", "from_release_id": release["id"]}, "detect")
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    versions = VersionService(service)
    proposals = versions.list_proposals(actor, pid, aid)
    proposal = next(p for p in proposals if p["kind"] == "entity_change" and p["entity_id"] == doc["entity_id"])
    updated_doc = service.workspace(actor, pid, aid)["documents"][0]
    save(service, actor, aid, updated_doc, updated_doc["content"] + "\n## Later change\nAnother fact\n")
    with pytest.raises(DomainError) as error:
        versions.decide(actor, proposal["id"], dict(decision="accept", reason="Stale review"), proposal["row_version"])
    assert error.value.status == 412
    with pytest.raises(DomainError):
        DocumentDiffService(service).get(actor, pid, doc["entity_id"], release["id"], aid, first["entity_revision_id"])
    migration = next(p for p in proposals if p["kind"] == "relation_migration")
    relation = next(
        r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == migration["target_relation_id"]
    )
    service.review_relation(
        actor,
        relation["id"],
        dict(decision="reject", reason="A newer human decision", evidence_ids=relation["evidence_ids"]),
        relation["row_version"],
    )
    before = next(r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == relation["id"])
    with pytest.raises(DomainError) as error:
        versions.decide(
            actor, migration["id"], dict(decision="reject", reason="Outdated proposal"), migration["row_version"]
        )
    assert error.value.status == 412
    assert next(r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == relation["id"]) == before
