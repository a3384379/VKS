"""Guided recall can leave the old page matches and distinguish module intent."""

from vks_api import ai_gateway
from vks_api.interactive_ai import (
    AssistanceInput,
    ChapterIndex,
    InteractiveAssistant,
    ReassociationInput,
    guided_candidates,
    merge_guided_candidates,
)
from vks_api.vector_rag import RagSettings

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_focused_guided_recall_keeps_business_formula_chapter():
    rows = [
        {
            "revision_id": f"ui-{n}",
            "document_revision_id": f"guide-{n // 3}",
            "document_name": "收费标准设置",
            "name": "收费标准设置",
            "content": "收费标准设置中的选择操作。",
        }
        for n in range(18)
    ]
    business = {
        "revision_id": "business-formula",
        "document_revision_id": "billing",
        "document_name": "计费中心业务说明",
        "name": "计费公式体系",
        "content": "计费公式按面积、金额、用量及周期计算费用。",
    }
    rows.append(business)
    form = ReassociationInput(
        package_id="00000000-0000-0000-0000-000000000001",
        target={"route": "/", "frame_path": [], "selector": "#formula", "label": "请选择计费公式"},
        context={"page_title": "收费标准设置"},
    )
    index = ChapterIndex(rows)
    contextual = index.search(form.target, form.context, set())
    focused = guided_candidates(index, form)
    assert business["revision_id"] in {r["revision_id"] for r in focused}
    combined = merge_guided_candidates(focused, contextual)
    assert combined[0]["revision_id"] == business["revision_id"]
    assert len(combined) <= 24 and len({r["revision_id"] for r in combined}) == len(combined)


def test_module_reassociation_direction_is_part_of_embedding_and_query_cache(assistant, monkeypatch):
    (_web, service, owner, pid, _aid, _grant, _package, _section, _), body, _calls = assistant
    calls = []

    def embed(config, payload):
        calls.append(payload["input"])
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 63} for i, _ in enumerate(payload["input"])],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embed)
    engine = InteractiveAssistant(service)
    engine.rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    engine.rag._build(owner, pid, AssistanceInput.model_validate(body))
    calls.clear()
    for instruction in ["计费公式", "退款条件", "计费公式"]:
        form = ReassociationInput.model_validate({**body, "instruction": instruction, "context": {"scope": "module"}})
        state = engine.prepare(owner, pid, form)
        assert engine.rag.hybrid(owner, pid, form, state[1], state[2], [])
    assert len(calls) == 2
    assert "重新关联方向：计费公式" in calls[0][0] and "重新关联方向：退款条件" in calls[1][0]
