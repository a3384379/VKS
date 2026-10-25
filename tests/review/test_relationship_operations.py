import pytest
from sqlalchemy import select
from vks_api.service import Service, DomainError
from vks_api.models import Relation, Review
from vks_api.relationships import RelationshipService, RelationshipFeedback


@pytest.fixture
def system(tmp_path):
    service = Service(tmp_path)
    _, actor = service.login(service.bootstrap_token)
    p = service.create_project(actor, {"key": "charge-demo", "name": "纠错测试"}, "p")
    job = service.demo_import(actor, p["id"], "demo")
    service.process_job(job["id"])
    w = service.workspace(actor, p["id"])
    return service, actor, p, w, RelationshipService(service)


def test_batch_atomic_versions_type_and_independent_review_records(system):
    s, a, p, w, ops = system
    links = [r for r in w["relations"] if r["relation_type"] == "contains"]
    assert len(links) >= 2
    payload = {
        "analysis_set_id": w["selected_analysis_set"]["id"],
        "relation_ids": [r["id"] for r in links],
        "expected_versions": {r["id"]: r["row_version"] for r in links},
        "decision": "confirm",
        "reason": "核对声明结构",
    }
    payload["expected_versions"][links[-1]["id"]] = 0
    with pytest.raises(DomainError):
        ops.batch(a, p["id"], payload)
    assert all(r["review_status"] == "candidate" for r in s.workspace(a, p["id"])["relations"])
    payload["expected_versions"][links[-1]["id"]] = links[-1]["row_version"]
    result = ops.batch(a, p["id"], payload)
    assert result["count"] == len(links) and all(r["review_status"] == "confirmed" for r in result["relations"])
    with s.db() as db:
        assert len(list(db.scalars(select(Review)))) == len(links)


@pytest.mark.parametrize("source_type", ["action", "api"])
def test_retarget_keeps_rejected_history_and_new_candidate_never_auto_confirms(system, source_type):
    s, a, p, w, ops = system
    entities = {e["entity_revision_id"]: e for e in w["entities"]}
    link = next(
        r
        for r in w["relations"]
        if r["relation_type"] == "documents" and entities[r["from_revision_id"]]["type"] == source_type
    )
    previous_targets = {
        r["to_revision_id"]
        for r in w["relations"]
        if r["from_revision_id"] == link["from_revision_id"] and r["relation_type"] == "documents"
    }
    section = next(
        e for e in w["entities"] if e["type"] == "section" and e["entity_revision_id"] not in previous_targets
    )
    result = ops.correct(
        a,
        link["id"],
        {
            "analysis_set_id": link["analysis_set_id"],
            "operation": "retarget",
            "reason": "改选准确章节",
            "targets": [{"to_revision_id": section["entity_revision_id"], "evidence_ids": link["evidence_ids"]}],
        },
        link["row_version"],
    )
    assert result["original_relations"][0]["review_status"] == "rejected"
    assert result["new_relations"][0]["review_status"] == "candidate"
    assert result["new_relations"][0]["causal_basis"] == "not_established"
    with s.db() as db:
        assert db.get(Relation, link["id"]).review_status == "rejected"
        assert db.scalar(select(Review).where(Review.subject_id == link["id"])).reason == "改选准确章节"


def test_split_and_merge_reuses_identical_logical_edge_with_review_history(system):
    s, a, p, w, ops = system
    link = next(r for r in w["relations"] if r["relation_type"] == "documents")
    sections = [e for e in w["entities"] if e["type"] == "section"]
    other = next(e for e in sections if e["entity_revision_id"] != link["to_revision_id"])
    result = ops.correct(
        a,
        link["id"],
        {
            "analysis_set_id": link["analysis_set_id"],
            "operation": "split",
            "reason": "两段分别说明",
            "targets": [
                {"to_revision_id": x, "evidence_ids": link["evidence_ids"]}
                for x in (link["to_revision_id"], other["entity_revision_id"])
            ],
        },
        link["row_version"],
    )
    assert len(result["new_relations"]) == 2
    same = next(x for x in result["new_relations"] if x["id"] == link["id"])
    assert same["review_status"] == "candidate"
    merged = ops.correct(
        a,
        same["id"],
        {
            "analysis_set_id": link["analysis_set_id"],
            "operation": "merge",
            "reason": "合并说明",
            "merge_relation_ids": [r["id"] for r in result["new_relations"]],
            "expected_versions": {r["id"]: r["row_version"] for r in result["new_relations"]},
            "targets": [{"to_revision_id": link["to_revision_id"], "evidence_ids": link["evidence_ids"]}],
        },
        same["row_version"],
    )
    assert len(merged["new_relations"]) == 1 and merged["new_relations"][0]["review_status"] == "candidate"
    with s.db() as db:
        assert len(list(db.scalars(select(Review)))) >= 5


def test_feedback_records_eval_correction_without_mutation_and_machine_cannot_review(system):
    s, a, p, w, ops = system
    link = w["relations"][0]
    result = ops.correct(
        a,
        link["id"],
        {"analysis_set_id": link["analysis_set_id"], "operation": "feedback", "reason": "需要复核", "targets": []},
        link["row_version"],
    )
    with s.db() as db:
        assert db.get(RelationshipFeedback, result["feedback_id"]).details["evaluation_only"]
        assert db.get(Relation, link["id"]).row_version == link["row_version"]
    token = s.create_token(
        a, p["id"], {"name": "proposal", "scopes": ["knowledge:read", "links:propose"], "expires_in_days": 1}
    )
    machine = s.authenticate(token["token"], "token")
    with pytest.raises(DomainError) as e:
        ops.correct(
            machine,
            link["id"],
            {"analysis_set_id": link["analysis_set_id"], "operation": "feedback", "reason": "machine", "targets": []},
            link["row_version"],
        )
    assert e.value.status == 403
