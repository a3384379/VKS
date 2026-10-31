
from test_model_gateway import system as system  # pytest fixture registration
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import pytest
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.journeys import JourneyService
from vks_api.models import Entity, Relation, Artifact
from vks_api.service import DomainError
from vks_api.task_planning import PlanExecutor


def plan(context):
    action = next((row["revision_id"] for row in context["entities"] if row["type"] == "action"))
    api = next((row["revision_id"] for row in context["entities"] if row["type"] == "api"))
    evidence = context["evidence"][0]["evidence_id"]

    def step(key, tool, ids=(), relation=None, markdown=None):
        return dict(
            id=key,
            tool=tool,
            target_revision_ids=list(ids),
            evidence_ids=[evidence],
            relation_type=relation,
            rationale="Synthetic knowledge task evidence review",
            markdown=markdown,
        )

    return dict(
        title="Synthetic natural-language plan",
        unknowns=["No verified runtime chain"],
        steps=[
            step("context", "inspect_action", [action]),
            step("evidence", "inspect_evidence"),
            step("relation", "propose_relation", [action, api], "depends_on"),
            step("journey", "propose_journey", [action]),
            step("document", "draft_document", markdown="Synthetic evidence-based paragraph"),
        ],
    )


def create(system, key="plan"):
    service, actor, p, aid, ai, requests, response, config = system
    response["plan"] = plan
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        actor,
        p["id"],
        TaskInput(
            analysis_set_id=aid,
            task_kind="knowledge_plan",
            instruction="Explain this action and propose a reviewable flow and document",
            max_model_calls=1,
        ).model_dump(),
        key,
    )
    return (tasks, job)


def test_real_synthetic_http_plan_executes_bounded_tools_and_commits_only_candidates(system):
    service, actor, p, aid, ai, requests, response, config = system
    tasks, job = create(system)
    service.process_job(job["id"])
    assert JourneyService(service).list(actor, p["id"], aid)
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", (result.get("error"), result.get("stage_history"))
    assert len(requests) == 1 and requests[0][2]["store"] is False
    assert not requests[0][2].get("tools")
    report = result["report"]
    assert len(report["plan_steps"]) == 5 and len(report["tool_results"]) == 2
    assert report["saved_directly"] and report["model_calls"] == 1
    assert all(
        (row["excluded"] is False and row["causal_basis"] == "not_established" for row in report["relation_candidates"])
    )
    assert len(JourneyService(service).list(actor, p["id"], aid)) == 1
    with service.db() as db:
        docs = [
            r
            for r in service._revisions(db, aid)
            if db.get(Entity, r.entity_id).type == "document" and r.metadata_json.get("source_kind") == "ai_generated"
        ]
        assert len(docs) == 1
        rel = db.get(Relation, report["relation_candidates"][0]["relation_id"])
        assert rel.excluded is False and (not rel.manual_lock)
    assert tasks.get(replace(actor, scopes=actor.scopes - {"source:read", "trace:read"}), job["id"])["report"] is None


