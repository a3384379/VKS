import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from scripts.build_contracts import build

ROOT = Path(__file__).resolve().parents[2]


def test_original_design_integrity():
    design = ROOT / "docs/design"
    manifest = json.loads((design / "package_manifest.json").read_text())
    assert len(manifest["files"]) == manifest["file_count"] == 44
    for item in manifest["files"]:
        content = (design / item["path"]).read_bytes()
        assert len(content) == item["size_bytes"], item["path"]
        assert hashlib.sha256(content).hexdigest() == item["sha256"], item["path"]


def test_standard_schema_and_openapi_validation():
    root = ROOT / "packages/contracts"
    for path in root.glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    validate(yaml.safe_load((root / "openapi_1.0.yaml").read_text()), base_uri=(root / "openapi_1.0.yaml").as_uri())


def test_mcp_contracts_are_self_contained_and_do_not_approve():
    catalog = build()
    original = json.loads((ROOT / "packages/contracts/mcp_tools.json").read_text())
    assert len(original["tools"]) == 18
    assert {t["name"] for t in original["tools"]} <= {t["name"] for t in catalog["tools"]}
    assert len(catalog["tools"]) == 30
    assert len({t["name"] for t in catalog["tools"]}) == 30
    for tool in catalog["tools"]:
        Draft202012Validator.check_schema(tool["inputSchema"])
        Draft202012Validator.check_schema(tool["outputSchema"])
        assert '"$ref"' not in json.dumps(tool["outputSchema"])
        assert tool["name"] not in {"approve", "publish", "review_relation"}
    assert catalog["authorization"]["annotations_are_security_boundary"] is False


def test_v2_openapi_snapshot_and_journey_contract_match_runtime():
    from scripts.freeze_v2_contracts import document
    from packages.journey_dsl import Journey
    root = ROOT / 'packages/contracts'
    frozen = json.loads((root / 'openapi_2.0.json').read_text())
    assert frozen == document()
    validate(frozen)
    assert json.loads((root / 'journey.schema.json').read_text()) == Journey.model_json_schema()
    assert all(path.startswith('/api/v2/') for path in frozen['paths'])
    from vks_api.task_planning import PLAN_SCHEMA
    assert json.loads((root / 'knowledge_plan.schema.json').read_text()) == PLAN_SCHEMA



def test_v3_http_and_mcp_contracts_match_runtime():
    from scripts.freeze_v3_contracts import document, mcp_document

    root = ROOT / "packages/contracts"
    frozen = json.loads((root / "openapi_3.0.json").read_text())
    assert frozen == document(3)
    validate(frozen)
    assert json.loads((root / "mcp_tools.v3.json").read_text()) == mcp_document()
