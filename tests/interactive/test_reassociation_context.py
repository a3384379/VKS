"""Revision requests retain the working draft and complete, topical evidence."""

import json

import pytest
from vks_api import ai_gateway
from vks_api.interactive_ai import InteractiveAssistant, ReassociationInput, answer_schema
from vks_api.service import DomainError

from tests.interactive import test_assistant, test_knowledge
from tests.interactive.test_reassociation_preview import bindings, points
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
root = test_knowledge.root


def test_add_chapter_rewrites_exact_target_only_after_preview_confirmation(assistant, monkeypatch):
    (web, service, owner, pid, aid, grant, package, section, _), body, calls = assistant
    saved = web.post(
        root(pid) + "/bindings",
        json=test_knowledge.form(aid, package, section),
        headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "old-link"),
    )
    assert saved.status_code == 200
    old = bindings(web, pid, aid, grant)[0]
    engine = InteractiveAssistant(service)
    chapters = engine.prepare(owner, pid, ReassociationInput.model_validate(body))[1]
    added = next(row for row in chapters if row["revision_id"] not in {r["revision_id"] for r in old["sections"]})
    received = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        received.append(data)
        assert data["control"] == body["target"]
        assert data["existing_binding"]["summary"] == old["summary"]
        assert data["additional_section_revision_ids"] == [added["revision_id"]]
        assert (
            next(row for row in data["chapters"] if row["revision_id"] == added["revision_id"])["content"]
            == added["content"]
        )
        response = test_assistant.response(added["revision_id"])
        result = json.loads(response["output"][0]["content"][0]["text"])
        result["section_revision_ids"] = [old["sections"][0]["revision_id"], added["revision_id"]]
        result["explanation"] = old["summary"] + "\n\n新增章节补全的规则。[2]"
        response["output"][0]["content"][0]["text"] = json.dumps(result)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    payload = {
        **body,
        "binding_id": old["id"],
        **{k: old[k] for k in ["binding_version", "point_revision_id"]},
        "additional_section_revision_ids": [added["revision_id"]],
    }
    before = points(service)
    response = web.post(root(pid) + "/reassociate", json=payload, headers=_headers(grant, 0, "added-preview"))
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["can_confirm"] and not preview["saved"] and preview["baseline"]["explanation"] == old["summary"]
    assert bindings(web, pid, aid, grant)[0] == old and points(service) == before
    changed_basis = web.post(
        root(pid) + "/reassociate",
        json={**payload, "additional_section_revision_ids": [], "base_preview_id": preview["preview_id"]},
        headers=_headers(grant, 0, "changed-added"),
    )
    assert changed_basis.status_code == 412 and len(received) == 1
    confirm = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": preview["preview_id"]},
        headers=_headers(grant, 0, "added-confirm"),
    )
    assert confirm.status_code == 200, confirm.text
    final = bindings(web, pid, aid, grant)[0]
    assert final["target"] == old["target"] and final["summary"] == preview["explanation"]
    assert added["revision_id"] in {row["revision_id"] for row in final["sections"]}
    assert len(received) == 1 and not calls


def test_added_chapter_is_not_discarded_or_leaked_outside_current_scope(assistant, monkeypatch):
    (web, service, owner, pid, _aid, grant, _package, _section, _), body, calls = assistant
    chapters = InteractiveAssistant(service).prepare(owner, pid, ReassociationInput.model_validate(body))[1]
    added = chapters[-1]
    seen = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        seen.append(data)
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        # Omit the required citation even when recall returns only that chapter.
        # A generator failure would test the mock, not the server's evidence gate.
        answer["section_revision_ids"] = []
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    missing = web.post(
        root(pid) + "/reassociate",
        json={**body, "additional_section_revision_ids": ["not-selected"]},
        headers=_headers(grant, 0, "invalid-added"),
    )
    assert missing.status_code == 412 and not seen
    before = points(service)
    omitted = web.post(
        root(pid) + "/reassociate",
        json={**body, "additional_section_revision_ids": [added["revision_id"]]},
        headers=_headers(grant, 0, "omitted-added"),
    )
    assert omitted.status_code == 422 and "遗漏了新增关联章节" in omitted.text
    assert len(seen) == 1 and not calls and points(service) == before


