"""Sent model usage is trusted only as a complete validated integer pair."""

import json

import pytest
from sqlalchemy import select
from test_platform_design_alignment import model_input
from test_round2_model_budget_recovery import KINDS, make_task, model_slot
from vks_api import ai_gateway as gateway
from vks_api.ai_gateway import ModelLedger, ModelPolicy, ModelUsage

INVALID_USAGE = [
    {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": -1},
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": False},
    {"input_tokens": 10**100, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 10**100},
    {"input_tokens": 1.0, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 1.0},
    {"input_tokens": "100", "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": "100"},
    {"input_tokens": 100},
    {"output_tokens": 100},
    {"input_tokens": 100, "output_tokens": None},
    {"input_tokens": None, "output_tokens": 100},
    {"prompt_tokens": 100, "completion_tokens": -1},
    {"input_tokens": None, "prompt_tokens": 100, "output_tokens": 1},
    {},
    None,
    [],
    "untrusted usage",
]


def sent_response(c, monkeypatch, kind, usage, *, protocol="responses", invalid_output=False):
    """Both wire protocols preserve the fixture's exact sent input catalog."""
    model_input(c, monkeypatch, document=kind == "document_draft")
    original = gateway.request_model
    sent = []
    with c.s.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
        policy.settings = dict(
            policy.settings,
            protocol=protocol,
            input_rate_micro_per_million=1000000,
            output_rate_micro_per_million=2000000,
        )

    def response(config, body):
        sent.append(body)
        fixture_body = (
            body
            if protocol == "responses"
            else {
                "input": body["messages"],
                "text": {"format": body["response_format"]["json_schema"]},
            }
        )
        result = original(config, fixture_body)
        if invalid_output:
            answer = json.loads(result["output"][0]["content"][0]["text"])
            if kind == "knowledge_plan":
                answer["steps"][0]["evidence_ids"] = ["untrusted-evidence"]
            else:
                answer["paragraphs"][0]["evidence_ids"] = ["untrusted-evidence"]
            result["output"][0]["content"][0]["text"] = json.dumps(answer)
        if protocol == "chat_completions":
            result = {"choices": [{"message": {"content": result["output"][0]["content"][0]["text"]}}]}
        result["usage"] = usage
        return result

    monkeypatch.setattr(gateway, "request_model", response)
    return sent


def charge(c):
    with c.s.db() as s:
        usage = s.scalar(select(ModelUsage).where(ModelUsage.project_id == c.p))
        ledger = s.get(ModelLedger, usage.ledger_id)
        return {
            "state": usage.state,
            "error": usage.error_code,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cost": usage.cost_micro,
            "reservation_tokens": usage.reservation_tokens,
            "reservation_cost": usage.reservation_cost_micro,
            "reserved_tokens": ledger.reserved_tokens,
            "reserved_cost": ledger.reserved_cost_micro,
            "spent_tokens": ledger.spent_tokens,
            "spent_cost": ledger.spent_cost_micro,
        }


def assert_no_resend(c, tasks, job, sent):
    assert len(sent) == 1 and model_slot(c, job["id"])
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    second = tasks.get(c.owner, job["id"])
    assert second["state"] == "failed" and second["error"]["code"] == "VKS_LIMIT_EXCEEDED"
    assert len(sent) == 1 and model_slot(c, job["id"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("usage", INVALID_USAGE)
def test_invalid_or_missing_sent_usage_settles_full_reservation_and_retains_one_call(
    audit_store,
    monkeypatch,
    kind,
    usage,
):
    c = audit_store
    sent = sent_response(c, monkeypatch, kind, usage)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_INVALID_INPUT"
    assert result["report"] is None
    settled = charge(c)
    assert settled["state"] == "failed" and settled["error"] == "VKS_INVALID_INPUT"
    assert settled["reserved_tokens"] == settled["reserved_cost"] == 0
    assert settled["input"] + settled["output"] == settled["spent_tokens"] == settled["reservation_tokens"]
    assert settled["cost"] == settled["spent_cost"] == settled["reservation_cost"] > 0
    assert_no_resend(c, tasks, job, sent)
    assert charge(c) == settled


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"input_tokens": 321, "output_tokens": 17}, (321, 17)),
        ({"prompt_tokens": 321, "completion_tokens": 17}, (321, 17)),
        ({"input_tokens": 0, "output_tokens": 0}, (0, 0)),
    ],
)
def test_valid_sent_usage_still_charges_exact_pair_on_invalid_output(
    audit_store,
    monkeypatch,
    kind,
    protocol,
    usage,
    expected,
):
    c = audit_store
    sent = sent_response(c, monkeypatch, kind, usage, protocol=protocol, invalid_output=True)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_INVALID_EVIDENCE"
    settled = charge(c)
    assert (settled["input"], settled["output"]) == expected
    assert settled["cost"] == settled["spent_cost"] == expected[0] + 2 * expected[1]
    assert settled["spent_tokens"] == sum(expected)
    assert settled["state"] == "failed" and settled["reserved_tokens"] == settled["reserved_cost"] == 0
    assert_no_resend(c, tasks, job, sent)
    assert charge(c) == settled


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 100001, "output_tokens": 1},
        {"input_tokens": 321, "output_tokens": 2001},
        {"input_tokens": 1000000000, "output_tokens": 1},
    ],
)
def test_valid_usage_above_retired_limits_is_recorded_without_rejecting_result(
    audit_store,
    monkeypatch,
    kind,
    usage,
):
    c = audit_store
    sent = sent_response(c, monkeypatch, kind, usage)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "succeeded", result.get("error")
    settled = charge(c)
    assert (settled["input"], settled["output"]) == (usage["input_tokens"], usage["output_tokens"])
    assert settled["spent_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert settled["spent_cost"] == usage["input_tokens"] + 2 * usage["output_tokens"]
    assert settled["state"] == "succeeded" and settled["reserved_tokens"] == settled["reserved_cost"] == 0
    c.s.process_job(job["id"])
    assert len(sent) == 1
    assert charge(c) == settled
