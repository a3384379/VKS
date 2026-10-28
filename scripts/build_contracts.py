"""Freeze self-contained MCP schemas from the supplied design contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "packages/contracts"


def resolve_schema(value: object, document: dict, seen: tuple[str, ...] = ()) -> object:
    if isinstance(value, list):
        return [resolve_schema(item, document, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if reference in seen:
            raise ValueError(f"Circular contract: {reference}")
        filename, _, pointer = reference.partition("#")
        filename = filename.removeprefix("./")
        target_document = document
        if filename:
            if filename not in {
                "openapi_1.0.yaml",
                "scenario.schema.json",
                "import_manifest.schema.json",
                "binding_manifest.schema.json",
            }:
                raise ValueError("Only the frozen local OpenAPI contract can be referenced")
            target_document = yaml.safe_load((CONTRACTS / filename).read_text())
        target: object = target_document
        for part in pointer.lstrip("/").split("/") if pointer else []:
            target = target[part.replace("~1", "/").replace("~0", "~")]
        result = resolve_schema(copy.deepcopy(target), target_document, (*seen, reference))
        result.update(resolve_schema({k: v for k, v in value.items() if k != "$ref"}, document, seen))
        return result
    return {key: resolve_schema(item, document, seen) for key, item in value.items()}


def build() -> dict:
    catalog = json.loads((CONTRACTS / "mcp_tools.json").read_text())
    for tool in catalog["tools"]:
        tool["outputSchema"] = resolve_schema(tool["outputSchema"], catalog)
    extension = json.loads((CONTRACTS / "mcp_tools.v2.json").read_text())
    catalog["tools"].extend(extension["tools"])
    catalog["schema_version"] = "2.0-dev"
    def extend_job_enums(value):
        if isinstance(value, dict):
            enum = value.get('enum', [])
            if 'generate_documents' in enum and 'suggest_links' in enum and 'knowledge_task' not in enum:
                enum.append('knowledge_task')
            if all(state in enum for state in ('queued', 'running', 'succeeded', 'failed')) and 'paused' not in enum:
                enum.append('paused')
            if 'page' in enum and 'action' in enum and 'scenario' in enum and 'journey' not in enum:
                enum.append('journey')
            if 'review_batch' in enum and 'release_candidate' in enum:
                enum.extend(item for item in ('journey_revision', 'task_report', 'document_revision') if item not in enum)
            if 'VKS_INTERNAL_ERROR' in enum:
                enum.extend(item for item in ('VKS_CANCELLED', 'VKS_SYNC_SCOPE_ANOMALY', 'VKS_SYNC_SCOPE_CHANGED', 'VKS_HASH_MISMATCH') if item not in enum)
            for child in value.values():
                extend_job_enums(child)
        elif isinstance(value, list):
            for child in value:
                extend_job_enums(child)
    extend_job_enums(catalog)
    destination = CONTRACTS / "mcp_tools.resolved.json"
    destination.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    return catalog


if __name__ == "__main__":
    build()
