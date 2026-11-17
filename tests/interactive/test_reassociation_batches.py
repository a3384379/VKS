"""Preview jobs leave bindings unchanged; explicit atomic commit selects drafts."""

from vks_api.jobs import execute_job
from vks_api.models import Artifact, Job
from vks_api.service import DomainError

from tests.interactive import test_assistant, test_reassociation_preview
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
root = test_assistant.root


def post(web, pid, grant, path, body):
    headers = _headers(grant, 0, path)
    headers.pop("If-Match", None)
    response = web.post(root(pid) + path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def get(web, pid, grant, path, **params):
    response = web.get(root(pid) + path, params=params, headers=_headers(grant, 0, "read"))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_batch_preview_regenerate_remove_and_atomic_save(assistant, monkeypatch):
    (web, service, _owner, pid, aid, grant, _, _, _), body, calls = assistant
    other = {**body, "target": {**body["target"], "selector": "#other", "label": "Other"}}
    before = test_reassociation_preview.points(service)
    receipt = post(
        web,
        pid,
        grant,
        "/reassociate/batch-preview",
        {
            "analysis_set_id": aid,
            "instruction": "本轮共同方向",
            "items": [body, other],
            "concurrency": 1,
        },
    )
    pending = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert receipt["total"] == 2 and all(row["status"] == "pending" for row in pending["items"])
    execute_job(service, receipt["job_id"])
    data = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"], limit=1)
    assert data["state"] == "succeeded" and data["total"] == 2 and data["processed"] == 2
    assert data["next_offset"] == 1 and len(calls) == 2
    assert all(call["writing_instruction"] == "本轮共同方向" for call in calls)
    assert not test_reassociation_preview.bindings(web, pid, aid, grant)
    assert test_reassociation_preview.points(service) == before
    row = data["items"][0]
    assert "explanation" not in row and "sections" not in row and row["can_confirm"]
    detail = get(web, pid, grant, "/reassociate/previews/" + row["preview_id"])
    assert detail["explanation"] and detail["saved"] is False
    replacement = post(web, pid, grant, "/reassociate", {**row["request"], "instruction": "仅重生成第一项"})
    refreshed = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert refreshed["items"][0]["preview_id"] == replacement["preview_id"] != row["preview_id"]
    assert len(calls) == 3
    assert calls[-1]["writing_instruction"] == "仅重生成第一项"
    # Removing a row is local to this request: one ID is committed, the other
    # target remains unlinked and its immutable preview stays readable.
    commit = {"job_id": receipt["job_id"], "preview_ids": [replacement["preview_id"]]}
    saved = post(web, pid, grant, "/reassociate/batch-confirm", commit)
    assert saved["saved"] and saved["count"] == 1 and len(calls) == 3
    assert len(test_reassociation_preview.bindings(web, pid, aid, grant)) == 1
    assert post(web, pid, grant, "/reassociate/batch-confirm", commit) == saved
    assert get(web, pid, grant, "/reassociate/previews/" + refreshed["items"][1]["preview_id"])


def test_batch_confirm_rolls_back_all_on_second_write(assistant, monkeypatch):
    from vks_api.interactive_ai import InteractiveAssistant

    (web, service, _, pid, aid, grant, _, _, _), body, calls = assistant
    receipt = post(
        web,
        pid,
        grant,
        "/reassociate/batch-preview",
        {
            "analysis_set_id": aid,
            "items": [body, {**body, "target": {**body["target"], "selector": "#second"}}],
            "concurrency": 1,
        },
    )
    execute_job(service, receipt["job_id"])
    data = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert data["state"] == "succeeded", data
    Knowledge = type(InteractiveAssistant(service).links)
    original, writes = Knowledge.save, []

    def fail_second(self, *args, **kwargs):
        writes.append(True)
        if len(writes) == 2:
            raise DomainError("VKS_STATE_CONFLICT", "Synthetic conflict", 409)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Knowledge, "save", fail_second)
    response = web.post(
        root(pid) + "/reassociate/batch-confirm",
        json={"job_id": receipt["job_id"], "preview_ids": [r["preview_id"] for r in data["items"]]},
        headers=_headers(grant, 0, "atomic"),
    )
    assert response.status_code == 409, response.text
    assert not test_reassociation_preview.bindings(web, pid, aid, grant) and len(calls) == 2


def test_targets_metadata_and_cancelled_job_never_generate(assistant):
    (web, service, _, pid, aid, grant, _, _, _), body, calls = assistant
    targets = get(
        web, pid, grant, "/reassociate/targets", analysis_set_id=aid, module_only="false", mode="missing", limit=1
    )
    assert targets["total"] >= 1 and len(targets["items"]) == 1
    assert set(targets["items"][0]) == {"item_id", "label", "module_label", "state", "request"}
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "items": [body]})
    with service.db() as s:
        job = s.get(Job, receipt["job_id"])
        job.cancel_requested = True
    execute_job(service, receipt["job_id"])
    data = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert data["state"] == "cancelled" and calls == []
    assert not test_reassociation_preview.bindings(web, pid, aid, grant)