def test_guided_preflight_keeps_full_formula_family_tables_and_original_draft(assistant, monkeypatch):
    (_web, service, owner, pid, aid, _grant, _package, _section, _), body, _ = assistant
    engine = InteractiveAssistant(service)
    formulas = []
    for i, category in enumerate(["统一计算原则", "周期类公式", "仪表类公式", "临时类公式", "抽成类公式"], 1):
        text = (
            ("说明背景。" * 160 if i == 2 else "")
            + f"\n\n|类型|具体计费公式|\n|---|---|\n|{category}|金额 = 单价 × 用量 × 月数|\n\n适用条件：原文规定的完整期间。"
        )
        formulas.append(
            {
                "revision_id": f"formula-{i}",
                "document_revision_id": "billing",
                "document_name": "计费中心业务说明",
                "name": f"5.{i} {category}",
                "content": text,
            }
        )
    old = {
        "revision_id": "old",
        "document_revision_id": "guide",
        "document_name": "旧依据",
        "name": "收费标准配置",
        "content": "必须保留原有适用条件、结果和边界。",
    }
    rows = [*formulas, old]
    current = {
        "id": "existing",
        "title": "收费标准说明",
        "summary": "原有完整底稿\n\n|参数|含义|\n|---|---|\n|月数|完整期间|",
        "sections": [old],
        "excluded": False,
        "origin": "manual",
        "validity_status": "valid",
    }
    config = {"protocol": "responses", "model_id": "synthetic", "max_input_tokens": 24000, "max_output_tokens": 8000}
    monkeypatch.setattr(engine, "prepare", lambda *a, **k: (config, rows, "synthetic-corpus", "key", 1, True))
    monkeypatch.setattr(engine, "_binding", lambda *a, **k: current)
    monkeypatch.setattr(engine.rag, "available", lambda *a: False)
    reservations = []
    monkeypatch.setattr(engine.ai, "reserve", lambda *a: reservations.append(a[-1]) or "synthetic-usage")
    form = ReassociationInput.model_validate(
        {
            **body,
            "analysis_set_id": aid,
            "instruction": "在现在的基础上补充每种类型具体的计费公式，比如周期类就有很多种具体的计费公式",
        }
    )
    prepared = engine._prepare_inference(owner, pid, form)
    sent = json.loads(prepared.body["input"][1]["content"])
    assert sent["existing_binding"]["summary"] == current["summary"]
    assert sent["existing_binding"]["section_revision_ids"] == ["old"]
    sent_rows = {r["revision_id"]: r for r in sent["chapters"]}
    assert {r["revision_id"] for r in formulas} <= sent_rows.keys()
    assert sent_rows["formula-2"]["content"] == formulas[1]["content"]
    assert (
        len(sent_rows["formula-2"]["content"]) > 650
        and "金额 = 单价 × 用量 × 月数" in sent_rows["formula-2"]["content"]
    )
    assert "不要生成教学讲解、展开公式示例" not in prepared.body["input"][0]["content"]
    assert "在原有基础上继续补全完善细节" in prepared.body["input"][0]["content"]
    assert "尽量沿用底稿的章节结构" in prepared.body["input"][0]["content"]
    assert sent["writing_instruction"] == form.instruction
    assert answer_schema(form)["properties"]["section_revision_ids"]["maxItems"] == 12
    assert len(reservations) == 1
    from copy import deepcopy

    partial = deepcopy(prepared.body)
    limited = json.loads(partial["input"][1]["content"])
    limited["chapters"] = limited["chapters"][:1]
    partial["input"][1]["content"] = json.dumps(limited, ensure_ascii=False)
    config["max_input_tokens"] = len(json.dumps(partial, ensure_ascii=False).encode()) + 257
    with pytest.raises(DomainError, match="完整覆盖"):
        engine._prepare_inference(owner, pid, form)
    assert len(reservations) == 1
    config["max_input_tokens"] = 1000
    with pytest.raises(DomainError, match="完整底稿"):
        engine._prepare_inference(owner, pid, form)
    assert len(reservations) == 1


def test_regeneration_uses_last_preview_and_rejects_other_control(assistant, monkeypatch):
    (web, service, _owner, pid, aid, grant, package, section, _), body, calls = assistant
    version = test_knowledge.current(web, pid, aid, grant)["selection_version"]
    saved = web.post(
        root(pid) + "/bindings",
        json=test_knowledge.form(aid, package, section),
        headers=_headers(grant, version, "baseline"),
    )
    assert saved.status_code == 200
    old = bindings(web, pid, aid, grant)[0]
    payload = {**body, "binding_id": old["id"], **{k: old[k] for k in ["binding_version", "point_revision_id"]}}
    submitted = []

    def model(config, request):
        data = json.loads(request["input"][1]["content"])
        submitted.append(data)
        assert data["existing_binding"]["summary"] == old["summary"]
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        if len(submitted) == 1:
            assert data["previous_preview"] is None
            answer["explanation"] = old["summary"] + "\n\n## 第一轮补充\n完整适用条件。"
        else:
            assert "第一轮补充" in data["previous_preview"]["explanation"]
            answer["explanation"] = (
                data["previous_preview"]["explanation"] + "\n\n|公式|条件|\n|---|---|\n|金额 = 单价 × 月数|整月|"
            )
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    facts = points(service)
    first = web.post(
        root(pid) + "/reassociate", json={**payload, "instruction": "补充适用条件"}, headers=_headers(grant, 0, "first")
    )
    assert first.status_code == 200, first.text
    first = first.json()["data"]
    assert first["baseline"]["explanation"] == old["summary"]
    assert first["baseline"]["point_revision_id"] == old["point_revision_id"]
    second = web.post(
        root(pid) + "/reassociate",
        json={**payload, "instruction": "保留第一轮，再补充公式表格", "base_preview_id": first["preview_id"]},
        headers=_headers(grant, 0, "second"),
    )
    assert second.status_code == 200, second.text
    second = second.json()["data"]
    assert second["baseline"] == first["baseline"]
    assert "第一轮补充" in second["explanation"] and "|公式|条件|" in second["explanation"]
    assert len(submitted) == 2 and bindings(web, pid, aid, grant)[0] == old and points(service) == facts
    wrong = web.post(
        root(pid) + "/reassociate",
        json={**payload, "target": {**payload["target"], "selector": "#other"}, "base_preview_id": first["preview_id"]},
        headers=_headers(grant, 0, "wrong-target"),
    )
    assert wrong.status_code == 412 and len(submitted) == 2
    confirmed = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": second["preview_id"]},
        headers=_headers(grant, 0, "confirm"),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert bindings(web, pid, aid, grant)[0]["summary"] == second["explanation"] and len(submitted) == 2 and calls == []


