from packages.analyzers.vks_ingest import parse_bundle
from packages.collector.snapshots import Snapshot, bundle_bytes
import json


def bundle(two_services=False):
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1"},
        "paths": {"/items": {"get": {"operationId": "listItems", "responses": {"200": {"description": "fixture"}}}}},
    }
    files = {
        "docs/rules.md": b"# Rules\nGET /items is a declared read operation.\n",
        "api/spec.json": json.dumps(spec, indent=2).encode(),
    }
    return parse_bundle(
        bundle_bytes(Snapshot("local", "fixed", files), "doc-links", "v1", role="documentation"), "doc-links"
    )


def test_exact_markdown_api_reference_creates_only_grounded_static_candidate():
    parsed = bundle()
    link = next(r for r in parsed.relations if r["type"] == "documents")
    entities = {e["stable_key"]: e for e in parsed.entities}
    assert entities[link["from_key"]]["type"] == "api" and entities[link["to_key"]]["type"] == "section"
    assert link["causal_basis"] == "static_potential"
    evidence = {e["stable_key"]: e for e in parsed.evidence}
    assert len(link["evidence_keys"]) == 2
    assert {evidence[k]["artifact_path"] for k in link["evidence_keys"]} == {"docs/rules.md", "api/spec.json"}
    for key in link["evidence_keys"]:
        ev = evidence[key]
        artifact = next(a for a in parsed.artifacts if a["path"] == ev["artifact_path"])
        assert ev["sha256"] == artifact["sha256"] and ev["locator"]["endLine"] <= len(artifact["content"].splitlines())


def test_exact_path_boundary_and_cross_service_ambiguity_never_choose_first():
    from copy import deepcopy
    from packages.analyzers.document_links import add_candidates

    parsed = bundle()
    api = next(e for e in parsed.entities if e["type"] == "api")
    section = next(e for e in parsed.entities if e["type"] == "section")
    duplicate = deepcopy(api)
    duplicate["stable_key"] = "api.other-service"
    duplicate["metadata"]["service_key"] = "other"
    parsed.relations = []
    parsed.evidence = []
    parsed.issues = []
    entities = {e["stable_key"]: e for e in (api, duplicate, section)}
    artifacts = {a["path"]: a for a in parsed.artifacts}
    texts = {a["path"]: a["content"].decode() for a in parsed.artifacts}
    add_candidates(parsed, entities, artifacts, {}, texts)
    assert not parsed.relations and any(i["code"] == "VKS_DOCUMENT_API_AMBIGUOUS" for i in parsed.issues)
    section["content"] = "GET /itemsExtra"
    parsed.issues = []
    add_candidates(parsed, {e["stable_key"]: e for e in (api, section)}, artifacts, {}, texts)
    assert not parsed.relations and not parsed.issues
