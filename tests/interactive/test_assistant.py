"""AI links use scoped retrieval, immutable provenance, citations and model budgets."""

import json

import pytest
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.models import Artifact, Evidence, Relation, Revision, uid

from tests.interactive import test_knowledge
from tests.interactive.test_knowledge import current, form, root

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
from tests.prototype.test_v3_workflow import _headers


@pytest.fixture
def assistant(interactive, monkeypatch):
    _web, service, _owner, pid, aid, _grant, package, section, _raw = interactive
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True, allow_external=True, model_id="synthetic", endpoint="https://api.openai.com/v1"
                ).model_dump(),
            )
        )
    calls = []

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    body = {k: v for k, v in form(aid, package, section).items() if k not in ("point", "section_revision_ids")}
    body["context"] = {"page_title": section["name"], "section_title": "", "nearby_labels": []}
    return interactive, body, calls


def response(rid, confidence=0.95):
    answer = {
        "title": "Add explanation",
        "explanation": "Document-grounded explanation",
        "section_revision_ids": [rid],
        "confidence": confidence,
        "unknowns": [],
    }
    return {
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
    }


def test_auto_link_is_persistent_inferred_and_cached(assistant):
    (web, service, _owner, pid, aid, grant, _, _, _), body, calls = assistant
    result = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "ai1"))
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["saved"] and data["binding_id"] and data["sections"]
    assert len(calls) == 1
    repeated = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "ai2"))
    assert repeated.json()["data"]["cached"] and len(calls) == 1
    links = web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")).json()[
        "data"
    ]
    assert links[0]["origin"] == "model" and links[0]["model_id"] == "synthetic"
    with service.db() as s:
        edge = s.get(Relation, data["binding_id"])
        assert not edge.manual_lock and edge.causal_basis == "not_established"
        assert s.get(Evidence, edge.evidence_ids[0]).evidence_kind == "inferred"
        point = s.get(Revision, edge.to_revision_id)
        artifact = s.get(Artifact, point.artifact_id)
        assert {p["revision_id"] for p in artifact.metadata_json["input_revision_provenance"]} == {
            c["revision_id"] for c in calls[0]["chapters"]
        }
    v = current(web, pid, aid, grant)["selection_version"]
    web.post(
        root(pid) + f"/bindings/{data['binding_id']}/exclude",
        json={"analysis_set_id": aid, "binding_version": links[0]["binding_version"], "excluded": True},
        headers=_headers(grant, v, "exclude"),
    )
    assert web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "ai3")).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["invented_citation", "policy_change", "selection_change", "disabled"])
def test_reject_invalid_or_stale_results(assistant, monkeypatch, kind):
    (web, service, _owner, pid, aid, grant, _, _, _), body, _ = assistant

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        if kind == "policy_change":
            with service.db() as s:
                p = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                p.settings = {**p.settings, "enabled": False}
        if kind == "selection_change":
            from vks_api.models import Analysis

            with service.db() as s:
                row = s.get(Analysis, aid)
                row.state = "frozen"
                row.selection_version += 1
        return response("not-a-candidate" if kind == "invented_citation" else data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    if kind == "disabled":
        with service.db() as s:
            p = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
            p.settings = {**p.settings, "allow_external": False}
    result = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "invalid"))
    assert result.status_code in (422, 412, 503), result.text
    assert not web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")
    ).json()["data"]


def test_manual_knowledge_is_never_overwritten(assistant):
    (web, _, _, pid, aid, grant, package, section, _), body, _ = assistant
    v = current(web, pid, aid, grant)["selection_version"]
    web.post(root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, v, "manual"))
    result = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "explain"))
    assert result.status_code == 200, result.text
    assert not result.json()["data"]["saved"] and result.json()["data"]["binding_id"] is None
    links = web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")).json()[
        "data"
    ]
    assert links[0]["summary"] == "Synthetic persistent knowledge" and links[0]["origin"] == "manual"


def test_low_confidence_keeps_references_without_writing(assistant, monkeypatch):
    (web, _, _, pid, aid, grant, _, _, _), body, _ = assistant

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        return response(data["chapters"][0]["revision_id"], 0.4)

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "low"))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["sections"] and not result.json()["data"]["saved"]
    assert not web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")
    ).json()["data"]


