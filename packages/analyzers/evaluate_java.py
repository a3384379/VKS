"""Evaluate frozen, public synthetic labels without exposing answers to analysis."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from .java_spring import analyze_sources, canonical

ROOT = Path(__file__).resolve().parents[2]


def signature(row):
    return tuple(row[k] for k in ("from_service", "from", "type", "to_service", "to"))


def evaluate(fixture_root: Path | None = None) -> dict:
    root = fixture_root or ROOT / "fixtures/java-gold"
    gold_bytes = (root / "gold.json").read_bytes()
    labels = json.loads(gold_bytes)
    artifacts = []
    for relative, expected in labels["fixture_sha256"].items():
        path = root / "v1" / relative
        content = path.read_bytes()
        actual = sha256(content).hexdigest()
        if actual != expected:
            raise ValueError("Frozen Java fixture fingerprint mismatch: " + relative)
        artifacts.append(
            {
                "path": relative,
                "kind": "source",
                "source_id": relative.split("/")[0],
                "content": content,
                "sha256": actual,
            }
        )
    sources = [{"source_id": s, "service_key": s} for s in sorted({a["source_id"] for a in artifacts})]
    # Gold labels are not passed to analyze_sources or any model.
    analysis = analyze_sources(artifacts, sources)
    entities = {e["stable_key"]: e for e in analysis.entities}
    actual = set()
    for relation in analysis.relations:
        source, target = (entities[relation[key]] for key in ("from_key", "to_key"))
        actual.add(
            (
                source["metadata"]["service_key"],
                source["display_name"],
                relation["type"],
                target["metadata"]["service_key"],
                target["display_name"],
            )
        )
    expected = {signature(row) for row in labels["expected_relations"]}
    tp, fp, fn = actual & expected, actual - expected, expected - actual
    kinds = sorted({row[2] for row in actual | expected})

    def counts(actual_set, expected_set):
        t, f, n = len(actual_set & expected_set), len(actual_set - expected_set), len(expected_set - actual_set)
        return {
            "tp": t,
            "fp": f,
            "fn": n,
            "precision": t / (t + f) if t + f else None,
            "recall": t / (t + n) if t + n else None,
        }

    return {
        "fixture": labels["fixture"],
        "gold_sha256": sha256(gold_bytes).hexdigest(),
        "independently_human_reviewed": False,
        "relation_denominator": len(expected),
        "candidate_count": len(actual),
        "confirmed_count": 0,
        **counts(actual, expected),
        "by_relation_type": {
            kind: counts({x for x in actual if x[2] == kind}, {x for x in expected if x[2] == kind}) for kind in kinds
        },
        "missing_relations": [list(row) for row in sorted(fn)],
        "unexpected_relations": [list(row) for row in sorted(fp)],
        "analysis_metrics": analysis.metrics,
        "resolution_states": dict(Counter(i["resolution_state"] for i in analysis.issues)),
        "call_resolution_coverage": analysis.metrics["resolved_calls"] / analysis.metrics["calls"]
        if analysis.metrics["calls"]
        else None,
        "limitations": labels["limitations"],
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
