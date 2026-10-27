from copy import deepcopy
import pytest
from scripts.evaluate_v2_pilot import evaluate


def fixture():
    gold = dict(
        project_id="synthetic",
        release_id="frozen",
        manifest_sha256="a" * 64,
        frozen_at="2026-10-01T00:00:00Z",
        material_kind="synthetic",
        independently_reviewed=True,
        action_ids=[f"a{i}" for i in range(30)],
        relation_keys=[f"r{i}" for i in range(30)],
        impact_entity_ids=["changed"],
    )
    ref = dict(
        project_id="synthetic",
        release_id="frozen",
        artifact_id="artifact",
        artifact_sha256="b" * 64,
        locator=dict(startLine=1),
        kind="static",
    )
    run = dict(
        project_id="synthetic",
        release_id="frozen",
        manifest_sha256="a" * 64,
        started_at="2026-10-02T00:00:00Z",
        actions=[dict(action_id=f"a{i}", has_page_or_step=True, relation_keys=[f"r{i}"]) for i in range(30)],
        relations=[
            dict(key=f"r{i}", review_status="confirmed", validity_status="valid", evidence=[deepcopy(ref)])
            for i in range(30)
        ],
        impact_entity_ids=["changed"],
        user_tasks=[
            dict(
                participant_id=f"p{p}",
                task_id=f"t{t}",
                independent=True,
                unassisted=True,
                baseline_seconds=100,
                vks_seconds=60,
                success=True,
            )
            for p in range(5)
            for t in range(5)
        ],
        recorded_branch_kinds=["success", "validation_failure", "permission_denied"],
        forbidden_side_effects=0,
        history_consistent=True,
    )
    return gold, run


def test_paired_thresholds_do_not_certify_real_business_or_independence():
    gold, run = fixture()
    value = evaluate(gold, run)
    assert value["status"] == "reported_pass" and not value["real_business_acceptance"]
    assert value["metrics"]["paired_time_improvement"]["value"] == 0.4
    assert value["metrics"]["independent_task_success"]["denominator"] == 25


def test_candidates_are_not_confirmed_and_no_gold_denominator_becomes_zero_percent_or_pass():
    gold, run = fixture()
    run["relations"] = []
    run["user_tasks"] = []
    gold["impact_entity_ids"] = []
    value = evaluate(gold, run)
    assert value["metrics"]["action_explorability"]["value"] == 0
    assert value["metrics"]["candidate_precision"]["status"] == "not_evaluated"
    assert value["metrics"]["impact_recall"]["status"] == "not_evaluated"
    assert value["metrics"]["independent_task_success"]["status"] == "not_evaluated"


def test_wrong_frozen_identity_or_duplicate_user_cannot_inflate_evidence():
    gold, run = fixture()
    run["release_id"] = "different"
    with pytest.raises(ValueError):
        evaluate(gold, run)
    gold, run = fixture()
    run["user_tasks"].append(run["user_tasks"][0])
    with pytest.raises(ValueError):
        evaluate(gold, run)


def test_evidence_from_wrong_release_and_false_positive_candidates_count_as_failure():
    gold, run = fixture()
    for item in run["relations"]:
        item["evidence"][0]["release_id"] = "wrong"
    run["relations"].extend(
        dict(key=f"wrong{i}", review_status="candidate", validity_status="valid", evidence=[]) for i in range(5)
    )
    value = evaluate(gold, run)
    assert value["metrics"]["confirmed_traceability"]["value"] == 0
    assert value["metrics"]["candidate_precision"]["false_positives"] == 5
    assert value["status"] == "reported_fail"
