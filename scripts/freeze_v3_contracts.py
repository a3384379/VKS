"""Freeze VKS 3.0 HTTP and MCP contracts using the actual request and response models."""

import json
import os
from pathlib import Path
import tempfile

if __package__:
    from .freeze_v2_contracts import ROOT, document
else:
    from freeze_v2_contracts import ROOT, document


def mcp_document():
    from vks_api.prototype_contracts import PrototypeBindingResult, PrototypeContext, PrototypeEnvelope
    from vks_api.prototype_edit import BindingEdit

    uuid = {"type": "string", "format": "uuid"}
    key = {"type": "string", "pattern": r"^[a-z][a-z0-9._-]{0,95}$"}
    common = {"project_id": uuid, "action_key": key}
    context = {
        "type": "object",
        "properties": {
            **common,
            "page_key": key,
            "trigger_state_key": key,
            "analysis_set_id": uuid,
            "release_id": uuid,
            "policy": {"enum": ["valid", "outdated", "all"]},
        },
        "required": [*common, "page_key", "trigger_state_key"],
        "oneOf": [{"required": ["analysis_set_id"]}, {"required": ["release_id"]}],
        "additionalProperties": False,
    }
    binding = BindingEdit.model_json_schema()
    binding["properties"].update(
        common,
        selection_version={"type": "integer", "minimum": 1},
        idempotency_key={"type": "string", "minLength": 1, "maxLength": 128},
    )
    binding["required"].extend([*common, "selection_version", "idempotency_key"])
    return {
        "schema_version": "3.0-dev",
        "tools": [
            {
                "name": "get_prototype_action_context",
                "title": "读取原型动作与业务说明",
                "description": "读取准确版本、页面及触发状态中的可见业务点和原文章节。",
                "inputSchema": context,
                "outputSchema": PrototypeEnvelope[PrototypeContext].model_json_schema(),
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "save_business_point_binding",
                "title": "保存业务点与动作关联",
                "description": "按草稿版本直接保存业务点、适用状态和准确章节证据。相同幂等键可重试。",
                "inputSchema": binding,
                "outputSchema": PrototypeEnvelope[PrototypeBindingResult].model_json_schema(),
                "annotations": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False},
            },
        ],
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="vks-v3-contract-") as directory:
        os.environ.update(VKS_DATA_DIR=directory, VKS_DATABASE_URL="sqlite:///:memory:", VKS_TASK_MODE="external")
        for name, value in (("openapi_3.0.json", document(3)), ("mcp_tools.v3.json", mcp_document())):
            (Path(ROOT) / "packages/contracts" / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )
