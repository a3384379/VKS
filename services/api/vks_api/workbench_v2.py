"""V2 product projections; all reads use the existing permission-filtered services."""

from .v2_contracts import (
    Envelope,
    JobOutput,
    OnboardingOutput,
    CoverageOutput,
    ExplanationOutput,
    BusinessReferencesOutput,
    ReplayOutput,
    InboxItem,
    KnowledgeMapOutput,
)

from fastapi import Depends, Header, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from .journeys import JourneyService
from .models import Analysis
from .service import DomainError, canonical, sha


class OnboardingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str | None = None
    goal: str = Field(default="", max_length=2000)
    allow_platform_llm: bool = False
    source_code_available: bool = True


class ImpactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_release_id: str
    analysis_set_id: str


def coverage(service, actor, pid, analysis_set_id=None, release_id=None):
    w = service.workspace(actor, pid, analysis_set_id, release_id)
    actions = {row["entity_revision_id"] for row in w["actions"]}
    trusted = [r for r in w["relations"] if r["review_status"] == "confirmed" and r["validity_status"] == "valid"]
    linked = actions & {rid for row in trusted for rid in (row["from_revision_id"], row["to_revision_id"])}
    runtime_linked = (
        actions
        & {
            rid
            for row in trusted
            if row["causal_basis"] == "correlated_runtime"
            for rid in (row["from_revision_id"], row["to_revision_id"])
        }
        if (w["selected_analysis_set"] or {}).get("runtime_alignment") == "matched"
        else set()
    )
    return dict(
        project_id=pid,
        analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
        release_id=(w["selected_release"] or {}).get("id"),
        source_vector=w["sources"],
        scope="当前身份可见、当前固定基线；不含已移除动作；候选不计入确认覆盖",
        actions={"confirmed": len(linked), "total": len(actions)},
        relations={
            "confirmed": len(trusted),
            "total": len(w["relations"]),
            "candidate": sum(r["review_status"] == "candidate" for r in w["relations"]),
            "stale": sum(r["validity_status"] == "stale" for r in w["relations"]),
            "broken": sum(r["validity_status"] == "broken" for r in w["relations"]),
        },
        journeys={"total": sum(e["type"] == "journey" for e in w["entities"])},
        knowledge_health=dict(
            unverified_runtime_actions=len(actions - runtime_linked),
            action_denominator=len(actions),
            review_backlog=sum(
                r["review_status"] != "rejected"
                and (r["review_status"] == "candidate" or r["validity_status"] != "valid")
                for r in w["relations"]
            ),
            relation_denominator=len(w["relations"]),
        ),
        source_alignment=(w["selected_analysis_set"] or {}).get("runtime_alignment", "unknown"),
        unknowns=w["unknowns"],
    )


def explanation(service, actor, pid, eid, analysis_set_id=None, release_id=None):
    from collections import defaultdict, deque
    from sqlalchemy import select
    from .models import Evidence
    from .reading import ReadingService, chunks

    reading = ReadingService(service)
    with reading.db() as s:
        graph, analysis, release = reading._graph(
            s, actor, pid, eid, analysis_set_id, release_id, 3, 100, None, True, "trusted"
        )
        action = next((row for row in graph["nodes"] if row["entity_id"] == eid and row["type"] == "action"), None)
        if not action:
            raise DomainError("VKS_NOT_FOUND", "未找到当前版本操作", 404)
        seed = action["entity_revision_id"]
        adjacent = defaultdict(list)
        for edge in graph["edges"]:
            adjacent[edge["from_revision_id"]].append(edge)
            adjacent[edge["to_revision_id"]].append(edge)
        paths = []
        frontier = deque([(seed, [seed], [])])
        truncated = graph["truncated"]
        while frontier and len(paths) < 120:
            current, visited, relations = frontier.popleft()
            if len(relations) >= 3:
                continue
            for edge in adjacent[current]:
                target = edge["to_revision_id"] if edge["from_revision_id"] == current else edge["from_revision_id"]
                if target in visited:
                    continue
                edges = [*relations, edge]
                path = [*visited, target]
                paths.append(
                    dict(
                        revision_ids=path,
                        relation_ids=[r["id"] for r in edges],
                        causal_basis=[r["causal_basis"] for r in edges],
                        review_status=[r["review_status"] for r in edges],
                        evidence_ids=sorted({key for r in edges for key in r["evidence_ids"]}),
                        interpretation="知识关联路径；不表示完整运行调用链",
                    )
                )
                if len(paths) >= 120:
                    truncated = True
                    break
                frontier.append((target, path, edges))
        choices, _ = reading.selections(s, analysis, release)
        journeys = [
            row
            for row in JourneyService(service)._selected_list(s, actor, pid, choices)
            if any(node.get("binding", {}).get("revision_id") == seed for node in row["dsl"]["nodes"])
        ]
        path_relations = {rid for path in paths for rid in path["relation_ids"]}
        path_nodes = {seed} | {rid for path in paths for rid in path["revision_ids"]}
        relations = [row for row in graph["edges"] if row["id"] in path_relations]
        needed_evidence = {key for row in relations for key in row["evidence_ids"]}
        evidence = []
        for batch in chunks(needed_evidence):
            evidence.extend(
                service.evidence_dto(row)
                for row in s.scalars(
                    select(Evidence).where(
                        Evidence.project_id == pid, Evidence.analysis_set_id == analysis.id, Evidence.id.in_(batch)
                    )
                )
            )
        omitted = len(relations) < len(graph["edges"])
        return dict(
            project_id=pid,
            analysis_set_id=analysis.id,
            release_id=release.id if release else None,
            action_id=eid,
            action_revision_id=seed,
            source_vector=service._visible_sources(s, actor, pid, analysis.sources),
            source_alignment=analysis.runtime_alignment,
            entities=[
                {key: value for key, value in row.items() if key != "content"}
                for row in graph["nodes"]
                if row["entity_revision_id"] in path_nodes
            ],
            paths=paths,
            journeys=journeys,
            relations=relations,
            evidence=evidence,
            is_truncated=truncated or omitted,
            unknowns=[*graph["unknowns"], "没有经核验且与源码版本对齐的 Trace 时，不能宣称完整运行因果链"]
            + (["有限路径仅返回已列出路径涉及的关联；循环、其他分支或超限关联需在局部图中继续核对"] if omitted else []),
        )


