"""Generated association previews and saved bodies cannot repeat their title."""

import json

import pytest
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy
from vks_api.association_inference import response_for
from vks_api.association_text import association_body_without_title

from tests.interactive import test_assistant, test_knowledge
from tests.interactive.test_reassociation_preview import bindings, points
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
root = test_knowledge.root
TITLE = "租赁应收模块说明"
BODY = "### 功能与适用范围\n\n核对计划和正式账单。[1]\n\n|参数|计算规则|\n|---|---|\n|金额|单价 × 用量|\n\n后文仍可提到租赁应收模块说明。\n"


@pytest.mark.parametrize(
    "prefix",
    [
        f"# {TITLE}\n\n",
        f"# {TITLE}\n\n# {TITLE}\n\n",
        f"\n\n## **{TITLE}** ###\n\n",
        f"{TITLE}\n===\n\n",
        f"**{TITLE}**\n---\n\n",
        f"标题：{TITLE}\n\n",
        f"**标题：** {TITLE}\n\n",
        f"{TITLE}\n\n",
    ],
)
def test_only_leading_total_titles_are_removed_and_body_bytes_are_preserved(prefix):
    assert association_body_without_title(prefix + BODY, TITLE) == BODY


@pytest.mark.parametrize(
    "body",
    [
        BODY,
        "# 功能与适用范围\n\n规则不得删除。",
        f"    {TITLE}\n\n代码样例。",
        f" \t{TITLE}\n\n代码样例。",
        f"```markdown\n# {TITLE}\n```\n\n代码样例。",
        f"> # {TITLE}\n\n引用块。",
        f"- {TITLE}\n\n列表。",
        f"前言不是标题。\n\n# {TITLE}\n\n后文标题保留。",
        f"{TITLE}用于核对计划。[1]",
        f"# <span>{TITLE}</span>\n\nHTML 标记保留。",
    ],
)
def test_business_sections_mentions_and_code_are_not_removed(body):
    assert association_body_without_title(body, TITLE) == body


def test_inherited_title_and_crlf_content_are_preserved_separately():
    body = BODY.replace("\n", "\r\n")
    assert association_body_without_title("# 原有关联标题\r\n\r\n" + body, TITLE, "原有关联标题") == body


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize("operation", ["assist", "reassociate"])
def test_generated_body_is_normalized_before_preview_and_exact_confirmation(
    assistant, monkeypatch, protocol, operation
):
    (web, service, _owner, pid, aid, grant, package, section, _), request, calls = assistant
    with service.db() as session:
        policy = session.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = {**policy.settings, "protocol": protocol}
    old = None
    if operation == "reassociate":
        initial = test_knowledge.form(aid, package, section)
        initial["point"] = {"title": "原有关联标题", "summary": "# 原有关联标题\n\n旧稿的有效业务规则。"}
        result = web.post(
            root(pid) + "/bindings",
            json=initial,
            headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "old"),
        )
        assert result.status_code == 200, result.text
        old = bindings(web, pid, aid, grant)[0]
        request = {
            **request,
            "binding_id": old["id"],
            **{key: old[key] for key in ("binding_version", "point_revision_id")},
        }
    before = points(service)

    def model(config, payload):
        messages = payload.get("input", payload.get("messages"))
        data = json.loads(messages[1]["content"])
        calls.append(data)
        assert "explanation 只填写正文" in messages[0]["content"]
        answer = {
            "title": TITLE,
            "explanation": f"# {TITLE}\n\n# {TITLE}\n\n" + ("# 原有关联标题\n\n" if old else "") + BODY,
            "section_revision_ids": [data["chapters"][0]["revision_id"]],
            "confidence": 0.95,
            "unknowns": [],
        }
        return response_for(answer, protocol, (100, 50), [])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    response = web.post(root(pid) + "/" + operation, json=request, headers=_headers(grant, 0, "generate"))
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["title"] == TITLE and result["explanation"] == BODY
    if operation == "reassociate":
        assert not result["saved"] and points(service) == before
        assert result["baseline"]["explanation"] == old["summary"]
        viewed = web.get(
            root(pid) + "/reassociate/previews/" + result["preview_id"], headers=_headers(grant, 0, "view")
        )
        assert viewed.status_code == 200 and viewed.json()["data"] == result
        confirmed = web.post(
            root(pid) + "/reassociate/confirm",
            json={"preview_id": result["preview_id"]},
            headers=_headers(grant, 0, "confirm"),
        )
        assert confirmed.status_code == 200 and confirmed.json()["data"]["explanation"] == BODY
        assert all(row in points(service) for row in before)
    saved = bindings(web, pid, aid, grant)[0]
    assert saved["title"] == TITLE and saved["summary"] == BODY and len(calls) == 1


def test_title_only_result_is_rejected_without_replacing_knowledge(assistant, monkeypatch):
    (web, service, _owner, pid, _aid, grant, _package, _section, _), request, _calls = assistant
    before = points(service)

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        answer = {
            "title": TITLE,
            "explanation": f"# {TITLE}",
            "section_revision_ids": [data["chapters"][0]["revision_id"]],
            "confidence": 0.95,
            "unknowns": [],
        }
        return response_for(answer, "responses", (100, 50), [])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    response = web.post(root(pid) + "/reassociate", json=request, headers=_headers(grant, 0, "empty"))
    assert response.status_code == 422 and "缺少正文" in response.text
    assert points(service) == before


@pytest.mark.parametrize("text, status", [("缺少原文的正文 [2]。", 422), ("正文 [1]。示例代码 `[12]`。", 200)])
def test_numbered_citations_in_generated_prose_need_matching_sections(assistant, monkeypatch, text, status):
    (web, service, _owner, pid, _aid, grant, _package, _section, _), request, _calls = assistant
    before = points(service)

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        answer = {
            "title": TITLE,
            "explanation": text,
            "section_revision_ids": [data["chapters"][0]["revision_id"]],
            "confidence": 0.95,
            "unknowns": [],
        }
        return response_for(answer, "responses", (100, 50), [])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    response = web.post(root(pid) + "/reassociate", json=request, headers=_headers(grant, 0, "mapped-refs"))
    assert response.status_code == status, response.text
    assert points(service) == before
    if status == 422:
        assert "无对应原文的引用编号" in response.text
