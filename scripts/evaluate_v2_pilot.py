"""Evaluate supplied pilot records against a separately frozen Gold.

This computes thresholds, not human independence, business authorization or release
approval. Keep real pilot files in private .vks/ storage, outside Git and images.
"""

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import median


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate(gold, run):
    for field in ("project_id", "release_id", "manifest_sha256"):
        if not gold.get(field) or gold[field] != run.get(field):
            raise ValueError("Gold and measurements must identify the exact same frozen project/release/manifest")
    frozen = datetime.fromisoformat(gold["frozen_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
    if frozen.utcoffset() is None or started.utcoffset() is None or frozen > started:
        raise ValueError("Gold must be frozen before the measured run with explicit timezone")
    actions = gold["action_ids"]
    if len(actions) != 30 or len(set(actions)) != 30:
        raise ValueError("Gold must contain exactly 30 distinct actions")
    expected_relations = set(gold["relation_keys"])
    expected_impacts = set(gold["impact_entity_ids"])
    if len(expected_relations) != len(gold["relation_keys"]) or len(expected_impacts) != len(gold["impact_entity_ids"]):
        raise ValueError("Duplicate Gold entries cannot inflate denominators")
    observed = run["actions"]
    if len({row["action_id"] for row in observed}) != len(observed) or {row["action_id"] for row in observed} - set(
        actions
    ):
        raise ValueError("Measured action identities must belong to Gold and be unique")
    relations = run["relations"]
    if len({row["key"] for row in relations}) != len(relations):
        raise ValueError("Duplicate candidate relationships")
    predicted_relations = {row["key"] for row in relations}
    confirmed = [row for row in relations if row["review_status"] == "confirmed" and row["validity_status"] == "valid"]
    traceable = set()
    for row in confirmed:
        refs = row.get("evidence", [])
        if refs and all(
            all(ref.get(k) == gold[k] for k in ("project_id", "release_id"))
            and ref.get("artifact_id")
            and len(ref.get("artifact_sha256", "")) == 64
            and all(c in "0123456789abcdef" for c in ref["artifact_sha256"])
            and ref.get("locator")
            and ref.get("kind") in ("static", "manual", "observed", "derived")
            for ref in refs
        ):
            traceable.add(row["key"])
    confirmed_keys = {row["key"] for row in confirmed}
    explored = {
        row["action_id"]
        for row in observed
        if row.get("has_page_or_step") is True and set(row.get("relation_keys", [])) & confirmed_keys
    }
    impacts = set(run["impact_entity_ids"])
    users, pairs = Counter(), set()
    successes, paired_before, paired_after = 0, [], []
    for row in run["user_tasks"]:
        pair = (row["participant_id"], row["task_id"])
        if pair in pairs:
            raise ValueError("Duplicate participant/task pairs")
        pairs.add(pair)
        if row.get("independent") is not True or row.get("unassisted") is not True:
            continue
        before, after = row.get("baseline_seconds"), row.get("vks_seconds")
        if any(type(value) not in (int, float) or not 0 < value < 86400 for value in (before, after)):
            raise ValueError("Every user task needs finite positive paired measurements")
        if type(row.get("success")) is not bool:
            raise ValueError("Success must be measured explicitly")
        users[row["participant_id"]] += 1
        successes += int(row["success"])
        paired_before.append(before)
        paired_after.append(after)
    enough_users = len(users) >= 5 and all(count >= 5 for count in users.values())
    improvement = 1 - median(paired_after) / median(paired_before) if paired_before else None
    precision = ratio(len(predicted_relations & expected_relations), len(predicted_relations))
    recall = ratio(len(impacts & expected_impacts), len(expected_impacts))
    user_rate = ratio(successes, sum(users.values()))
    metrics = dict(
        action_explorability=dict(numerator=len(explored), denominator=30, value=len(explored) / 30, threshold=0.85),
        confirmed_traceability=dict(
            numerator=len(traceable),
            denominator=len(confirmed),
            value=ratio(len(traceable), len(confirmed)),
            threshold=1.0,
        ),
        candidate_precision=dict(
            numerator=len(predicted_relations & expected_relations),
            denominator=len(predicted_relations),
            value=precision,
            threshold=0.9,
            false_positives=len(predicted_relations - expected_relations),
            gold_coverage=ratio(len(predicted_relations & expected_relations), len(expected_relations)),
        ),
        impact_recall=dict(
            numerator=len(impacts & expected_impacts),
            denominator=len(expected_impacts),
            value=recall,
            threshold=0.9,
            false_positives=len(impacts - expected_impacts),
            missed=len(expected_impacts - impacts),
        ),
        independent_task_success=dict(
            numerator=successes,
            denominator=sum(users.values()),
            value=user_rate if enough_users else None,
            threshold=0.8,
            participants=len(users),
            sufficient_sample=enough_users,
        ),
        paired_time_improvement=dict(
            value=improvement if enough_users else None, threshold=0.3, paired_tasks=len(paired_before)
        ),
    )
    for item in metrics.values():
        item["status"] = (
            "not_evaluated"
            if item["value"] is None
            else "pass"
            if item["value"] + 1e-12 >= item["threshold"]
            else "fail"
        )
    branches = set(run.get("recorded_branch_kinds", []))
    gates = dict(
        frozen_independent_gold=gold.get("independently_reviewed") is True,
        three_recorded_branches={"success", "validation_failure", "permission_denied"} <= branches,
        forbidden_side_effects=run.get("forbidden_side_effects") == 0,
        history_consistent=run.get("history_consistent") is True,
    )
    statuses = {item["status"] for item in metrics.values()}
    status = (
        "reported_fail"
        if "fail" in statuses or not all(gates.values())
        else "not_evaluated"
        if "not_evaluated" in statuses
        else "reported_pass"
    )
    return dict(
        status=status,
        evidence_level="supplied_records_only",
        material_kind=gold.get("material_kind", "unknown"),
        real_business_acceptance=False,
        requires_independent_attestation=True,
        metrics=metrics,
        gates=gates,
        unresolved=run.get("unresolved", []),
        denominator_scope="exact frozen 30 actions and supplied paired records; zero denominator is not evaluated",
    )


def read(path):
    raw = Path(path).read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Acceptance input over quota")
    return raw, json.loads(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--expected-gold-sha256", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raw, gold = read(args.gold)
    if sha256(raw).hexdigest() != args.expected_gold_sha256:
        parser.error("Gold SHA-256 differs from the independently recorded digest")
    run_raw, measurement = read(args.run)
    result = evaluate(gold, measurement)
    result.update(gold_sha256=sha256(raw).hexdigest(), run_sha256=sha256(run_raw).hexdigest())
    destination = Path(args.output).absolute()
    if ".vks" not in destination.parts:
        parser.error("Keep private acceptance output under .vks/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps({"status": result["status"], "evidence_level": result["evidence_level"]}))
