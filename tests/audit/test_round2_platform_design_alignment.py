"""Round-two platform design regressions; all data is synthetic and isolated."""

import os

import pytest
from sqlalchemy import select

from test_platform_design_alignment import model_input
from vks_api.ai_gateway import AiService, ModelLedger, ModelPolicy
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput


@pytest.mark.parametrize("kind", ["knowledge_plan", "document_draft"])
def test_existing_reservations_do_not_limit_model_tasks(audit_store, monkeypatch, kind):
    c = audit_store
    _, _, sent = model_input(c, monkeypatch, document=kind == "document_draft")
    ai = AiService(c.s)
    with c.s.db() as s:
        policy = dict(s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p)).settings)
    held = ai.reserve(c.owner, c.p, policy, policy["daily_token_limit"] - policy["max_output_tokens"])
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(
        c.owner,
        c.p,
        TaskInput(analysis_set_id=c.aid, task_kind=kind, instruction="Synthetic scope", max_model_calls=1).model_dump(),
        "no-daily-quota",
    )
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "succeeded", result.get("error")
    assert len(sent) == 1
    ai.settle(held, policy, 0, 0, "succeeded")
    with c.s.db() as s:
        ledger = s.scalar(select(ModelLedger).where(ModelLedger.project_id == c.p))
        assert ledger.reserved_tokens == 0
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "succeeded" and len(sent) == 1


@pytest.mark.parametrize("case", ["outbound_allowlist", "missing_credentials"])
@pytest.mark.parametrize("kind", ["knowledge_plan", "document_draft"])
def test_round2_platform_known_not_sent_failures_release_budget_and_remain_recoverable(
    audit_store, monkeypatch, case, kind
):
    from vks_api import ai_gateway as gateway

    c = audit_store
    actual_request_model = gateway.request_model
    _, _, sent = model_input(c, monkeypatch, document=kind == "document_draft")
    fixture_transport = gateway.request_model
    endpoint = "http://127.0.0.1:32991/v1"
    with c.s.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
        policy.settings = dict(policy.settings, endpoint=endpoint, secret_ref="model-default")
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", "" if case == "outbound_allowlist" else endpoint)
    if case == "missing_credentials":
        monkeypatch.delenv("VKS_MODEL_API_KEY", raising=False)
    else:
        monkeypatch.setenv("VKS_MODEL_API_KEY", "synthetic-not-sent-secret")

    def guarded_transport(config, body):
        if case == "missing_credentials" and not os.getenv("VKS_MODEL_API_KEY"):
            # The real gateway rejects before constructing HTTPConnection.
            return actual_request_model(config, body)
        gateway.endpoint_policy(config)  # Real local allow-list preflight.
        return fixture_transport(config, body)  # Successful transport is a fixture.

    monkeypatch.setattr(gateway, "request_model", guarded_transport)
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(
        c.owner,
        c.p,
        TaskInput(
            analysis_set_id=c.aid,
            task_kind=kind,
            instruction="Synthetic scope",
            max_model_calls=1,
        ).model_dump(),
        "round2-not-sent",
    )
    c.s.process_job(job["id"])
    failed = tasks.get(c.owner, job["id"])
    assert failed["state"] in ("failed", "paused")
    assert not sent
    with c.s.db() as s:
        ledger = s.scalar(select(ModelLedger).where(ModelLedger.project_id == c.p))
        assert ledger is None or (ledger.spent_tokens == 0 and ledger.reserved_tokens == 0), (
            ledger.spent_tokens,
            ledger.reserved_tokens,
        )
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", endpoint)
    monkeypatch.setenv("VKS_MODEL_API_KEY", "synthetic-restored-secret")
    if failed["state"] == "paused":
        tasks.control(
            c.owner,
            job["id"],
            dict(action="resume", expected_state="paused"),
            failed["attempt"],
            "local-preflight-resume",
        )
    else:
        c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "succeeded"
    assert len(sent) == 1