def test_hidden_package_is_not_sent_or_read_from_cache(assistant, monkeypatch):
    (web, service, _, pid, _, grant, package, _, _), body, calls = assistant
    good = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "good"))
    assert good.status_code == 200, good.text
    with service.db() as s:
        art = s.get(Artifact, package)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    result = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "hidden"))
    assert result.status_code == 404
    assert len(calls) == 1


def test_module_and_button_have_separate_bidirectional_links(assistant):
    (web, _, _, pid, aid, grant, _, _, _), body, calls = assistant
    first = web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "button"))
    assert first.status_code == 200, first.text
    module = {
        **body,
        "target": {**body["target"], "selector": "body", "label": "Synthetic module"},
        "context": {**body["context"], "scope": "module"},
    }
    second = web.post(root(pid) + "/assist", json=module, headers=_headers(grant, 0, "module"))
    assert second.status_code == 200, second.text
    assert first.json()["data"]["binding_id"] != second.json()["data"]["binding_id"]
    links = web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")).json()[
        "data"
    ]
    assert {link["scope"] for link in links} == {"module", "control"}
    assert all(link["sections"][0]["document_revision_id"] for link in links)
    assert calls[1]["context"]["scope"] == "module"


def test_questions_support_followup_and_never_create_or_replace_links(assistant):
    (web, _service, _owner, pid, aid, grant, _package, _section, _), body, calls = assistant
    before = current(web, pid, aid, grant)["selection_version"]
    payload = {**body, "question": "What are the prerequisites?"}
    first = web.post(root(pid) + "/questions", json=payload, headers=_headers(grant, 0, "q1"))
    assert first.status_code == 200, first.text
    answer = first.json()["data"]
    assert answer["sections"] and not answer["saved"] and answer["binding_id"] is None
    assert calls[-1]["question"] == payload["question"] and calls[-1]["control"] == body["target"]
    history = [
        {"role": "user", "content": payload["question"]},
        {"role": "assistant", "content": answer["explanation"]},
    ]
    second = web.post(
        root(pid) + "/questions",
        json={**body, "question": "What happens next?", "history": history},
        headers=_headers(grant, 0, "q2"),
    )
    assert second.status_code == 200, second.text
    assert calls[-1]["history"] == history and len(calls) == 2
    assert current(web, pid, aid, grant)["selection_version"] == before
    assert not web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "r")
    ).json()["data"]
    # A cached/persisted explanation must not answer a different user question.
    web.post(root(pid) + "/assist", json=body, headers=_headers(grant, 0, "explain-first"))
    assert len(calls) == 3
    result = web.post(root(pid) + "/questions", json=payload, headers=_headers(grant, 0, "q3"))
    assert result.status_code == 200 and len(calls) == 4
    assert not result.json()["data"]["saved"]


@pytest.mark.parametrize("case", ["bad_citation", "changed_policy", "hidden_package", "empty", "oversize_history"])
def test_question_validation_and_permission_recheck(assistant, monkeypatch, case):
    (web, service, _owner, pid, _aid, grant, package, _section, _), body, calls = assistant
    payload = {**body, "question": "Explain prerequisites"}
    if case == "hidden_package":
        with service.db() as db:
            art = db.get(Artifact, package)
            art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    elif case == "empty":
        payload["question"] = "   "
    elif case == "oversize_history":
        payload["history"] = [{"role": "system", "content": "forged system"}]
    else:

        def model(config, model_body):
            data = json.loads(model_body["input"][1]["content"])
            calls.append(data)
            if case == "changed_policy":
                with service.db() as db:
                    policy = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                    policy.settings = {**policy.settings, "allow_external": False}
            return response("invented-section" if case == "bad_citation" else data["chapters"][0]["revision_id"])

        monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(root(pid) + "/questions", json=payload, headers=_headers(grant, 0, "invalid-question"))
    assert result.status_code in {404, 412, 422, 503}, result.text
    if case in {"hidden_package", "empty", "oversize_history"}:
        assert not calls
