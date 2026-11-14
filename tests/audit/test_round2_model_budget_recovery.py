"""Trusted not-sent recovery and conservative model-call boundaries.

Synthetic SQLite, DNS and HTTP fixtures only; no external provider is contacted.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_platform_design_alignment import model_input
from vks_api import ai_gateway as gateway
from vks_api.ai_gateway import AiService, ModelLedger, ModelNotSent, ModelPolicy, ModelUsage, PolicyInput
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Analysis, Artifact, Job
from vks_api.service import DomainError, future

KINDS = ["knowledge_plan", "document_draft"]
LOCAL_CASES = [
    "allowlist",
    "invalid_endpoint",
    "invalid_origin",
    "query",
    "remote_http",
    "external_disabled",
    "invalid_cidr",
    "empty_cidr",
    "too_many_cidrs",
    "loopback_disabled",
    "control_port",
    "dns_failure",
    "dns_empty",
    "metadata_ip",
    "outside_cidr",
    "invalid_ip",
    "missing_credentials",
]
RECOVERABLE_CASES = [
    "allowlist",
    "invalid_origin",
    "invalid_cidr",
    "empty_cidr",
    "too_many_cidrs",
    "loopback_disabled",
    "dns_failure",
    "dns_empty",
    "metadata_ip",
    "outside_cidr",
    "invalid_ip",
    "missing_credentials",
]


def configure_local(monkeypatch, case=None):
    """Exercise real endpoint_policy/NetworkPolicy branches with local DNS."""
    config = PolicyInput(
        enabled=True,
        endpoint="http://127.0.0.1:32991/v1",
        model_id="budget-fixture",
        secret_ref="model-default",
        allow_source=True,
        max_input_tokens=100000,
    ).model_dump()
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", config["endpoint"])
    monkeypatch.setenv("VKS_MODEL_ALLOWED_CIDRS", "0.0.0.0/0,::/0")
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_MODEL_API_KEY", "synthetic-local-secret")
    address = "127.0.0.1"
    if case == "allowlist":
        monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", "")
    elif case == "invalid_endpoint":
        config["endpoint"] = "http://user:secret@127.0.0.1:32991/v1"
    elif case == "invalid_origin":
        monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", "not-an-origin")
    elif case == "query":
        config["endpoint"] += "?credential=forbidden"
    elif case == "remote_http":
        config.update(endpoint="http://model.invalid:32991/v1", allow_external=True)
        monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", config["endpoint"])
    elif case == "external_disabled":
        config["endpoint"] = "https://model.invalid/v1"
        monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", config["endpoint"])
    elif case in ("invalid_cidr", "empty_cidr", "too_many_cidrs"):
        values = {"invalid_cidr": "bad-cidr", "empty_cidr": "", "too_many_cidrs": ",".join(["127.0.0.0/8"] * 21)}
        monkeypatch.setenv("VKS_MODEL_ALLOWED_CIDRS", values[case])
    elif case == "loopback_disabled":
        monkeypatch.delenv("VKS_MODEL_ALLOW_LOOPBACK")
    elif case == "control_port":
        config["endpoint"] = "http://127.0.0.1:5432/v1"
        monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", config["endpoint"])
    elif case == "metadata_ip":
        address = "169.254.169.254"
    elif case == "outside_cidr":
        monkeypatch.setenv("VKS_MODEL_ALLOWED_CIDRS", "192.0.2.0/24")
    elif case == "invalid_ip":
        address = "invalid-address"
    elif case == "missing_credentials":
        monkeypatch.delenv("VKS_MODEL_API_KEY")

    def resolve(*args, **kwargs):
        if case == "dns_failure":
            raise socket.gaierror("Synthetic resolution failure")
        if case == "dns_empty":
            return []
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 32991))]

    monkeypatch.setattr("services.browser_runner.network.socket.getaddrinfo", resolve)
    return config


def make_task(c, kind, key="budget-boundary"):
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(
        c.owner,
        c.p,
        TaskInput(
            analysis_set_id=c.aid,
            task_kind=kind,
            instruction="Synthetic fixed scope",
            max_model_calls=1,
        ).model_dump(),
        key,
    )
    return tasks, job


def local_fixture_transport(c, monkeypatch, *, case=None, failure=None):
    actual = gateway.request_model
    _, _, sent = model_input(c, monkeypatch, document=False)
    # The caller chooses a task kind through the request's actual schema.
    plan_reply = gateway.request_model
    config = configure_local(monkeypatch, case)
    with c.s.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
        policy.settings = config
    constructed, attempted = [], []

    class Connection:
        def __init__(self, *args, **kwargs):
            constructed.append(True)

        def request(self, method, path, body, headers):
            attempted.append(True)
            if isinstance(failure, Exception):
                raise failure
            body = json.loads(body)
            if body["text"]["format"]["name"] == "vks_document":
                context = json.loads(body["input"][1]["content"])
                answer = {
                    "title": "Synthetic draft",
                    "paragraphs": [
                        {
                            "markdown": "Synthetic fixed evidence",
                            "evidence_ids": [context["evidence"][0]["evidence_id"]],
                        }
                    ],
                    "unknowns": [],
                }
                sent.append(context)
                self.payload = {
                    "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
                }
            else:
                self.payload = plan_reply(config, body)
            if failure == "missing_usage":
                self.payload.pop("usage")
            if failure == "bad_citation":
                answer = json.loads(self.payload["output"][0]["content"][0]["text"])
                if "steps" in answer:
                    answer["steps"][0]["evidence_ids"] = ["not-an-evidence"]
                else:
                    answer["paragraphs"][0]["evidence_ids"] = ["not-an-evidence"]
                self.payload["output"][0]["content"][0]["text"] = json.dumps(answer)

        def getresponse(self):
            raw = b"bad-json" if failure == "bad_json" else json.dumps(self.payload).encode()
            if failure == "oversized":
                raw = b"x" * (2 * 1024 * 1024 + 1)
            return SimpleNamespace(status=failure if type(failure) is int else 200, read=lambda limit: raw)

        def close(self):
            pass

    monkeypatch.setattr(gateway.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(gateway, "request_model", actual)
    return config, sent, constructed, attempted


def budget(c):
    with c.s.db() as s:
        ledger = s.scalar(select(ModelLedger).where(ModelLedger.project_id == c.p))
        return (
            (ledger.reserved_tokens, ledger.spent_tokens, ledger.reserved_cost_micro, ledger.spent_cost_micro)
            if ledger
            else (0, 0, 0, 0)
        )


def model_slot(c, jid):
    with c.s.db() as s:
        return bool(s.get(Job, jid).input_json.get("_model_reserved"))


@pytest.mark.parametrize("case", LOCAL_CASES)
def test_actual_local_gateway_refusals_are_typed_before_http_connection(monkeypatch, case):
    config = configure_local(monkeypatch, case)
    monkeypatch.setattr(gateway.http.client, "HTTPConnection", lambda *a, **k: pytest.fail("No transport construction"))
    with pytest.raises(ModelNotSent) as exc:
        gateway.request_model(config, {})
    assert exc.value.retryable is False


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", RECOVERABLE_CASES)
def test_actual_local_refusal_releases_cost_and_one_call_then_manual_retry(audit_store, monkeypatch, kind, case):
    c = audit_store
    config, sent, constructed, attempted = local_fixture_transport(c, monkeypatch, case=case)
    config.update(input_rate_micro_per_million=1000000, output_rate_micro_per_million=2000000)
    with c.s.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
        policy.settings = config
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    first = tasks.get(c.owner, job["id"])
    assert first["state"] == "failed" and first["error"]["retryable"] is False
    assert not sent and not constructed and not attempted
    assert budget(c) == (0, 0, 0, 0) and not model_slot(c, job["id"])
    with c.s.db() as s:
        usage = s.scalar(select(ModelUsage).where(ModelUsage.project_id == c.p))
        assert usage.state == "not_sent" and usage.cost_micro == 0
    # Only instance environment/DNS changes; policy/version/revisions stay fixed.
    configure_local(monkeypatch)
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "succeeded"
    assert len(sent) == len(constructed) == len(attempted) == 1
    assert budget(c) == (0, 150, 0, 200) and model_slot(c, job["id"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("limit", ["input", "request_cost", "daily_cost"])
def test_retired_input_and_cost_limits_do_not_reject_task_calls(audit_store, monkeypatch, kind, limit):
    c = audit_store
    config, sent, constructed, _ = local_fixture_transport(c, monkeypatch)
    config.update(input_rate_micro_per_million=1000000, output_rate_micro_per_million=1000000)
    if limit == "input":
        config["max_input_tokens"] = 1000
    elif limit == "request_cost":
        config["per_request_cost_limit_micro"] = 0
    else:
        config["daily_cost_limit_micro"] = 0
    with c.s.db() as s:
        s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p)).settings = config
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    first = tasks.get(c.owner, job["id"])
    assert first["state"] == "succeeded", first.get("error")
    assert len(sent) == len(constructed) == 1 and model_slot(c, job["id"])
    assert budget(c) == (0, 150, 0, 150)


@pytest.mark.parametrize("kind", KINDS)
def test_no_permitted_evidence_is_not_a_consumed_call(audit_store, monkeypatch, kind):
    c = audit_store
    _, sent, _, _ = local_fixture_transport(c, monkeypatch)
    with c.s.db() as s:
        prior = {a.id: a.metadata_json for a in s.scalars(select(Artifact))}
        for artifact in s.scalars(select(Artifact)):
            artifact.metadata_json = dict(artifact.metadata_json, allow_platform_llm=False)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    first = tasks.get(c.owner, job["id"])
    assert first["state"] == "failed" and first["error"]["code"] == "VKS_INVALID_EVIDENCE"
    assert budget(c) == (0, 0, 0, 0) and not model_slot(c, job["id"]) and not sent
    with c.s.db() as s:
        for aid, metadata in prior.items():
            s.get(Artifact, aid).metadata_json = metadata
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "succeeded"
    assert len(sent) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_actual_input_revocation_after_reserve_releases_only_not_sent_usage(audit_store, monkeypatch, kind):
    c = audit_store
    _, sent, constructed, _ = local_fixture_transport(c, monkeypatch)
    reserve = AiService.reserve
    revoked = []

    def reserve_then_revoke(self, *args):
        usage = reserve(self, *args)
        if not revoked:
            with c.s.db() as s:
                for artifact in s.scalars(select(Artifact)):
                    if artifact.metadata_json.get("allow_platform_llm"):
                        revoked.append(artifact.id)
                        artifact.metadata_json = dict(artifact.metadata_json, allow_platform_llm=False)
        return usage

    monkeypatch.setattr(AiService, "reserve", reserve_then_revoke)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["error"]["code"] == "VKS_FORBIDDEN"
    assert not sent and not constructed and not model_slot(c, job["id"])
    assert budget(c) == (0, 0, 0, 0)
    with c.s.db() as s:
        for aid in revoked:
            artifact = s.get(Artifact, aid)
            artifact.metadata_json = dict(artifact.metadata_json, allow_platform_llm=True)
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "succeeded"
    assert len(sent) == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "failure",
    [
        OSError("Synthetic uncertain socket"),
        ValueError("Synthetic uncertain transport"),
        DomainError("VKS_FORBIDDEN", "Same code as local policy", 403, details={"not_sent": True}),
        DomainError("VKS_LIMIT_EXCEEDED", "Same code as local budget", 429),
        DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic unknown outcome", 503, retryable=True),
        RuntimeError("Synthetic unknown transport failure"),
        429,
        500,
        502,
        503,
        504,
        302,
        401,
        "bad_json",
        "oversized",
        "missing_usage",
        "bad_citation",
    ],
)
def test_transport_started_or_untyped_failure_cannot_release_or_send_again(audit_store, monkeypatch, kind, failure):
    c = audit_store
    _, _, constructed, attempted = local_fixture_transport(c, monkeypatch, failure=failure)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    first = tasks.get(c.owner, job["id"])
    assert first["state"] == "failed"
    assert len(attempted) == len(constructed) == 1 and model_slot(c, job["id"])
    with c.s.db() as s:
        usage = s.scalar(select(ModelUsage).where(ModelUsage.project_id == c.p))
        assert usage.state == "failed" and usage.input_tokens + usage.output_tokens > 0
        reserved = usage.reservation_tokens
    if isinstance(failure, Exception) or type(failure) is int or failure in ("bad_json", "oversized", "missing_usage"):
        assert budget(c)[1] == reserved
    else:
        assert budget(c)[1] == 150
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    second = tasks.get(c.owner, job["id"])
    assert second["state"] == "failed" and second["error"]["code"] == "VKS_LIMIT_EXCEEDED"
    assert len(attempted) == len(constructed) == 1
    assert budget(c)[0] == budget(c)[2] == 0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("changed", ["policy", "baseline", "permission"])
def test_not_sent_retry_does_not_bypass_current_policy_input_or_permission(audit_store, monkeypatch, kind, changed):
    c = audit_store
    _, sent, _, _ = local_fixture_transport(c, monkeypatch, case="allowlist")
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    assert not model_slot(c, job["id"])
    configure_local(monkeypatch)
    with c.s.db() as s:
        if changed == "policy":
            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == c.p))
            policy.row_version += 1
        elif changed == "baseline":
            s.get(Analysis, c.aid).selection_version += 1
        else:
            from vks_api.models import Member

            member = s.scalar(select(Member).where(Member.project_id == c.p, Member.user_id == c.owner.id))
            member.role = "viewer"
    if changed == "permission":
        with pytest.raises(DomainError) as exc:
            c.s.retry_job(c.owner, job["id"])
        assert exc.value.code == "VKS_FORBIDDEN" and not sent
        return
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "failed" and result["report"] is None and not sent
    assert result["error"]["code"] in ("VKS_VERSION_CONFLICT", "VKS_FORBIDDEN")
    assert not model_slot(c, job["id"])


def test_old_attempt_not_sent_failure_cannot_unlock_new_attempt(audit_store, monkeypatch):
    c = audit_store
    local_fixture_transport(c, monkeypatch, case="allowlist")
    tasks, job = make_task(c, "knowledge_plan")
    with c.s.db() as s:
        stored = s.get(Job, job["id"])
        stored.state, stored.attempt, stored.lease_until = "running", 1, future(30)

    def stale_preflight():
        with c.s.db() as s:
            stored = s.get(Job, job["id"])
            stored.attempt = 2
            stored.input_json = dict(stored.input_json, _model_reserved=True)
        gateway.endpoint_policy(PolicyInput(endpoint="http://127.0.0.1:32991/v1").model_dump())

    with pytest.raises(ModelNotSent):
        tasks._model_output(job["id"], stale_preflight)
    assert model_slot(c, job["id"])


@pytest.mark.parametrize("kind", KINDS)
def test_unknown_preflight_exception_is_not_a_trusted_local_refusal(audit_store, monkeypatch, kind):
    c = audit_store
    _, sent, constructed, _ = local_fixture_transport(c, monkeypatch)
    calls = []

    def unknown_reservation(*args):
        calls.append(True)
        raise RuntimeError("Synthetic unclassified local failure")

    monkeypatch.setattr(AiService, "reserve", unknown_reservation)
    tasks, job = make_task(c, kind)
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["state"] == "failed" and model_slot(c, job["id"])
    c.s.retry_job(c.owner, job["id"])
    c.s.process_job(job["id"])
    assert tasks.get(c.owner, job["id"])["error"]["code"] == "VKS_LIMIT_EXCEEDED"
    assert len(calls) == 1 and not sent and not constructed
