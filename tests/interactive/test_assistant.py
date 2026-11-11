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
                s.get(Analysis, aid).selection_version += 1
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
