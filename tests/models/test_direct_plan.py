from test_model_gateway import system as system  # pytest fixture registration
from test_task_planning import create
from vks_api.journeys import JourneyService


def test_plan_executes_once_without_confirmation_and_can_be_read_immediately(system):
    service, actor, project, aid, _, requests, _, _ = system
    tasks, job = create(system)
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result.get("error")
    assert "plan_confirmation_required" not in result
    assert result["stage_history"][-1]["stage"] == "Save"
    assert JourneyService(service).list(actor, project["id"], aid)
    service.process_job(job["id"])
    assert len(requests) == 1


def test_authorized_machine_can_execute_the_same_plan(system):
    service, actor, project, aid, _, requests, _, _ = system
    values = list(system)
    token = service.create_token(
        actor,
        project["id"],
        {
            "name": "plan-client",
            "scopes": [
                "knowledge:read",
                "knowledge:edit",
                "analysis:run",
                "links:propose",
                "source:read",
                "trace:read",
            ],
        },
    )
    values[1] = service.authenticate(token["token"], "token")
    tasks, job = create(tuple(values))
    service.process_job(job["id"])
    result = tasks.get(values[1], job["id"])
    assert result["state"] == "succeeded", result.get("error")
    assert len(requests) == 1
