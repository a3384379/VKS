from dataclasses import replace
from sqlalchemy import select
import pytest

from test_model_gateway import system
from test_task_planning import create
from vks_api.journeys import JourneyService
from vks_api.knowledge_tasks import KnowledgeTaskService
from vks_api.models import Analysis, Job
from vks_api.service import DomainError


def test_confirmation_requires_exact_plan_person_and_no_generic_resume_bypass(system):
    service, actor, p, aid, _, requests, _, _ = system
    tasks, job = create(system)
    before = service.workspace(actor, p["id"], aid)
    service.process_job(job["id"])
    waiting = tasks.get(actor, job["id"])
    assert waiting["state"] == "paused" and waiting["stage"] == "PLAN_REVIEW"
    assert len(requests) == 1 and waiting["report"] is None
    assert service.workspace(actor, p["id"], aid)["relations"] == before["relations"]
    assert not JourneyService(service).list(actor, p["id"], aid)
    with pytest.raises(DomainError) as error:
        tasks.control(
            actor, job["id"], dict(action="resume", expected_state="paused"), waiting["attempt"], "cannot-bypass"
        )
    assert error.value.status == 409
    payload = dict(plan_sha256=waiting["plan_preview"]["sha256"], reason="Checked exact synthetic scope")
    with pytest.raises(DomainError) as error:
        tasks.confirm_plan(replace(actor, human=False), job["id"], payload, waiting["attempt"], "machine-confirm")
    assert error.value.status == 403
    with pytest.raises(DomainError) as error:
        tasks.confirm_plan(actor, job["id"], dict(payload, plan_sha256="0" * 64), waiting["attempt"], "wrong-digest")
    assert error.value.status == 412
    queued = tasks.confirm_plan(actor, job["id"], payload, waiting["attempt"], "confirm")
    assert tasks.confirm_plan(actor, job["id"], payload, waiting["attempt"], "confirm") == queued
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "succeeded" and len(requests) == 1


@pytest.mark.parametrize("change", ["baseline", "policy", "corrupt", "permission"])
def test_old_or_unreadable_plan_cannot_resume_or_call_model_again(system, change):
    from vks_api.ai_gateway import ModelPolicy
    from vks_api.jobs import Checkpoint

    service, actor, p, aid, _, requests, _, _ = system
    tasks, job = create(system)
    service.process_job(job["id"])
    waiting = tasks.get(actor, job["id"])
    payload = dict(plan_sha256=waiting["plan_preview"]["sha256"], reason="Exact scope was displayed")
    with service.db() as s:
        if change == "baseline":
            s.get(Analysis, aid).selection_version += 1
        elif change == "policy":
            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == p["id"]))
            policy.settings = dict(policy.settings, enabled=False)
        elif change == "corrupt":
            point = s.scalar(
                select(Checkpoint).where(Checkpoint.job_id == job["id"], Checkpoint.stage == "KNOWLEDGE_PLAN")
            )
            (service.data_dir / "objects" / point.result_json["object_key"]).write_bytes(b'{"untrusted":"broken plan"}')
    if change == "permission":
        actor = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    with pytest.raises(DomainError):
        tasks.confirm_plan(actor, job["id"], payload, waiting["attempt"], "stale-confirm")
    assert not JourneyService(service).list(system[1], p["id"], aid)
    assert len(requests) == 1
    with service.db() as s:
        assert s.get(Job, job["id"]).state == "paused"
