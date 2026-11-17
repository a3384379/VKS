"""Independent lecture generation cannot create or rewrite an association."""

import json

from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import Analysis, Artifact, Relation, Revision, Selection

from tests.interactive import test_assistant
from tests.interactive.test_knowledge import current, form, root
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def saved_binding(web, pid, aid, grant):
    return web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")).json()[
        "data"
    ][0]


def knowledge_snapshot(service, aid):
    with service.db() as s:
        return (
            s.get(Analysis, aid).selection_version,
            [
                (model.__name__, tuple(s.scalars(select(model.id).order_by(model.id))))
                for model in (Artifact, Revision, Relation)
            ],
            sorted(
                s.execute(
                    select(Selection.entity_id, Selection.entity_revision_id).where(Selection.analysis_set_id == aid)
                ).all()
            ),
        )


def test_explanation_refresh_is_independent_and_never_writes_knowledge(assistant, monkeypatch):
    (web, service, _owner, pid, aid, grant, package, section, _), body, _calls = assistant
    web.post(
        root(pid) + "/bindings",
        json=form(aid, package, section),
        headers=_headers(grant, current(web, pid, aid, grant)["selection_version"], "manual"),
    )
    old = saved_binding(web, pid, aid, grant)
    before = knowledge_snapshot(service, aid)
    calls = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        assert config["_request_timeout_seconds"] == 120
        assert "writing_instruction" not in data and "existing_binding" not in data
        assert data["associated_knowledge"]["summary"] == old["summary"]
        assert {row["revision_id"] for row in data["chapters"]} == {row["revision_id"] for row in old["sections"]}
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["explanation"] = f"独立讲解 {len(calls)}"
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    for refresh, expected, cached in [(False, 1, False), (False, 1, True), (True, 2, False)]:
        result = web.post(
            root(pid) + "/explain", json={**body, "refresh": refresh}, headers=_headers(grant, 0, "lecture")
        )
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert data["explanation"] == f"独立讲解 {expected}" and data["cached"] is cached
        assert not data["saved"] and data["binding_id"] is None
    assert len(calls) == 2 and knowledge_snapshot(service, aid) == before
    assert saved_binding(web, pid, aid, grant) == old
    rejected = web.post(
        root(pid) + "/explain", json={**body, "instruction": "不应该需要提示词"}, headers=_headers(grant, 0, "invalid")
    )
    assert rejected.status_code == 422 and len(calls) == 2


def test_reassociation_and_explanation_have_distinct_prompts_and_cache(assistant, monkeypatch):
    (web, _service, _owner, pid, aid, grant, _package, _section, _), body, _calls = assistant
    calls = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        system = payload["input"][0]["content"]
        guided = "writing_instruction" in data
        assert ("关联说明" in system) if guided else ("只生成独立讲解" in system)
        calls.append(data)
        response = test_assistant.response(data["chapters"][0]["revision_id"])
        answer = json.loads(response["output"][0]["content"][0]["text"])
        answer["explanation"] = "新的关联说明" if guided else "独立功能讲解"
        response["output"][0]["content"][0]["text"] = json.dumps(answer)
        return response

    monkeypatch.setattr(ai_gateway, "request_model", model)
    lecture = web.post(root(pid) + "/explain", json=body, headers=_headers(grant, 0, "first"))
    assert lecture.status_code == 200 and not lecture.json()["data"]["saved"]
    assert not web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "empty")
    ).json()["data"]
    assoc = web.post(
        root(pid) + "/reassociate",
        json={**body, "instruction": "重点关联办理前提"},
        headers=_headers(grant, 0, "assoc"),
    )
    assert assoc.status_code == 200 and not assoc.json()["data"]["saved"]
    confirmed = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": assoc.json()["data"]["preview_id"]},
        headers=_headers(grant, 0, "confirm-assoc"),
    )
    assert confirmed.status_code == 200 and confirmed.json()["data"]["saved"], confirmed.text
    before = saved_binding(web, pid, aid, grant)
    assert before["summary"] == "新的关联说明"
    lecture = web.post(root(pid) + "/explain", json=body, headers=_headers(grant, 0, "second"))
    assert lecture.status_code == 200 and not lecture.json()["data"]["cached"]
    assert lecture.json()["data"]["explanation"] == "独立功能讲解"
    assert len(calls) == 3 and saved_binding(web, pid, aid, grant) == before


def test_explanation_rejects_a_changed_association_without_overwriting_it(assistant, monkeypatch):
    (web, service, owner, pid, aid, grant, package, section, _), body, _calls = assistant
    web.post(
        root(pid) + "/bindings",
        json=form(aid, package, section),
        headers=_headers(grant, current(web, pid, aid, grant)["selection_version"], "manual"),
    )
    old = saved_binding(web, pid, aid, grant)

    def model(config, payload):
        update = {
            **form(aid, package, section),
            "binding_id": old["id"],
            **{key: old[key] for key in ("binding_version", "point_revision_id")},
            "point": {"title": "人工更新", "summary": "生成讲解期间更新的关联"},
        }
        InteractiveKnowledgeService(service).save(
            owner,
            pid,
            InteractiveBinding.model_validate(update),
            current(web, pid, aid, grant)["selection_version"],
            "concurrent",
        )
        data = json.loads(payload["input"][1]["content"])
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(root(pid) + "/explain", json=body, headers=_headers(grant, 0, "lecture"))
    assert result.status_code == 412, result.text
    assert saved_binding(web, pid, aid, grant)["summary"] == "生成讲解期间更新的关联"
