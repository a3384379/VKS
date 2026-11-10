"""Rebind portable business point declarations to exact bundle-local evidence."""

from hashlib import sha256

from .cancellation import checkpoint
from .vks_ingest import _entity, _fail


def add_points(manifest, path, artifacts, entity_map, add, result):
    proofs = {}

    def evidence(entity, claim):
        art = artifacts[entity["artifact_path"]]
        key = "prototype.import." + sha256(entity["stable_key"].encode()).hexdigest()[:40]
        if key not in proofs:
            proofs[key] = {
                "stable_key": key,
                "kind": "manual",
                "artifact_path": entity["artifact_path"],
                "locator": entity["locator"],
                "sha256": art["sha256"],
                "claim": claim,
                "content": entity["content"],
            }
            result.evidence.append(proofs[key])
        return key

    def relation(source, target, kind, proof, **extra):
        result.relations.append(
            {
                "from_key": source,
                "to_key": target,
                "type": kind,
                "evidence_keys": [proof],
                "causal_basis": "not_established",
                "reason": "按准确清单与来源声明导入",
                **extra,
            }
        )

    points = {}
    for point in manifest["points"]:
        checkpoint()
        key = point["key"]
        sections = []
        for section_key in point["sections"]:
            section = entity_map.get(section_key)
            if not section or section["type"] != "section":
                _fail("VKS_REFERENCE_INVALID", "业务点的原文章节必须属于当前资料包")
            sections.append(section)
        row = _entity(
            key,
            "business_point",
            point["title"],
            point["summary"],
            path,
            {"kind": "file", "relative_path": path},
            {
                **{field: point.get(field, "") for field in ("applicability", "outcome", "exceptions")},
                "prototype_input_paths": sorted({section["artifact_path"] for section in sections}),
            },
        )
        add(row)
        points[key] = row
        for section in sections:
            relation(key, section["stable_key"], "supported_by_section", evidence(section, "准确原文章节"))
    seen = set()
    for binding in manifest["bindings"]:
        checkpoint()
        action_key, point_key = binding["action"], binding["point"]
        action, point = entity_map.get(action_key), points.get(point_key)
        if not action or not point or not action["metadata"].get("prototype_action"):
            _fail("VKS_REFERENCE_INVALID", "业务点绑定的动作或业务点无法定位")
        page = entity_map[action["metadata"]["page_key"]]
        applicable = {
            state["stateKey"]
            for state in page["metadata"]["prototype_manifest"]["states"]
            if any(anchor["actionKey"] == action_key for anchor in state["anchors"])
        }
        if (action_key, point_key) in seen or not set(binding["states"]) <= applicable or not applicable:
            _fail("VKS_REFERENCE_INVALID", "业务点绑定重复或适用状态无法定位")
        seen.add((action_key, point_key))
        relation(
            action_key,
            point_key,
            "has_business_point",
            evidence(point, "维护说明"),
            prototype_scope={"states": sorted(binding["states"]), "priority": binding["priority"]},
        )
