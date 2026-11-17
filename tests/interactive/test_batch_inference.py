"""Batch transport retains full target-specific evidence and individual outcomes."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from vks_api.association_inference import AssociationBatch, decoded, messages, response_for, usage
from vks_api.interactive_ai import SCHEMA
from vks_api.service import Actor, DomainError


def request_body(index=0, *, shared=False, protocol="responses", content="Full original evidence"):
    rid = "shared" if shared else "revision-" + str(index)
    schema = deepcopy(SCHEMA)
    schema["properties"]["section_revision_ids"]["items"]["enum"] = [rid]
    prompt = [
        {"role": "system", "content": "Retain full independent grounded explanations."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "control": {"label": "control " + str(index)},
                    "context": {"page_title": "same page"},
                    "chapters": [{"revision_id": rid, "name": "Evidence", "content": content}],
                }
            ),
        },
    ]
    if protocol == "responses":
        return {"model": "test", "input": prompt, "text": {"format": {"schema": schema}}, "max_output_tokens": 2000}
    return {
        "model": "test",
        "messages": prompt,
        "response_format": {"json_schema": {"schema": schema}},
        "max_completion_tokens": 2000,
    }


def config(protocol="responses", bound=128000):
    return {"model_id": "test", "protocol": protocol, "max_output_tokens": 2000, "max_input_tokens": bound}


def answer(rid, explanation="Complete explanation with prerequisites, result, and cautions."):
    return {
        "title": "Title",
        "explanation": explanation,
        "section_revision_ids": [rid],
        "confidence": 0.95,
        "unknowns": [],
    }


def provider(body, *, broken=None, tokens=(501, 201)):
    data = json.loads(messages(body)[1]["content"])
    if "targets" in data:
        answers = {}
        for target in data["targets"]:
            rid = data["chapter_pool"][target["chapter_keys"][0]]["revision_id"]
            answers[target["task_id"]] = answer(rid)
        if broken:
            answers["t1"] = broken
        value = {"answers": answers}
    else:
        value = answer(data["chapters"][0]["revision_id"])
    return response_for(value, "responses" if "input" in body else "chat_completions", tokens, [])


def parallel(batch, bodies, *, protocol="responses", bound=128000, preflight=None, pages=None):
    ready = Barrier(len(bodies))

    def run(index):
        ready.wait(timeout=10)
        body = bodies[index]
        ids = [r["revision_id"] for r in json.loads(messages(body)[1]["content"])["chapters"]]
        return batch.request(
            config(protocol, bound),
            body,
            preflight=(lambda: None) if preflight is None else lambda: preflight(index),
            candidates=ids,
            page=["same"] if pages is None else pages[index],
        )

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        futures = [pool.submit(run, index) for index in range(len(bodies))]
        return [future.result(timeout=20) for future in futures]


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_batch_keeps_full_schema_each_target_evidence_and_exact_usage(protocol):
    calls = []

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    results = parallel(
        batch, [request_body(0, protocol=protocol), request_body(1, protocol=protocol)], protocol=protocol
    )
    assert len(calls) == 1
    assert calls[0].get("max_output_tokens", calls[0].get("max_completion_tokens")) == 4000
    assert [decoded(r, protocol)["section_revision_ids"] for r in results] == [["revision-0"], ["revision-1"]]
    assert all(set(r.input_revision_ids) == {"revision-0", "revision-1"} for r in results)
    assert sum(usage(r)[0] for r in results) == 501
    assert sum(usage(r)[1] for r in results) == 201


def test_exact_candidate_snippets_are_deduplicated_without_shortening():
    calls = []
    full = "Complete original content. " * 100

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    parallel(batch, [request_body(0, shared=True, content=full), request_body(1, shared=True, content=full)])
    assert len(calls) == 1
    data = json.loads(messages(calls[0])[1]["content"])
    assert len(data["chapter_pool"]) == 1
    assert data["chapter_pool"]["c0"]["content"] == full
    assert all(t["chapter_keys"] == ["c0"] for t in data["targets"])


def test_invalid_cross_target_citation_retries_only_failed_target():
    calls = []

    def send(cfg, body):
        calls.append(body)
        data = json.loads(messages(body)[1]["content"])
        if "targets" in data:
            first = data["chapter_pool"][data["targets"][0]["chapter_keys"][0]]["revision_id"]
            return provider(body, broken=answer(first))
        return provider(body, tokens=(100, 50))

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    results = parallel(batch, [request_body(0), request_body(1)])
    assert len(calls) == 2
    assert "targets" in json.loads(messages(calls[0])[1]["content"])
    assert "targets" not in json.loads(messages(calls[1])[1]["content"])
    assert [decoded(r, "responses")["section_revision_ids"] for r in results] == [["revision-0"], ["revision-1"]]
    assert sum(usage(r)[0] for r in results) == 601
    assert sum(usage(r)[1] for r in results) == 251


def test_schema_incompatible_completed_response_falls_back_without_resending_successes():
    calls = []

    def send(cfg, body):
        calls.append(body)
        data = json.loads(messages(body)[1]["content"])
        if "targets" in data:
            return response_for({"unrecognized": True}, "responses", (200, 10), [])
        return provider(body, tokens=(100, 50))

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    results = parallel(batch, [request_body(0), request_body(1)])
    assert len(calls) == 3
    assert sum(usage(r)[0] for r in results) == 400
    assert all(decoded(r, "responses")["confidence"] == 0.95 for r in results)


def test_uncertain_transport_is_never_automatically_replayed():
    calls = []

    def send(cfg, body):
        calls.append(body)
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Uncertain transmission", 503)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    with pytest.raises(DomainError):
        parallel(batch, [request_body(0), request_body(1)])
    assert len(calls) == 1


def test_cancellation_during_gather_prevents_affected_target_send():
    calls = []

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    def preflight(index):
        if index == 1:
            raise DomainError("VKS_CANCELLED", "Cancelled before send", 409)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2)
    with pytest.raises(DomainError) as caught:
        parallel(batch, [request_body(0), request_body(1)], preflight=preflight)
    assert caught.value.code == "VKS_CANCELLED"
    assert len(calls) == 1
    data = json.loads(messages(calls[0])[1]["content"])
    assert data["control"]["label"] == "control 0"


def test_different_pages_are_never_combined_and_small_input_cap_splits_without_trimming():
    calls = []

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2, gather_seconds=0.01)
    parallel(batch, [request_body(0), request_body(1)], pages=[["first"], ["second"]])
    assert len(calls) == 2
    calls.clear()
    bodies = [request_body(0, content="a" * 1000), request_body(1, content="b" * 1000)]
    parallel(batch, bodies, bound=2500)
    assert len(calls) == 2
    assert {json.loads(messages(b)[1]["content"])["chapters"][0]["content"] for b in calls} == {"a" * 1000, "b" * 1000}


# Exercise the coalescer through the real retrieval/model/save service boundary.
from contextvars import copy_context
from types import SimpleNamespace
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, ModelUsage, PolicyInput
from vks_api.bulk_association import BulkAssociationService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.models import Artifact, Revision, uid
from tests.interactive import test_materials

materials = test_materials.materials


def installed(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>收费项</title><button id="a">新增</button><button id="b">删除</button>',
    )
    document = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 新增\n\n新增前准备名称。\n\n## 删除\n\n删除前检查引用。"
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True, allow_external=True, model_id="synthetic", max_input_tokens=128000
                ).model_dump(),
            )
        )
        _, _, items, _, _ = BulkAssociationService(service).scan(s, owner, pid, aid)
    forms = [
        AssistanceInput(analysis_set_id=aid, package_id=i["package_id"], target=i["target"], context=i["context"])
        for i in items
        if i["target"]["selector"] in {"#a", "#b"}
    ]
    a = InteractiveAssistant(service)
    chapters = a.prepare(owner, pid, forms[0])[1]
    assert len(forms) == 2 and len(chapters) == 2
    chosen = {form.target.label: chapters[index] for index, form in enumerate(forms)}
    a._index = lambda *_: SimpleNamespace(search=lambda target, *_: [chosen[target.label]])
    return service, owner, pid, aid, a, forms, chosen


def normal_runs(a, owner, pid, forms):
    with a.batch(owner, pid, max_items=2, concurrency=1, gather_seconds=1):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, a.run, owner, pid, form) for form in forms]
            return [f.result(timeout=20) for f in futures]


def test_real_save_inherits_all_sent_batch_inputs_and_preserves_target_specific_citations(materials, monkeypatch):
    service, owner, pid, aid, a, forms, chosen = installed(materials)
    calls = []

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    monkeypatch.setattr(ai_gateway, "request_model", send)
    results = normal_runs(a, owner, pid, forms)
    assert len(calls) == 1 and all(r["saved"] for r in results)
    all_ids = {chapter["revision_id"] for chapter in chosen.values()}
    links = InteractiveKnowledgeService(service).bindings(owner, pid, aid)
    assert len(links) == 2
    with service.db() as s:
        for link in links:
            assert [c["revision_id"] for c in link["sections"]] == [chosen[link["target"]["label"]]["revision_id"]]
            art = s.get(Artifact, s.get(Revision, link["point_revision_id"]).artifact_id)
            assert set(art.metadata_json["interactive_generation"]["input_revision_ids"]) == all_ids
            assert {p["revision_id"] for p in art.metadata_json["input_revision_provenance"]} == all_ids
            assert link["summary"] == "Complete explanation with prerequisites, result, and cautions."
        usages = list(s.scalars(select(ModelUsage).where(ModelUsage.project_id == pid)))
        assert len(usages) == 2 and all(u.state == "succeeded" for u in usages)
        assert sum(u.input_tokens for u in usages) == 501
        assert sum(u.output_tokens for u in usages) == 201


def test_shared_input_policy_change_discards_every_late_batch_result(materials, monkeypatch):
    service, owner, pid, aid, a, forms, chosen = installed(materials)
    calls = []

    def send(cfg, body):
        calls.append(body)
        with service.db() as s:
            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
            policy.settings = {**policy.settings, "allow_external": False}
        return provider(body)

    monkeypatch.setattr(ai_gateway, "request_model", send)
    with pytest.raises(DomainError):
        normal_runs(a, owner, pid, forms)
    assert len(calls) == 1
    assert not InteractiveKnowledgeService(service).bindings(owner, pid, aid)


def test_explicit_batch_input_allowance_aggregates_individual_bounds_without_trimming():
    calls = []
    full = "evidence " * 150
    bodies = [request_body(0, content=full), request_body(1, content=full)]

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2, max_input_bytes=8000)
    results = parallel(batch, bodies, bound=4000)
    assert len(calls) == 1
    assert all(decoded(result, "responses")["confidence"] == 0.95 for result in results)
    data = json.loads(messages(calls[0])[1]["content"])
    assert len(data["targets"]) == 2
    assert all(c["content"] == full for c in data["chapter_pool"].values())


def test_remote_json_cannot_override_input_provenance(materials, monkeypatch):
    service, owner, pid, aid, a, forms, chosen = installed(materials)

    def send(cfg, body):
        result = dict(provider(body))
        result["_vks_batch_input_revision_ids"] = []
        return result

    monkeypatch.setattr(ai_gateway, "request_model", send)
    saved = a.run(owner, pid, forms[0])
    assert saved["saved"]
    link = InteractiveKnowledgeService(service).bindings(owner, pid, aid)[0]
    with service.db() as s:
        art = s.get(Artifact, s.get(Revision, link["point_revision_id"]).artifact_id)
        assert art.metadata_json["interactive_generation"]["input_revision_ids"] == [
            chosen[forms[0].target.label]["revision_id"]
        ]


def test_cohort_waits_for_slow_preparation_without_splitting_successful_requests():
    from threading import Event

    sent = Event()
    entered = Event()
    calls = []

    def send(cfg, body):
        calls.append(body)
        sent.set()
        return provider(body)

    batch = AssociationBatch(
        Actor("actor", frozenset()), "project", send, max_items=2, gather_seconds=0.001, participants=2
    )

    def worker(index):
        try:
            entered.set()
            return batch.request(
                config(), request_body(index), preflight=lambda: None,
                candidates=["revision-" + str(index)], page=["same"],
            )
        finally:
            batch.finish_participant()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, 0)
        assert entered.wait(5)
        assert not sent.wait(0.05), "A known preparing peer must not be split by the default gather timeout"
        second = pool.submit(worker, 1)
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert len(calls) == 1
    assert len(json.loads(messages(calls[0])[1]["content"])["targets"]) == 2
    assert [decoded(r, "responses")["section_revision_ids"] for r in results] == [["revision-0"], ["revision-1"]]


@pytest.mark.parametrize("peer_failure", [False, True])
def test_cohort_finished_manual_or_failed_peer_releases_waiting_request(peer_failure):
    from threading import Event

    entered = Event()
    calls = []

    def send(cfg, body):
        calls.append(body)
        return provider(body)

    batch = AssociationBatch(Actor("actor", frozenset()), "project", send, max_items=2, participants=2)

    def model_worker():
        try:
            entered.set()
            return batch.request(
                config(), request_body(), preflight=lambda: None, candidates=["revision-0"], page=["same"]
            )
        finally:
            batch.finish_participant()

    def no_model_worker():
        try:
            if peer_failure:
                raise DomainError("VKS_STATE_CONFLICT", "Preparation failed", 409)
        finally:
            batch.finish_participant()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(model_worker)
        assert entered.wait(5)
        second = pool.submit(no_model_worker)
        if peer_failure:
            with pytest.raises(DomainError):
                second.result(timeout=5)
        else:
            second.result(timeout=5)
        assert decoded(first.result(timeout=5), "responses")["section_revision_ids"] == ["revision-0"]
    assert len(calls) == 1
    assert "targets" not in json.loads(messages(calls[0])[1]["content"])


def test_cohort_finished_peer_still_checks_cancellation_and_sends_nothing():
    from threading import Event

    entered = Event()
    calls = []
    batch = AssociationBatch(
        Actor("actor", frozenset()), "project", lambda cfg, body: calls.append(body), participants=2
    )

    def cancelled():
        raise DomainError("VKS_CANCELLED", "Cancelled while peer prepared", 409)

    def worker():
        try:
            entered.set()
            return batch.request(config(), request_body(), preflight=cancelled, candidates=["revision-0"], page=["same"])
        finally:
            batch.finish_participant()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        pool.submit(batch.finish_participant).result(timeout=5)
        with pytest.raises(DomainError) as caught:
            future.result(timeout=5)
    assert caught.value.code == "VKS_CANCELLED"
    assert calls == []


def test_cohort_thread_reuse_and_different_page_keys_do_not_strand_workers():
    calls = []
    batch = AssociationBatch(
        Actor("actor", frozenset()), "project", lambda cfg, body: calls.append(body) or provider(body),
        max_items=2, participants=2,
    )
    # A skipped first group may finish before the pool starts its second group.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(batch.finish_participant).result(timeout=5)
        result = pool.submit(
            batch.request, config(), request_body(), preflight=lambda: None,
            candidates=["revision-0"], page=["same"],
        ).result(timeout=5)
        pool.submit(batch.finish_participant).result(timeout=5)
    assert decoded(result, "responses")["section_revision_ids"] == ["revision-0"]
    assert len(calls) == 1
    calls.clear()
    other = AssociationBatch(
        Actor("actor", frozenset()), "project", lambda cfg, body: calls.append(body) or provider(body),
        max_items=2, participants=2,
    )
    results = parallel(other, [request_body(0), request_body(1)], pages=[["one"], ["two"]])
    assert len(calls) == 2
    assert [decoded(r, "responses")["section_revision_ids"] for r in results] == [["revision-0"], ["revision-1"]]


@pytest.mark.parametrize("targets,timeout", [(1, 30), (2, 60), (6, 120)])
def test_batch_transport_wait_scales_without_changing_each_full_answer_contract(targets, timeout):
    calls = []

    def send(cfg, body):
        calls.append((cfg, body))
        return provider(body)

    batch = AssociationBatch(
        Actor("actor", frozenset()), "project", send, max_items=targets, gather_seconds=0.001
    )
    bodies = [request_body(index) for index in range(targets)]
    original = deepcopy(bodies)
    results = parallel(batch, bodies)
    assert len(calls) == 1
    sent_config, sent_body = calls[0]
    assert sent_config.get("_request_timeout_seconds", 30) == timeout
    assert "_request_timeout_seconds" not in sent_body
    assert bodies == original
    assert sent_body["max_output_tokens"] == targets * 2000
    assert len(results) == targets
    assert all(usage(result)[0] >= 0 for result in results)


@pytest.mark.parametrize("timeout", [None, False, True, 29, 901, "60", 60.0])
def test_gateway_rejects_untrusted_timeout_before_any_transport(monkeypatch, timeout):
    from vks_api.ai_gateway import ModelNotSent

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid transient timeout reached network setup")

    monkeypatch.setattr(ai_gateway, "endpoint_policy", forbidden)
    monkeypatch.setattr(ai_gateway.http.client, "HTTPConnection", forbidden)
    with pytest.raises(ModelNotSent) as caught:
        ai_gateway.request_model({"_request_timeout_seconds": timeout}, {"unchanged": True})
    assert caught.value.code == "VKS_INVALID_INPUT"


@pytest.mark.parametrize("hint,expected", [(None, 30), (60, 60), (120, 120)])
def test_gateway_uses_bounded_timeout_and_preserves_wire_body(monkeypatch, hint, expected):
    from vks_api import model_credentials

    sent = []
    endpoint = SimpleNamespace(scheme="http", hostname="127.0.0.1", path="/v1/responses")
    policy = SimpleNamespace(resolve=lambda url: (endpoint, "127.0.0.1", 8080))
    monkeypatch.setattr(ai_gateway, "endpoint_policy", lambda config: (policy, "http://127.0.0.1:8080/v1/responses"))
    monkeypatch.setattr(model_credentials, "key_for_ref", lambda *args: None)

    class Connection:
        def __init__(self, ip, port, *, timeout):
            assert (ip, port, timeout) == ("127.0.0.1", 8080, expected)

        def request(self, method, path, *, body, headers):
            sent.append((method, path, body))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda bound: b'{"ok":true}')

        def close(self):
            pass

    monkeypatch.setattr(ai_gateway.http.client, "HTTPConnection", Connection)
    cfg = {"endpoint": "http://127.0.0.1:8080/v1"}
    if hint is not None:
        cfg["_request_timeout_seconds"] = hint
    body = request_body()
    assert ai_gateway.request_model(cfg, body) == {"ok": True}
    assert sent == [("POST", "/v1/responses", json.dumps(body, ensure_ascii=False).encode())]