def test_saved_references_are_distinct_from_additional_local_candidates(assistant):
    (web, service, owner, pid, aid, grant, package, section, _), body, calls = assistant
    version = test_knowledge.current(web, pid, aid, grant)["selection_version"]
    saved = web.post(
        root(pid) + "/bindings",
        json=test_knowledge.form(aid, package, section),
        headers=_headers(grant, version, "saved-reference"),
    )
    assert saved.status_code == 200
    engine = InteractiveAssistant(service)
    from vks_api.interactive_ai import AssistanceInput

    form = AssistanceInput.model_validate(body)
    authorized = engine.prepare(owner, pid, form, for_model=False)[1]
    assert len(authorized) > 1
    form = form.model_copy(
        update={
            "context": form.context.model_copy(update={"page_title": " ".join(row["name"] for row in authorized)[:200]})
        }
    )
    result = engine.related(owner, pid, form)
    assert result["origin"] == "binding" and len(result["sections"]) == 1
    cited = {row["revision_id"] for row in result["sections"]}
    assert result["related_sections"] and not cited & {row["revision_id"] for row in result["related_sections"]}
    assert calls == [] and len(bindings(web, pid, aid, grant)[0]["sections"]) == 1


def test_luna_association_budget_is_independent_and_long_preview_confirms(assistant, monkeypatch):
    from vks_api.ai_gateway import AiService
    from vks_api.interactive_ai import InteractiveQuestionInput

    (web, service, owner, pid, aid, grant, _package, _section, _), body, _calls = assistant
    ai = AiService(service)
    saved = ai.get_policy(owner, pid)
    config = {k: v for k, v in saved.items() if k in ai_gateway.PolicyInput.model_fields}
    config.update(model_id="gpt-6-luna", max_input_tokens=12000, max_output_tokens=8000)
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", config["endpoint"])
    ai.set_policy(owner, pid, config, saved["row_version"])
    engine = InteractiveAssistant(service)
    form = ReassociationInput.model_validate({**body, "instruction": "补充完整的公式表格"})
    effective, *_ = engine.prepare(owner, pid, form)
    assert effective["max_input_tokens"] == 128000 and effective["max_output_tokens"] == 16000
    question = InteractiveQuestionInput.model_validate({**body, "question": "规则是什么？"})
    chat, *_ = engine.prepare(owner, pid, question)
    assert chat["max_input_tokens"] == 12000 and chat["max_output_tokens"] == 8000
    long_text = "## 具体公式与适用条件\n\n|参数|公式|\n|---|---|\n" + "|周期|单价 × 面积 × 月数|\n" * 600
    assert len(long_text) > 8000

    def model(config, request):
        assert config["max_output_tokens"] == request["max_output_tokens"] == 16000
        assert config["_request_timeout_seconds"] == 120
        answer = test_assistant.response(json.loads(request["input"][1]["content"])["chapters"][0]["revision_id"])
        parsed = json.loads(answer["output"][0]["content"][0]["text"])
        parsed["explanation"] = long_text
        answer["output"][0]["content"][0]["text"] = json.dumps(parsed)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", model)
    before = bindings(web, pid, aid, grant)
    generated = web.post(
        root(pid) + "/reassociate",
        json={**body, "instruction": "补充完整公式"},
        headers=_headers(grant, 0, "long-preview"),
    )
    assert generated.status_code == 200, generated.text
    preview = generated.json()["data"]
    assert preview["explanation"] == long_text and bindings(web, pid, aid, grant) == before
    confirmed = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": preview["preview_id"]},
        headers=_headers(grant, 0, "long-confirm"),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert bindings(web, pid, aid, grant)[0]["summary"] == long_text
    assert ai.get_policy(owner, pid)["max_output_tokens"] == 8000

    saved = ai.get_policy(owner, pid)
    config.update(association_max_input_tokens=64000, association_max_output_tokens=24000)
    ai.set_policy(owner, pid, config, saved["row_version"])
    changed, *_ = engine.prepare(owner, pid, form)
    assert (changed["max_input_tokens"], changed["max_output_tokens"]) == (64000, 24000)
    chat, *_ = engine.prepare(owner, pid, question)
    assert chat["max_output_tokens"] == 8000
