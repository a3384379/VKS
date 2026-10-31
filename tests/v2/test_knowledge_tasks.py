
from test_journeys import store as store  # pytest fixture registration
from dataclasses import replace
import pytest
from sqlalchemy import select
from test_journeys import journey
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Analysis, Artifact, Entity, Revision
from vks_api.journeys import JourneyService
from vks_api.service import DomainError
from vks_api.workbench_v2 import coverage, explanation


def test_task_flow_is_durable_saved_directly_idempotent_and_bounded(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"],
        task_kind="business_flow",
        action_ids=[a["entity_id"] for a in w["actions"][:3]],
    ).model_dump()
    job = tasks.create(actor, pid, payload, "task")
    assert tasks.create(actor, pid, payload, "task") == job
    service.process_job(job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert result["stage"] == "COMPLETE"
    assert result["report"]["saved_directly"] and result["report"]["model_calls"] == 0
    assert result["stage_history"][-1]["stage"] == "Save"
    journeys = JourneyService(service).list(actor, pid, payload["analysis_set_id"])
    assert len(journeys) == 1
    assert journeys[0]["dsl"]["semantics"]["kind"] == "evidence_navigation"
    assert len([node for node in journeys[0]["dsl"]["nodes"] if node["kind"] == "action"]) == len(payload["action_ids"])
    with service.db() as s:
        assert not JourneyService(service).assert_publishable(s, s.get(Analysis, payload["analysis_set_id"]))
    with service.db() as s:
        assert not JourneyService(service).assert_publishable(s, s.get(Analysis, payload["analysis_set_id"]))


def test_task_cancel_and_baseline_change_discard_outputs(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(analysis_set_id=w["selected_analysis_set"]["id"], task_kind="evidence_gaps").model_dump()
    job = tasks.create(actor, pid, payload, "cancel")
    service.cancel_job(actor, job["id"])
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "cancelled"
    job = tasks.create(actor, pid, payload, "stale")
    with service.db() as s:
        s.get(Analysis, payload["analysis_set_id"]).selection_version += 1
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_VERSION_CONFLICT"
    assert not result["report"]


def test_report_and_journey_respect_current_artifact_classification(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(analysis_set_id=w["selected_analysis_set"]["id"], task_kind="project_overview").model_dump()
    job = tasks.create(actor, pid, payload, "report")
    service.process_job(job["id"])
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    assert tasks.get(limited, job["id"])["report"] is None
    journeys = JourneyService(service)
    row = journeys.create(actor, pid, dict(analysis_set_id=payload["analysis_set_id"], dsl=journey(w)), "j")
    assert journeys.list(limited, pid, payload["analysis_set_id"])
    with service.db() as s:
        ref = s.get(Revision, w["actions"][0]["entity_revision_id"])
        s.get(Artifact, ref.artifact_id).sensitivity = "confidential"
    assert not journeys.list(limited, pid, payload["analysis_set_id"])
    with pytest.raises(DomainError):
        journeys.get(limited, pid, row["journey_id"], row["revision_id"])


def test_coverage_explanation_same_fixed_baseline_and_no_false_trace(store, monkeypatch):
    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    result = coverage(service, actor, pid, aid)
    assert result["actions"] == {"linked": len(w["actions"]), "total": len(w["actions"])}
    assert result["knowledge_health"]["unverified_runtime_actions"] == len(w["actions"])
    assert result["knowledge_health"]["action_denominator"] == len(w["actions"])
    monkeypatch.setattr(
        service,
        "workspace",
        lambda *a, **k: pytest.fail("A bounded V2 action read must not materialize the full workspace"),
    )
    action = w["actions"][0]
    result = explanation(service, actor, pid, action["entity_id"], aid)
    assert result["action_revision_id"] == action["entity_revision_id"]
    assert result["relations"] and all(
        not row["excluded"] and row["validity_status"] == "valid" for row in result["relations"]
    )
    assert result["source_alignment"] == "unknown"
    assert all(("content" not in row for row in result["entities"]))


def test_model_call_reservation_survives_failure_retry_without_extra_call(store, monkeypatch):
    from vks_api.ai_gateway import AiService, ModelPolicy

    service, actor, pid, w, _, _ = store
    with service.db() as s:
        s.add(ModelPolicy(project_id=pid, settings={"enabled": True}))
    calls = []

    def offline(*args):
        calls.append("model")
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic provider interruption", 503, retryable=True)

    monkeypatch.setattr(AiService, "prepare_document", offline)
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(
        analysis_set_id=w["selected_analysis_set"]["id"], task_kind="document_draft", max_model_calls=1
    ).model_dump()
    job = tasks.create(actor, pid, payload, "model")
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "failed"
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert calls == ["model"]
    assert result["error"]["code"] == "VKS_LIMIT_EXCEEDED"
    assert result["report"] is None
    assert result["stage_history"][-1]["state"] == "failed"


def test_module_candidates_need_explicit_human_acceptance_and_fixed_inputs(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(analysis_set_id=w["selected_analysis_set"]["id"], task_kind="module_analysis").model_dump()
    job = tasks.create(actor, pid, payload, "modules")
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    candidate = result["report"]["module_candidates"][0]
    from vks_api.modules import BusinessModule

    with service.db() as s:
        assert not s.scalar(select(BusinessModule).where(BusinessModule.project_id == pid))
    row = tasks.accept_module(actor, job["id"], candidate["key"], "human")
    assert tasks.accept_module(actor, job["id"], candidate["key"], "human") == row
    with service.db() as s:
        assert s.get(BusinessModule, row["module_id"]).name == candidate["title"]


def test_module_discovery_reports_route_source_and_document_bases_and_batch_rejection_is_final_for_report(store):
    service, actor, pid, w, _, _ = store
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(analysis_set_id=w["selected_analysis_set"]["id"], task_kind="module_analysis").model_dump()
    job = tasks.create(actor, pid, payload, "module-reject")
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    candidates = result["report"]["module_candidates"]
    assert {"explicit_page_action_membership", "route_prefix", "source_directory", "document_directory"} <= {
        row["basis"] for row in candidates
    }
    keys = [row["key"] for row in candidates[:2]]
    decision = dict(candidate_keys=keys, reason="Synthetic independent module boundary rejection")
    first = tasks.reject_modules(actor, job["id"], decision, "reject-batch")
    assert tasks.reject_modules(actor, job["id"], decision, "reject-batch") == first
    assert tasks.get(actor, job["id"])["module_decisions"] == {key: "rejected" for key in keys}
    with pytest.raises(DomainError) as error:
        tasks.accept_module(actor, job["id"], keys[0], "must-preserve-reject")
    assert error.value.status == 409
    with pytest.raises(DomainError):
        tasks.reject_modules(actor, job["id"], dict(candidate_keys=["forged"], reason="Synthetic"), "forged")
    from vks_api.models import Audit

    with service.db() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(Audit).where(Audit.action == "module.proposal.reject", Audit.resource_id == job["id"])
                    )
                )
            )
            == 1
        )


