"""Freeze VKS 3.0 HTTP and MCP contracts using the actual request and response models."""

import json
import os
import tempfile
from pathlib import Path

if __package__:
    from .freeze_v2_contracts import ROOT, document
else:
    from freeze_v2_contracts import ROOT, document


def mcp_document():
    from vks_api.interactive import InteractiveBinding
    from vks_api.interactive_ai import AssistanceInput
    from vks_api.project_tools import DocumentPage, DocumentRead, DocumentWrite, LegacyDocument, ToolContext, WorkspaceExport
    from vks_api.prototype_contracts import PrototypeBindingResult, PrototypeContext, PrototypeEnvelope
    from vks_api.prototype_edit import BindingEdit
    from vks_api.prototype_previews import PreviewRead, PreviewResult

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

    def tool(name, title, description, model, *, write=False, result=dict, version=False):
        schema = model.model_json_schema()
        schema["properties"]["project_id"] = uuid
        schema.setdefault("required", []).append("project_id")
        if write:
            schema["properties"]["idempotency_key"] = {"type": "string", "minLength": 1, "maxLength": 128}
            schema["required"].append("idempotency_key")
        if version:
            schema["properties"]["selection_version"] = {"type": "integer", "minimum": 1}
            schema["required"].append("selection_version")
        return {
            "name": name,
            "title": title,
            "description": description,
            "inputSchema": schema,
            "outputSchema": PrototypeEnvelope[result].model_json_schema(),
            "annotations": {
                "readOnlyHint": not write,
                "openWorldHint": False,
                **({"idempotentHint": True} if write else {}),
            },
        }

    additions = [
        tool(
            "get_interactive_context",
            "读取交互目标的关联与文档",
            "读取准确草稿或保存版本的控件关联、可见文档和章节候选；返回保存所需的版本。",
            AssistanceInput,
            result=ToolContext,
        ),
        tool(
            "save_interactive_binding",
            "保存交互目标的关联文案",
            "共用 Studio 保存服务；修改时必须提交读取所得 binding_id、binding_version、point_revision_id 和 selection_version。保留不可变历史，相同幂等键可重试。",
            InteractiveBinding,
            write=True,
            version=True,
        ),
        tool(
            "get_workspace_document",
            "读取准确版本的文档",
            "读取准确文档修订及其章节。传 max_chars 可分页读取，section_revision_ids 可限定章节。",
            DocumentRead,
            result=DocumentPage | LegacyDocument,
        ),
        tool(
            "save_workspace_document",
            "保存文档新修订",
            "按 base_revision_id 和 row_version 保存新 Markdown；保留原始文件、历史修订与人工章节锁，相同幂等键可重试。",
            DocumentWrite,
            write=True,
        ),
        tool(
            "export_workspace_documents",
            "导出当前项目文档",
            "从草稿复制不可变导出快照，不冻结当前工作区；或导出指定保存版本。返回任务，使用 get_job 查看并取得下载地址。",
            WorkspaceExport,
            write=True,
        ),
        tool(
            "get_prototype_preview",
            "读取准确原型预览",
            "按已返回的定位器读取保存版本中的原型缩略图和 Studio 链接。无图时返回文字，查询不会启动采集。",
            PreviewRead,
            result=PreviewResult,
        ),
    ]
    return {
        "schema_version": "3.0-dev",
        "tools": [
            *additions,
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
