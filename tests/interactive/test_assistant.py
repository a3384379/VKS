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


@pytest.mark.parametrize("change", ["endpoint", "context"])
def test_persisted_explanation_rechecks_full_request_identity(assistant, change):
    from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant

    (_web, service, owner, pid, _aid, _grant, _package, _section, _), body, calls = assistant
    first = InteractiveAssistant(service).run(owner, pid, AssistanceInput.model_validate(body))
    assert first["saved"] and len(calls) == 1
    if change == "endpoint":
        with service.db() as db:
            policy = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
            policy.settings = {**policy.settings, "endpoint": "https://changed-provider.invalid/v1"}
    else:
        body = {**body, "context": {**body["context"], "page_title": "Changed workflow context"}}
    # A fresh process has no memory cache and may only reuse a persisted answer
    # if its complete request fingerprint still matches, not just its model ID.
    changed = InteractiveAssistant(service).run(owner, pid, AssistanceInput.model_validate(body))
    assert not changed["cached"] and len(calls) == 2


@pytest.mark.parametrize("route", ["assist", "questions"])
def test_uncited_model_claims_are_not_presented_as_grounded_knowledge(assistant, monkeypatch, route):
    (web, _service, _owner, pid, _aid, grant, _package, _section, _), body, _calls = assistant
    if route == "questions":
        body = {**body, "question": "What are the prerequisites?"}

    def model(config, payload):
        data = response("unused")
        answer = {
            "title": "Unsupported rule",
            "explanation": "UNSUPPORTED_RULE_MUST_NOT_APPEAR",
            "section_revision_ids": [],
            "confidence": 0.99,
            "unknowns": [],
        }
        data["output"][0]["content"][0]["text"] = json.dumps(answer)
        return data

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(root(pid) + "/" + route, json=body, headers=_headers(grant, 0, "uncited"))
    assert result.status_code == 200, result.text
    output = result.json()["data"]
    assert output["confidence"] == 0 and not output["saved"] and not output["sections"]
    assert "UNSUPPORTED_RULE" not in result.text and "不足" in output["explanation"]


def test_concurrent_corpus_index_build_is_shared_without_blocking_other_corpora(assistant, monkeypatch):
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock

    from vks_api import interactive_ai

    (_web, service, _owner, _pid, _aid, _grant, _package, _section, _), _body, _calls = assistant
    engine = interactive_ai.InteractiveAssistant(service)
    original = interactive_ai.ChapterIndex
    gate, entered = Event(), Event()
    start = Barrier(8)
    counts, counter_lock = Counter(), Lock()

    def chapters(key):
        return [{"revision_id": key, "document_revision_id": key, "document_name": key, "name": key, "content": key}]

    def construct(rows):
        key = rows[0]["revision_id"]
        with counter_lock:
            counts[key] += 1
        if key == "same":
            entered.set()
            assert gate.wait(5), "test did not release the slow index build"
        return original(rows)

    monkeypatch.setattr(interactive_ai, "ChapterIndex", construct)

    def request(_index):
        start.wait(5)
        return engine._index("same", chapters("same"))

    with ThreadPoolExecutor(max_workers=9) as pool:
        requests = [pool.submit(request, i) for i in range(8)]
        try:
            assert entered.wait(5)
            # A different corpus can finish while the first build is still busy.
            separate = pool.submit(engine._index, "separate", chapters("separate")).result(timeout=5)
            assert separate.rows[0]["revision_id"] == "separate"
        finally:
            gate.set()
        results = [future.result(timeout=5) for future in requests]
    assert counts == {"same": 1, "separate": 1}
    assert len({id(result) for result in results}) == 1
    assert not engine.index_builds
    assert engine._index("same", chapters("same")) is results[0]
    for key in ["third", "fourth", "fifth"]:
        engine._index(key, chapters(key))
    assert len(engine.indexes) == 4 and "same" in engine.indexes and "separate" not in engine.indexes


@pytest.mark.parametrize("change", ["prompt", "schema"])
def test_persisted_explanation_expires_when_generation_contract_changes(assistant, monkeypatch, change):
    from copy import deepcopy

    from vks_api import interactive_ai

    (_web, service, owner, pid, _aid, _grant, _package, _section, _), body, calls = assistant
    form = interactive_ai.AssistanceInput.model_validate(body)
    first = interactive_ai.InteractiveAssistant(service).run(owner, pid, form)
    assert first["saved"] and len(calls) == 1
    if change == "prompt":
        monkeypatch.setattr(interactive_ai, "SYSTEM", interactive_ai.SYSTEM + "\nUse the revised explanation policy.")
    else:
        schema = deepcopy(interactive_ai.SCHEMA)
        schema["description"] = "Revised generation contract"
        monkeypatch.setattr(interactive_ai, "SCHEMA", schema)
    repeated = interactive_ai.InteractiveAssistant(service).run(owner, pid, form)
    assert not repeated["cached"] and len(calls) == 2