def test_observed_scenario_metadata_and_derived_journey_are_trace_restricted(store):
    from vks_api.models import Selection, uid
    from vks_api.service import sha

    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    with service.db() as s:
        entity = Entity(id=uid(), project_id=pid, type="scenario", stable_key="legacy-observed")
        s.add(entity)
        s.flush()
        metadata = {
            "dsl": {
                "explanation_graph": {
                    "states": [
                        {"stateKey": "private", "label": "Private observed state", "evidenceOrigin": "observed"}
                    ],
                    "transitions": [],
                }
            }
        }
        revision = Revision(
            id=uid(),
            entity_id=entity.id,
            project_id=pid,
            name="Private observed scenario",
            content="{}",
            content_sha256=sha("private-scenario"),
            semantic_content_sha256=sha("{}"),
            metadata_json=metadata,
        )
        s.add(revision)
        s.flush()
        s.add(Selection(analysis_set_id=aid, entity_id=entity.id, entity_revision_id=revision.id))
    limited = replace(actor, scopes=actor.scopes - {"trace:read"})
    assert not service.workspace(limited, pid, aid)["scenarios"]
    assert service.workspace(actor, pid, aid)["scenarios"][0]["name"] == "Private observed scenario"
    dsl = dict(
        schema_version="2.0",
        key="reference",
        title="Scenario reference",
        entry="ref",
        nodes=[
            dict(
                key="ref",
                kind="reference",
                title="Snapshot reference",
                x=40,
                y=40,
                binding={"entity_id": entity.id, "revision_id": revision.id},
            ),
            dict(key="end", kind="end", title="End", x=300, y=40),
        ],
        edges=[dict(key="next", source="ref", target="end", label="")],
    )
    journeys = JourneyService(service)
    journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=dsl), "observed-journey")
    assert not journeys.list(limited, pid, aid)
