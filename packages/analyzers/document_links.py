"""Exact method/path references in fixed Markdown; never semantic or causal proof."""

from hashlib import sha256
import re
from .cancellation import checkpoint


def add_candidates(result, entities, artifacts, evidence_map, texts):
    apis = [e for e in entities.values() if e["type"] == "api" and e["metadata"].get("resolution_state") == "resolved"]
    sections = [e for e in entities.values() if e["type"] == "section"]
    existing = {(r["from_key"], r["to_key"], r["type"]) for r in result.relations}
    references = {}
    for api in apis:
        meta = api["metadata"]
        ident = (meta.get("method"), meta.get("normalized_route"))
        if not all(isinstance(x, str) and x for x in ident):
            continue
        references.setdefault(ident, []).append(api)
    for section in sections:
        checkpoint()
        for (method, path), matches in references.items():
            pattern = r"(?<![A-Za-z])(?i:" + re.escape(method) + r")\s+`?" + re.escape(path) + r"(?![A-Za-z0-9_/{-])"
            if not re.search(pattern, section["content"]):
                continue
            if len({a["metadata"].get("service_key") for a in matches}) > 1:
                result.issues.append(
                    {
                        "code": "VKS_DOCUMENT_API_AMBIGUOUS",
                        "severity": "info",
                        "artifact_path": section["artifact_path"],
                        "message": "文档中的method/path对应多个服务，未自动选取目标；请补充服务范围并人工关联。",
                    }
                )
                continue
            for api in matches:
                if api["metadata"].get("conditions"):
                    result.issues.append(
                        {
                            "code": "VKS_DOCUMENT_API_CONDITIONAL",
                            "severity": "info",
                            "artifact_path": section["artifact_path"],
                            "message": "条件路由需要额外请求约束，普通method/path文本不足以自动绑定。",
                        }
                    )
                    continue
                identity = (api["stable_key"], section["stable_key"], "documents")
                if identity in existing:
                    continue
                keys = []
                for entity, kind in ((section, "markdown"), (api, "declaration")):
                    artifact = artifacts.get(entity["artifact_path"])
                    if not artifact:
                        break
                    key = (
                        "md.api." + sha256((entity["stable_key"] + ":" + artifact["sha256"]).encode()).hexdigest()[:40]
                    )
                    if key in evidence_map and evidence_map[key]["artifact_path"] != entity["artifact_path"]:
                        raise ValueError("Generated evidence key collision")
                    if key not in evidence_map:
                        locator = entity.get("locator", {})
                        span = {
                            "startLine": locator.get("startLine", 1),
                            "endLine": locator.get(
                                "endLine", max(1, len(texts.get(entity["artifact_path"], "").splitlines()))
                            ),
                        }
                        record = {
                            "stable_key": key,
                            "kind": "static",
                            "artifact_path": entity["artifact_path"],
                            "locator": {"kind": "file_lines", **span},
                            "sha256": artifact["sha256"],
                            "claim": "固定材料中的精确" + kind + "引用；不证明业务实现或运行因果。",
                            "content": entity["content"],
                            "extractor_version": "vks-markdown-api-reference-1",
                        }
                        evidence_map[key] = record
                        result.evidence.append(record)
                    keys.append(key)
                if len(keys) != 2:
                    continue
                result.relations.append(
                    {
                        "from_key": identity[0],
                        "to_key": identity[1],
                        "type": "documents",
                        "evidence_keys": keys,
                        "reason": "Markdown出现相同服务范围下的确切HTTP method/path；作为待审核静态引用，不推断真实调用。",
                        "causal_basis": "static_potential",
                    }
                )
                existing.add(identity)