def test_recovered_batch_reuses_committed_previews_without_models(assistant):
    (web, service, _, pid, aid, grant, _, _, _), body, calls = assistant
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "items": [body]})
    execute_job(service, receipt["job_id"])
    first = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert len(calls) == 1 and first["state"] == "succeeded"
    with service.db() as s:
        job = s.get(Job, receipt["job_id"])
        job.state, job.stage, job.result_json = "queued", "RECOVERED", {}
    execute_job(service, receipt["job_id"])
    second = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert second["items"][0]["preview_id"] == first["items"][0]["preview_id"] and len(calls) == 1


def test_preview_list_counts_and_job_results_recheck_input_permissions(assistant):
    (web, service, _, pid, aid, grant, _, section, _), body, calls = assistant
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "items": [body]})
    execute_job(service, receipt["job_id"])
    data = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert data["total"] == 1
    from vks_api.models import Revision

    with service.db() as s:
        artifact = s.get(Artifact, s.get(Revision, section["revision_id"]).artifact_id)
        artifact.sensitivity = "confidential"
    hidden = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert hidden["total"] == 0 and hidden["processed"] == 0 and hidden["items"] == []
    from vks_api.reading import ReadingService

    actor = service.authenticate(grant["token"], "token")
    with service.db() as s:
        dto = ReadingService(service).job_dto(s, actor, s.get(Job, receipt["job_id"]))
        assert "result" not in dto and dto["result_refs"] == []
    cancelled = service.cancel_job(actor, receipt["job_id"])
    assert "result" not in cancelled and cancelled["result_refs"] == []
    with service.db() as s:
        s.get(Job, receipt["job_id"]).state = "failed"
    retried = service.retry_job(actor, receipt["job_id"])
    assert "result" not in retried and retried["result_refs"] == []
    with service.db() as s:
        s.get(Job, receipt["job_id"]).state = "failed"
    response = web.post(
        root(pid) + "/reassociate/batch-confirm",
        json={"job_id": receipt["job_id"], "preview_ids": [data["items"][0]["preview_id"]]},
        headers=_headers(grant, 0, "hidden"),
    )
    assert response.status_code == 404 and len(calls) == 1


def test_cancel_during_model_response_discards_candidate_and_keeps_old_binding(assistant, monkeypatch):
    from vks_api import ai_gateway

    (web, service, _, pid, aid, grant, _, _, _), body, calls = assistant
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "items": [body]})
    original = ai_gateway.request_model

    def cancel_before_return(config, payload):
        answer = original(config, payload)
        with service.db() as s:
            s.get(Job, receipt["job_id"]).cancel_requested = True
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", cancel_before_return)
    execute_job(service, receipt["job_id"])
    result = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert result["state"] == "cancelled" and len(calls) == 1
    assert result["items"][0]["preview_id"] is None
    assert not test_reassociation_preview.bindings(web, pid, aid, grant)


def test_inventory_module_only_preview_uses_exact_targets(assistant):
    (web, _service, _, pid, aid, grant, _, _, _), _, calls = assistant
    targets = get(web, pid, grant, "/reassociate/targets", analysis_set_id=aid)
    assert targets["items"] and all(row["request"]["target"]["selector"] == "body" for row in targets["items"])
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "module_only": True})
    pending = get(web, pid, grant, "/reassociate/batch-preview/" + receipt["job_id"])
    assert {row["item_id"] for row in pending["items"]} == {row["item_id"] for row in targets["items"]}
    assert calls == []


def test_module_target_search_keeps_exact_identity_and_pagination(assistant):
    (web, _service, _, pid, aid, grant, _, _, _), _, calls = assistant
    targets = get(web, pid, grant, "/reassociate/targets", analysis_set_id=aid)
    first = targets["items"][0]
    matched = get(web, pid, grant, "/reassociate/targets", analysis_set_id=aid, q=first["label"], limit=1)
    assert matched["total"] >= 1 and len(matched["items"]) == 1
    assert matched["items"][0]["request"]["target"]["selector"] == "body"
    assert any(
        row["item_id"] == matched["items"][0]["item_id"] and row["request"] == matched["items"][0]["request"]
        for row in targets["items"]
    )
    assert get(web, pid, grant, "/reassociate/targets", analysis_set_id=aid, q="no-such-module-value")["total"] == 0
    assert calls == []


def test_preview_shares_capacity_with_existing_association_jobs(assistant):
    from vks_api.association_limits import GLOBAL_REQUEST_LIMIT, active_request_slots
    from vks_api.service import future

    (web, service, owner, pid, aid, grant, _, _, _), body, calls = assistant
    receipt = post(web, pid, grant, "/reassociate/batch-preview", {"analysis_set_id": aid, "items": [body]})
    with service.db() as s:
        occupied = service._new_job(s, owner, pid, "associate_all", {"concurrency": 3})
        occupied.state, occupied.lease_until = "running", future(60)
        occupied_id = occupied.id
    execute_job(service, receipt["job_id"])
    with service.db() as s:
        pending = s.get(Job, receipt["job_id"])
        assert pending.state == "queued" and pending.attempt == 0 and calls == []
        assert active_request_slots(s) == GLOBAL_REQUEST_LIMIT
        s.get(Job, occupied_id).state = "succeeded"
    execute_job(service, receipt["job_id"])
    assert len(calls) == 1