def test_chatgpt_plan_is_explicit_local_and_read_only(assistant, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from vks_api import chatgpt_plan
    from vks_connect.chatgpt_plan import responses_body

    (web, service, _owner, pid, aid, grant, _package, section, _), body, _ = assistant
    origin = "http://127.0.0.1:5193"
    service.studio_base_url = origin + "/"
    service.personal_mode = True
    monkeypatch.setenv("VKS_CHATGPT_LOCAL", "1")
    profile = {"profile_id": "authorized-profile", "connected": True, "account": "synthetic@example.test",
               "pending": False, "message": ""}
    calls, settings = [], {}

    class Runtime:
        def claim_studio(self, owner):
            settings["studio-owner"] = {"owner": owner}
            return True

        def setting(self, key, value=None):
            if value is not None:
                settings[key] = value
            return settings.get(key, {})

        def begin(self):
            return {}

        def status(self):
            return profile.copy()

        def models(self, selected):
            assert selected == profile["profile_id"]
            return [{"id": "synthetic-plan", "name": "Synthetic model"}]

        def generate(self, selected, request, **_):
            assert selected == profile["profile_id"]
            payload = responses_body(request)
            calls.append(payload)
            assert payload["model"] == "synthetic-plan"
            assert payload["input"][0]["role"] == "developer" and payload["store"] is False
            return response(section["entity_revision_id"])

    runtime = Runtime()
    monkeypatch.setattr(chatgpt_plan, "runtime", lambda _: runtime)
    monkeypatch.setattr(ai_gateway, "request_model", ai_gateway._request_model)
    session, owner = service.login(service.bootstrap_token)
    client = TestClient(web.app, base_url=origin, client=("127.0.0.1", 43210))
    client.cookies.set("vks_session", session)
    headers = {"Origin": origin, "X-CSRF-Token": owner.csrf_token}
    manage = f"/api/v3/projects/{pid}/chatgpt-plan"
    monkeypatch.setattr(chatgpt_plan, "sys", SimpleNamespace(platform="linux"))
    unavailable = client.post(manage, json={"action": "connect"}, headers=headers)
    assert unavailable.status_code == 200 and unavailable.json()["data"] == {"available": False}
    assert not settings
    monkeypatch.setattr(chatgpt_plan, "sys", SimpleNamespace(platform="darwin"))
    connected = client.post(manage, json={"action": "connect"}, headers=headers)
    assert connected.status_code == 200 and connected.json()["data"]["connected"], connected.text
    enabled = client.post(manage, json={"action": "enable", "model": "synthetic-plan", "allow_external": True},
                          headers=headers)
    assert enabled.status_code == 200 and enabled.json()["data"]["enabled"], enabled.text
    ask = {**body, "question": "What is the rule for Add?"}
    plan_headers = {**headers, "X-VKS-Model-Access": "chatgpt-plan"}
    denied = client.post(root(pid) + "/questions", json=ask,
                         headers={**plan_headers, "Origin": "http://example.test"})
    assert denied.status_code == 403 and not calls
    denied = client.post(root(pid) + "/questions", json=ask, headers={
        **_headers(grant, 0, "plan-denied"), "Origin": origin, "X-VKS-Model-Access": "chatgpt-plan",
    })
    assert denied.status_code == 403 and not calls
    answer = client.post(root(pid) + "/questions", json=ask, headers=plan_headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["data"]["model_id"] == "synthetic-plan"
    assert answer.json()["data"]["sections"]
    streamed = client.post(root(pid) + "/conversation", json={**body, "question": "导出项目"},
                           headers={**plan_headers, "Accept": "text/event-stream", "Idempotency-Key": "plan-chat"})
    assert "event: complete" in streamed.text and '"operations": []' in streamed.text, streamed.text
    explain = client.post(root(pid) + "/explain", headers=plan_headers, json=body)
    assert explain.status_code == 200 and explain.json()["data"]["model_id"] == "synthetic-plan", explain.text
    assert len(calls) == 3
    assert not web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")).json()["data"]
    client.post(manage, json={"action": "disable"}, headers=headers)
    refused = client.post(root(pid) + "/questions", json=ask, headers=plan_headers)
    assert refused.status_code == 409 and len(calls) == 3
