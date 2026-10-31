"""Independent sent-response accounting checks for the round-two budget closure.

Only isolated synthetic task stores and a local in-process provider fixture run.
"""

import pytest
from sqlalchemy import select
from test_platform_design_alignment import model_input
from vks_api import ai_gateway as gateway
from vks_api.ai_gateway import ModelLedger, ModelPolicy, ModelUsage
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Job


@pytest.mark.parametrize("kind", ["knowledge_plan", "document_draft"])
@pytest.mark.parametrize("usage", [
    {"input_tokens": -1000, "output_tokens": 1},
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": 100},
])
def test_round2_platform_sent_invalid_usage_keeps_conservative_charge_and_cannot_resend(
    audit_store, monkeypatch, kind, usage
):
    c = audit_store
    _, _, sent = model_input(c, monkeypatch, document=kind == "document_draft")
    fixture_request = gateway.request_model

    def invalid_usage_response(config, body):
        response = fixture_request(config, body)
        response["usage"] = dict(usage)
        return response

    monkeypatch.setattr(gateway, "request_model", invalid_usage_response)
    with c.s.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
        policy.settings = dict(
            policy.settings, input_rate_micro_per_million=1000000, output_rate_micro_per_million=2000000
        )
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(c.owner, c.p, TaskInput(
        analysis_set_id=c.aid, task_kind=kind, instruction="Synthetic fixed scope", max_model_calls=1,
    ).model_dump(), "round2-invalid-usage")
    c.s.process_job(job["id"])
    first = tasks.get(c.owner, job["id"])
    assert first["state"] == "failed" and first["report"] is None
    assert len(sent) == 1
    with c.s.db() as s:
        usage_row = s.scalar(select(ModelUsage).where(ModelUsage.project_id == c.p))
        ledger = s.get(ModelLedger, usage_row.ledger_id)
        actual = (
            usage_row.state, ledger.reserved_tokens, ledger.reserved_cost_micro,
            ledger.spent_tokens, ledger.spent_cost_micro,
        )
        expected = ("failed", 0, 0, usage_row.reservation_tokens, usage_row.reservation_cost_micro)
        assert actual == expected, {"provider_usage": usage, "actual": actual, "expected": expected}
        assert s.get(Job, job["id"]).input_json.get("_model_reserved") is True
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    second = tasks.get(c.owner, job["id"])
    assert second["state"] == "failed" and second["error"]["code"] == "VKS_LIMIT_EXCEEDED"
    assert len(sent) == 1