def install_workbench(app, service, actor, envelope):
    api = "/api/v2"

    @app.get(api + "/entities/{eid}/knowledge-map", response_model=Envelope[KnowledgeMapOutput])
    def knowledge_map(
        eid: str,
        project_id: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        depth: int = Query(2, ge=1, le=3),
        max_nodes: int = Query(100, ge=1, le=100),
        confirmed_only: bool = False,
        a=Depends(actor),
    ):
        from .reading import ReadingService

        reading = ReadingService(service)
        with reading.db() as s:
            graph, analysis, release = reading._graph(
                s,
                a,
                project_id,
                eid,
                analysis_set_id,
                release_id,
                depth,
                max_nodes,
                None,
                True,
                "trusted" if confirmed_only else "map",
            )
            return envelope(
                request, dict(graph, analysis_set_id=analysis.id, release_id=release.id if release else None)
            )

    @app.post(
        api + "/projects/{pid}/onboarding/preflight",
        response_model=Envelope[OnboardingOutput],
        response_model_exclude_unset=True,
    )
    def preflight(pid: str, payload: OnboardingInput, request: Request, a=Depends(actor)):
        with service.db() as s:
            service.require(s, a, pid, "source:import")
        w = service.workspace(a, pid, payload.analysis_set_id)
        result = dict(
            project_id=pid,
            analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
            source_vector=w["sources"],
            allow_platform_llm=False,
            requires_model_policy=payload.allow_platform_llm,
            source_alignment="unknown",
            issues=([] if payload.source_code_available else ["未提供源码，调用链与 SQL 能力降级"])
            + (["资料未接入：请上传资料并运行安全预检"] if not w["sources"] else []),
            capabilities=["readonly_import", "passive_explanation", "human_review"],
            requires_review=True,
            server_filesystem_access=False,
        )
        result["preflight_sha256"] = sha(canonical(result))
        return envelope(request, result)

    @app.get(
        api + "/projects/{pid}/knowledge-coverage",
        response_model=Envelope[CoverageOutput],
        response_model_exclude_unset=True,
    )
    def quality(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, coverage(service, a, pid, analysis_set_id, release_id))

    @app.get(
        api + "/actions/{eid}/explanation",
        response_model=Envelope[ExplanationOutput],
        response_model_exclude_unset=True,
    )
    def explain(
        eid: str,
        project_id: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a=Depends(actor),
    ):
        return envelope(request, explanation(service, a, project_id, eid, analysis_set_id, release_id))

    @app.get(
        api + "/entities/{eid}/business-references",
        response_model=Envelope[BusinessReferencesOutput],
        response_model_exclude_unset=True,
    )
    def business_references(
        eid: str,
        project_id: str,
        request: Request,
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a=Depends(actor),
    ):
        from .reading import ReadingService

        reading = ReadingService(service)
        with reading.db() as s:
            graph, analysis, release = reading._graph(
                s, a, project_id, eid, analysis_set_id, release_id, 3, 100, None, True, "trusted"
            )
            actions = [row for row in graph["nodes"] if row["type"] == "action"]
            return envelope(
                request,
                dict(
                    project_id=project_id,
                    analysis_set_id=analysis.id,
                    release_id=release.id if release else None,
                    entity_id=eid,
                    actions=actions,
                    is_truncated=graph["truncated"],
                    unknowns=graph["unknowns"],
                ),
            )

    @app.get(
        api + "/projects/{pid}/review-inbox",
        response_model=Envelope[list[InboxItem]],
        response_model_exclude_unset=True,
    )
    def inbox(pid: str, request: Request, analysis_set_id: str, a=Depends(actor)):
        with service.db() as s:
            service.require(s, a, pid, "links:review")
        w = service.workspace(a, pid, analysis_set_id)
        from .versions import VersionService
        from .documents import DocumentService

        items = [
            dict(kind="relation", subject_id=r["id"], data=r)
            for r in w["relations"]
            if r["review_status"] == "candidate" or r["validity_status"] != "valid"
        ]
        from sqlalchemy import select
        from .models import Audit

        journeys = JourneyService(service).list(a, pid, analysis_set_id)
        with service.db() as s:
            for row in journeys:
                decision = s.scalar(
                    select(Audit)
                    .where(
                        Audit.project_id == pid,
                        Audit.resource_id == row["revision_id"],
                        Audit.action.in_(["journey.approve", "journey.reject"]),
                    )
                    .order_by(Audit.created_at.desc(), Audit.id.desc())
                )
                if (
                    not decision
                    or decision.action != "journey.approve"
                    or decision.metadata_json.get("sha256") != row["content_sha256"]
                ):
                    items.append(dict(kind="journey", subject_id=row["journey_id"], data=row))
        items += [
            dict(kind="migration", subject_id=row["id"], data=row)
            for row in VersionService(service).list_proposals(a, pid, analysis_set_id)
            if row["status"] == "pending"
        ]
        for row in w["documents"]:
            workflow = DocumentService(service).workflow(a, row["entity_id"], analysis_set_id)
            if workflow["state"] != "approved" or workflow["revision_id"] != row["entity_revision_id"]:
                items.append(dict(kind="document", subject_id=row["entity_id"], data=workflow))
        return envelope(request, items)

    @app.get(
        api + "/projects/{pid}/state-replay", response_model=Envelope[ReplayOutput], response_model_exclude_unset=True
    )
    def replay(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        from .scenarios import CaptureRecord, ScenarioService

        w = service.workspace(a, pid, analysis_set_id, release_id)
        by_key = {e["stable_key"]: e for e in w["entities"]}
        with service.db() as s:
            trace = service._can(s, a, pid, "trace:read")
            analysis = s.get(Analysis, w["selected_analysis_set"]["id"]) if w["selected_analysis_set"] else None
            digest = ScenarioService(service)._baseline_digest(s, analysis) if analysis else None
            scenarios = []
            for row in w["scenarios"]:
                graph = row["metadata"].get("dsl", {}).get("explanation_graph", {})
                states = []
                for state in graph.get("states", []):
                    if any(state.get(field) and state[field] not in by_key for field in ("actionKey", "pageKey")):
                        continue
                    if state["evidenceOrigin"] == "observed":
                        capture = s.get(CaptureRecord, state.get("captureRef")) if trace else None
                        if (
                            not capture
                            or capture.project_id != pid
                            or capture.state != "succeeded"
                            or capture.result.get("baseline_sha256") != digest
                        ):
                            continue
                    states.append(state)
                state_keys = {state["stateKey"] for state in states}
                transitions = [
                    item
                    for item in graph.get("transitions", [])
                    if item["fromState"] in state_keys
                    and item["toState"] in state_keys
                    and item.get("actionKey") in by_key
                    and (item.get("evidenceOrigin") != "observed" or trace)
                ]
                scenarios.append(
                    dict(
                        entity_id=row["entity_id"],
                        revision_id=row["entity_revision_id"],
                        name=row["name"],
                        states=states,
                        transitions=transitions,
                    )
                )
        return envelope(
            request,
            dict(
                project_id=pid,
                analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
                release_id=(w["selected_release"] or {}).get("id"),
                source_vector=w["sources"],
                scenarios=scenarios,
                mode="passive",
                unknowns=["声明/模拟/观察独立呈现；页面绑定不代表后端运行因果"],
            ),
        )

    @app.post(
        api + "/projects/{pid}/change-impacts",
        status_code=202,
        response_model=Envelope[JobOutput],
        response_model_exclude_unset=True,
    )
    def impact(pid: str, payload: ImpactInput, request: Request, a=Depends(actor), idempotency_key: str = Header(...)):
        with service.db() as s:
            service.require(s, a, pid, "analysis:run")
            analysis = s.get(Analysis, payload.analysis_set_id)
            if not analysis or analysis.project_id != pid:
                raise DomainError("VKS_NOT_FOUND", "未找到基线", 404)
        return envelope(
            request,
            service.create_analysis_job(
                a,
                payload.analysis_set_id,
                {"kind": "detect_drift", "from_release_id": payload.from_release_id},
                idempotency_key,
            ),
        )

    @app.get(
        api + "/projects/{pid}/change-impacts/{jid}",
        response_model=Envelope[JobOutput],
        response_model_exclude_unset=True,
    )
    def impact_report(pid: str, jid: str, request: Request, a=Depends(actor)):
        result = service.get_job(a, jid, pid)
        if result["kind"] != "detect_drift":
            raise DomainError("VKS_NOT_FOUND", "未找到影响报告", 404)
        return envelope(request, result)