@pytest.mark.parametrize(
    "case", ["forged_evidence", "forged_revision", "shell", "approve", "extra_fields", "duplicate_step", "budget"]
)
def test_model_plan_rejects_fabricated_scope_tools_and_budget_before_mutations(system, case):
    service, actor, p, aid, ai, requests, response, config = system
    tasks, job = create(system)

    def invalid(context):
        value = plan(context)
        if case == "forged_evidence":
            value["steps"][0]["evidence_ids"] = ["not-supplied"]
        if case == "forged_revision":
            value["steps"][0]["target_revision_ids"] = ["not-supplied"]
        if case in ("shell", "approve"):
            value["steps"][0]["tool"] = case
        if case == "extra_fields":
            value["steps"][0]["review_status"] = "confirmed"
        if case == "duplicate_step":
            value["steps"][1]["id"] = value["steps"][0]["id"]
        if case == "budget":
            value["steps"] *= 2
        return value

    response["plan"] = invalid
    before = deepcopy(service.workspace(actor, p["id"], aid)["relations"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "failed" and result["report"] is None
    assert len(requests) == 1
    assert service.workspace(actor, p["id"], aid)["relations"] == before
    assert not JourneyService(service).list(actor, p["id"], aid)


def test_plan_resume_reuses_model_and_completed_tool_without_duplicate_side_effects(system, monkeypatch):
    service, actor, p, aid, ai, requests, response, config = system
    tasks, job = create(system)
    counts = Counter()
    original = PlanExecutor.read

    def interrupted(self, step, plan):
        counts[step["id"]] += 1
        if step["id"] == "evidence" and counts[step["id"]] == 1:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic tool interruption", 503)
        return original(self, step, plan)

    monkeypatch.setattr(PlanExecutor, "read", interrupted)
    service.process_job(job["id"])
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "failed"
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", (result.get("error"), result.get("stage_history"))
    assert len(requests) == 1 and counts == {"context": 1, "evidence": 2}
    assert len(JourneyService(service).list(actor, p["id"], aid)) == 1


def test_plan_does_not_reset_existing_human_rejection(system):
    service, actor, p, aid, ai, requests, response, config = system
    tasks, job = create(system)
    with service.db() as db:
        selected = list(service._revisions(db, aid))
        action = next((r for r in selected if db.get(Entity, r.entity_id).type == "action"))
        api = next(
            (
                r
                for r in selected
                if db.get(Entity, r.entity_id).type == "api" and db.get(Artifact, r.artifact_id).kind == "openapi"
            )
        )
    evidence = service.workspace(actor, p["id"], aid)["evidence"][0]["id"]
    candidate = service.propose_links(
        actor,
        p["id"],
        dict(
            analysis_set_id=aid,
            candidates=[
                dict(
                    from_revision_id=action.id,
                    to_revision_id=api.id,
                    relation_type="depends_on",
                    evidence_ids=[evidence],
                    causal_basis="not_established",
                    reason="Synthetic",
                )
            ],
        ),
        "prior",
    )["candidates"][0]
    service.set_relation_state(
        actor,
        candidate["id"],
        {
            "excluded": dict(decision="reject", reason="Human rejection", evidence_ids=candidate["evidence_ids"])[
                "decision"
            ]
            == "reject"
        },
        candidate["row_version"],
    )
    tasks, job = create(system, "after-review")

    def rejected(context):
        value = plan(context)
        value["steps"] = [step for step in value["steps"] if step["tool"] == "propose_relation"]
        value["steps"][0]["target_revision_ids"] = [action.id, api.id]
        return value

    response["plan"] = rejected
    service.process_job(job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", (result.get("error"), result.get("stage_history"))
    assert result["report"]["relation_candidates"][0]["manual_decision_preserved"] is True
    assert result["report"]["relation_candidates"][0]["excluded"] is True


@pytest.mark.asyncio
async def test_official_sdk_natural_language_task_schema_and_saved_directly_result(system):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from vks_api.app import create_app
    from vks_api.mcp_gateway import APIBridge, create_http_app

    service, actor, p, aid, _, requests, response, _ = system
    response["plan"] = plan
    token = service.create_token(
        actor,
        p["id"],
        dict(
            name="Plan SDK",
            scopes=["knowledge:read", "knowledge:edit", "analysis:run", "source:read", "trace:read", "links:propose"],
        ),
    )["token"]
    app = create_app(service.data_dir, enable_worker=False)
    gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=app)))
    try:
        async with gateway.router.lifespan_context(gateway):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + token}
            ) as client:
                async with streamable_http_client("http://localhost:8788/mcp", http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as sdk:
                        await sdk.initialize()
                        created = await sdk.call_tool(
                            "create_knowledge_task",
                            dict(
                                project_id=p["id"],
                                analysis_set_id=aid,
                                task_kind="knowledge_plan",
                                instruction="Propose bounded evidence-grounded candidates",
                                max_model_calls=1,
                                idempotency_key="sdk-plan",
                            ),
                        )
                        assert not created.isError, created.content
                        jid = created.structuredContent["data"]["id"]
                        app.state.service.process_job(jid)
                        result = await sdk.call_tool("get_knowledge_task", dict(project_id=p["id"], job_id=jid))
                        assert not result.isError, result.content
                        value = result.structuredContent["data"]
                        assert JourneyService(app.state.service).list(actor, p["id"], aid)
                        app.state.service.process_job(jid)
                        result = await sdk.call_tool("get_knowledge_task", dict(project_id=p["id"], job_id=jid))
                        assert not result.isError, result.content
                        value = result.structuredContent["data"]
                        assert value["state"] == "succeeded", value
                        assert value["report"]["saved_directly"] and value["stage_artifacts"]
                        assert value["module_decisions"] == {} and len(requests) == 1
    finally:
        app.state.service.engine.dispose()
