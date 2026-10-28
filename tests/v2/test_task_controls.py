import pytest

from test_journeys import store
from vks_api.journeys import JourneyService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.service import DomainError
from vks_api.task_checkpoints import TaskCheckpoints


def ordinary(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        actor,
        pid,
        TaskInput(
            analysis_set_id=w["selected_analysis_set"]["id"],
            task_kind="business_flow",
            action_ids=[w["actions"][0]["id"]],
            selection_version=w["selected_analysis_set"]["selection_version"],
        ).model_dump(),
        "control-flow",
    )
    return tasks, job


def test_queued_pause_resume_exact_state_idempotency_and_cancel(store):
    service, actor, pid, w, _, _ = store
    tasks, job = ordinary(store)
    request = dict(action="pause", expected_state="queued")
    paused = tasks.control(actor, job["id"], request, 0, "pause")
    assert paused["state"] == "paused"
    assert tasks.control(actor, job["id"], request, 0, "pause") == paused
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "paused"
    assert not JourneyService(service).list(actor, pid, w["selected_analysis_set"]["id"])
    with pytest.raises(DomainError) as error:
        tasks.control(actor, job["id"], request, 0, "stale-state")
    assert error.value.status == 412
    tasks.control(actor, job["id"], dict(action="resume", expected_state="paused"), 0, "resume")
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "succeeded"


def test_pause_at_stage_boundary_resumes_checkpoints_without_duplicate_candidates(store, monkeypatch):
    service, actor, pid, w, _, _ = store
    tasks, job = ordinary(store)
    original = TaskCheckpoints._complete
    seen = []

    def pause_after_collect(self, stage, cached):
        original(self, stage, cached)
        if stage == "Collect" and not seen:
            seen.append(stage)
            tasks.control(actor, job["id"], dict(action="pause", expected_state="running"), 1, "boundary-pause")

    monkeypatch.setattr(TaskCheckpoints, "_complete", pause_after_collect)
    service.process_job(job["id"])
    paused = tasks.get(actor, job["id"])
    assert paused["state"] == "paused" and paused["stage"] == "PAUSED"
    assert not JourneyService(service).list(actor, pid, w["selected_analysis_set"]["id"])
    tasks.control(
        actor, job["id"], dict(action="resume", expected_state="paused"), paused["attempt"], "boundary-resume"
    )
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded"
    assert any(row["stage"] == "Collect" and row.get("cached") == "true" for row in result["stage_history"])
    assert len(JourneyService(service).list(actor, pid, w["selected_analysis_set"]["id"])) == 1


def test_paused_job_can_be_cancelled_and_cannot_be_resumed_with_stale_authority(store):
    service, actor, _, _, _, _ = store
    tasks, job = ordinary(store)
    tasks.control(actor, job["id"], dict(action="pause", expected_state="queued"), 0, "pause")
    service.cancel_job(actor, job["id"])
    assert tasks.get(actor, job["id"])["state"] == "cancelled"
    with pytest.raises(DomainError):
        tasks.control(actor, job["id"], dict(action="resume", expected_state="paused"), 0, "resume-cancelled")


def test_task_creation_rejects_changed_scope_before_any_job_is_created(store):
    service, actor, pid, w, _, _ = store
    payload = TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"],
        task_kind="project_overview",
        selection_version=w["selected_analysis_set"]["selection_version"] + 1,
    ).model_dump()
    with pytest.raises(DomainError) as error:
        KnowledgeTaskService(service).create(actor, pid, payload, "stale-create")
    assert error.value.status == 412
