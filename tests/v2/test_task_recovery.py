from collections import Counter
import pytest
from sqlalchemy import select
from test_journeys import store
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.jobs import Checkpoint
from vks_api.service import DomainError


def test_retry_resumes_completed_stages_without_recollecting_or_duplicate_candidates(store, monkeypatch):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"], task_kind="business_flow", action_ids=[w["actions"][0]["id"]]
    ).model_dump()
    counts = Counter()
    for name in ("_collect", "_analyze", "_propose"):
        method = getattr(KnowledgeTaskService, name)

        def wrapped(self, *args, _name=name, _method=method, **kwargs):
            counts[_name] += 1
            if _name == "_propose" and counts[_name] == 1:
                raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic interrupted stage", 503, retryable=True)
            return _method(self, *args, **kwargs)

        monkeypatch.setattr(KnowledgeTaskService, name, wrapped)
    job = tasks.create(actor, pid, payload, "resume")
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "failed"
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert counts == {"_collect": 1, "_analyze": 1, "_propose": 2}
    assert [r["stage"] for r in result["stage_history"] if r.get("cached") == "true"] == ["Collect", "Analyze"]
    assert (
        sum(e["type"] == "journey" for e in service.workspace(actor, pid, payload["analysis_set_id"])["entities"]) == 1
    )


def test_corrupt_stage_checkpoint_rebuilds_from_authorized_fixed_inputs(store, monkeypatch):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    original = tasks._propose
    monkeypatch.setattr(
        KnowledgeTaskService,
        "_propose",
        lambda *a, **k: (_ for _ in ()).throw(DomainError("VKS_INTERNAL_ERROR", "Synthetic stage failure", 500)),
    )
    job = tasks.create(
        actor,
        pid,
        TaskInput(analysis_set_id=w["selected_analysis_set"]["id"], task_kind="project_overview").model_dump(),
        "corrupt",
    )
    service.process_job(job["id"])
    with service.db() as s:
        point = s.scalar(
            select(Checkpoint).where(Checkpoint.job_id == job["id"], Checkpoint.stage == "KNOWLEDGE_COLLECT")
        )
        assert point
        (service.data_dir / "objects" / point.result_json["object_key"]).write_bytes(b'{"not":"a trusted checkpoint"}')
    monkeypatch.setattr(KnowledgeTaskService, "_propose", lambda self, *a, **k: original(*a, **k))
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    latest_collect = [r for r in result["stage_history"] if r["stage"] == "Collect"][-1]
    assert latest_collect.get("cached") != "true"


def test_completed_stage_does_not_bypass_current_baseline_or_running_cancel(store, monkeypatch):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    original = KnowledgeTaskService._propose

    def cancel(self, jid, *args, **kwargs):
        self.service.cancel_job(actor, jid)
        return original(self, jid, *args, **kwargs)

    monkeypatch.setattr(KnowledgeTaskService, "_propose", cancel)
    job = tasks.create(
        actor,
        pid,
        TaskInput(
            analysis_set_id=w["selected_analysis_set"]["id"],
            task_kind="business_flow",
            action_ids=[w["actions"][0]["id"]],
        ).model_dump(),
        "cancel-running",
    )
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "cancelled"
    assert not any(e["type"] == "journey" for e in service.workspace(actor, pid)["entities"])


def test_stage_artifacts_bound_large_bodies_recheck_current_acl_and_reject_corruption(store):
    from dataclasses import replace
    from vks_api.models import Job, Revision

    tasks = KnowledgeTaskService(store[0])
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    payload = TaskInput(analysis_set_id=aid, task_kind="evidence_gaps", max_entities=1).model_dump()
    with service.db() as db:
        db.get(Revision, w["actions"][0]["entity_revision_id"]).content = "Synthetic body " * 100000
    collected = tasks._collect(actor, pid, aid, payload)
    assert not any("content" in row for row in collected["actions"])
    assert collected["_scope"]["actions"]["total"] == len(w["actions"])
    assert collected["_scope"]["actions"]["included"] == 1
    assert collected["_scope"]["actions"]["is_truncated"]
    job = tasks.create(actor, pid, payload, "stage-read")
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    point = next(row for row in result["stage_artifacts"] if row["stage"] == "Collect")
    artifact = tasks.stage_artifact(actor, job["id"], point["sha256"])
    assert artifact["result"]["_scope"]["actions"]["total"] == len(w["actions"])
    limited = replace(actor, scopes=actor.scopes - {"source:read"})
    assert tasks.get(limited, job["id"])["stage_artifacts"] == []
    with pytest.raises(DomainError) as error:
        tasks.stage_artifact(limited, job["id"], point["sha256"])
    assert error.value.status == 404
    (service.data_dir / "objects" / point["sha256"]).write_bytes(b'{"corrupt":true}')
    with pytest.raises(DomainError) as error:
        tasks.stage_artifact(actor, job["id"], point["sha256"])
    assert error.value.status == 409


@pytest.mark.parametrize("policy_change", ["disable", "change"])
def test_retry_cannot_reuse_stages_after_model_policy_revocation(store, monkeypatch, policy_change):
    from vks_api.ai_gateway import AiService, ModelPolicy

    service, actor, pid, w, _, _ = store
    with service.db() as db:
        db.add(ModelPolicy(project_id=pid, settings={"enabled": True}))
    calls = []

    def offline(*args):
        calls.append("model")
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic provider failure", 503)

    monkeypatch.setattr(AiService, "prepare_document", offline)
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        actor,
        pid,
        TaskInput(
            analysis_set_id=w["selected_analysis_set"]["id"], task_kind="document_draft", max_model_calls=1
        ).model_dump(),
        "model-policy",
    )
    service.process_job(job["id"])
    assert calls == ["model"]
    with service.db() as db:
        policy = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = dict(policy.settings, enabled=policy_change != "disable", model="changed")
        policy.row_version += 1
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert calls == ["model"]
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_VERSION_CONFLICT"
    assert result["report"] is None
