"""V2 product projections; all reads use the existing permission-filtered services."""

from collections import deque

from .v2_contracts import (
    Envelope,
    JobOutput,
    OnboardingOutput,
    CoverageOutput,
    ExplanationOutput,
    BusinessReferencesOutput,
    ReplayOutput,
    KnowledgeMapOutput,
)

from fastapi import Depends, Header, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from .journeys import JourneyService
from .knowledge_coverage import action_knowledge_revisions
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


TECHNICAL_TYPES = {
    "handler_of",
    "may_call",
    "implements",
    "maps_to_sql",
    "reads_table",
    "writes_table",
    "documents",
    "depends_on",
    "observed_during",
    "confirmed_trigger",
}
KNOWLEDGE_TYPES = {"api", "symbol", "sql", "table", "document", "section"}


def technical_adjacency(graph, *, reverse=False):
    from collections import defaultdict

    nodes = {row["entity_revision_id"]: row for row in graph["nodes"]}
    adjacent = defaultdict(list)
    for edge in graph["edges"]:
        source, target = edge["from_revision_id"], edge["to_revision_id"]
        if edge["relation_type"] == "handler_of":
            source, target = target, source
        elif edge["relation_type"] == "documents":
            # Normalize the association towards documentation. It is a leaf of
            # a technical explanation, not a bridge between related actions.
            if nodes[source]["type"] in ("document", "section"):
                source, target = target, source
            if nodes[target]["type"] not in ("document", "section"):
                continue
        if reverse:
            source, target = target, source
        adjacent[source].append((target, edge))
    return nodes, adjacent


def coverage(service, actor, pid, analysis_set_id=None, release_id=None):
    w = service.workspace(actor, pid, analysis_set_id, release_id)
    actions = {row["entity_revision_id"] for row in w["actions"]}
    trusted = [r for r in w["relations"] if (r["excluded"] is False) and r["validity_status"] == "valid"]
    linked = action_knowledge_revisions(w["entities"], trusted)
    runtime_linked = (
        action_knowledge_revisions(
            w["entities"], [row for row in trusted if row["causal_basis"] == "correlated_runtime"]
        )
        if (w["selected_analysis_set"] or {}).get("runtime_alignment") == "matched"
        else set()
    )
    journey_counts = dict.fromkeys(
        ("business_process", "operation_sequence", "evidence_navigation", "observation_comparison", "unclassified"), 0
    )
    for row in w["entities"]:
        if row["type"] == "journey":
            meaning = row.get("metadata", {}).get("journey_dsl", {}).get("semantics", {}).get("kind", "unclassified")
            journey_counts[meaning if meaning in journey_counts else "unclassified"] += 1
    return dict(
        project_id=pid,
        analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
        release_id=(w["selected_release"] or {}).get("id"),
        source_vector=w["sources"],
        scope="当前身份可见固定基线的关联知识覆盖；排除页面成员及结构关系，仅计算有效且未移除的关联",
        actions={"linked": len(linked), "total": len(actions)},
        relations={
            "linked": len(trusted),
            "total": len(w["relations"]),
            "excluded": sum(r["excluded"] for r in w["relations"]),
            "stale": sum(r["validity_status"] == "stale" for r in w["relations"]),
            "broken": sum(r["validity_status"] == "broken" for r in w["relations"]),
        },
        journeys={"total": sum(journey_counts.values()), **journey_counts},
        knowledge_health=dict(
            unverified_runtime_actions=len(actions - runtime_linked),
            action_denominator=len(actions),
            outdated_relations=sum(
                (r["excluded"] is False) and r["validity_status"] != "valid" for r in w["relations"]
            ),
            relation_denominator=len(w["relations"]),
        ),
        source_alignment=(w["selected_analysis_set"] or {}).get("runtime_alignment", "unknown"),
        unknowns=w["unknowns"],
    )


def explanation(service, actor, pid, eid, analysis_set_id=None, release_id=None):
    from sqlalchemy import select
    from .models import Evidence
    from .reading import ReadingService, chunks

    reading = ReadingService(service)
    with reading.db() as s:
        graph, analysis, release = reading._graph(
            s, actor, pid, eid, analysis_set_id, release_id, 3, 100, TECHNICAL_TYPES, True, "valid"
        )
        action = next((row for row in graph["nodes"] if row["entity_id"] == eid and row["type"] == "action"), None)
        if not action:
            raise DomainError("VKS_NOT_FOUND", "未找到当前版本操作", 404)
        seed = action["entity_revision_id"]
        nodes, adjacent = technical_adjacency(graph)
        paths = []
        frontier = deque([(seed, [seed], [])])
        truncated = graph["truncated"]
        while frontier and len(paths) < 120:
            current, visited, relations = frontier.popleft()
            if len(relations) >= 3:
                if any(target not in visited for target, _ in adjacent[current]):
                    truncated = True
                continue
            if current != seed and nodes[current]["type"] in ("document", "section"):
                continue
            for target, edge in adjacent[current]:
                if target in visited or nodes[target]["type"] not in KNOWLEDGE_TYPES:
                    continue
                edges = [*relations, edge]
                path = [*visited, target]
                paths.append(
                    dict(
                        revision_ids=path,
                        relation_ids=[r["id"] for r in edges],
                        causal_basis=[r["causal_basis"] for r in edges],
                        excluded=[r["excluded"] for r in edges],
                        evidence_ids=sorted({key for r in edges for key in r["evidence_ids"]}),
                        interpretation="按关系方向展开的技术关联路径；文档为终点，不表示完整运行调用链",
                    )
                )
                if len(paths) >= 120:
                    truncated = True
                    break
                frontier.append((target, path, edges))
        choices, _ = reading.selections(s, analysis, release)
        journeys, journeys_truncated = JourneyService(service).referencing(s, actor, pid, choices, seed)
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
            is_truncated=truncated or omitted or journeys_truncated,
            unknowns=[
                *graph["unknowns"],
                "排除页面包含等结构关系；技术路径最多3跳、100节点、120条路径",
                "没有经核验且与源码版本对齐的 Trace 时，不能宣称完整运行因果链",
            ]
            + (["有限路径仅返回已列出路径涉及的关联；循环、其他分支或超限关联需在局部图中继续核对"] if omitted else [])
            + (["流程反查最多检查200个当前可见流程，未检查部分可能仍有引用"] if journeys_truncated else []),
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
        valid_only: bool = False,
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
                "valid" if valid_only else "all",
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
            + (["资料未接入：请上传资料并运行格式预检"] if not w["sources"] else []),
            capabilities=["readonly_import", "passive_explanation", "direct_knowledge_save"],
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
                s, a, project_id, eid, analysis_set_id, release_id, 3, 100, TECHNICAL_TYPES, True, "valid"
            )
            choices, _ = reading.selections(s, analysis, release)
            seed = choices[eid]
            nodes, adjacent = technical_adjacency(graph, reverse=True)
            reached, frontier = {seed}, deque([(seed, 0)])
            truncated = graph["truncated"]
            while frontier:
                current, depth = frontier.popleft()
                if nodes[current]["type"] == "action":
                    continue
                if depth >= 3:
                    if any(target not in reached for target, _ in adjacent[current]):
                        truncated = True
                    continue
                for target, _ in adjacent[current]:
                    if target in reached or nodes[target]["type"] not in KNOWLEDGE_TYPES | {"action"}:
                        continue
                    reached.add(target)
                    frontier.append((target, depth + 1))
            actions = [
                row for row in graph["nodes"] if row["entity_revision_id"] in reached and row["type"] == "action"
            ]
            journeys, journeys_truncated = JourneyService(service).referencing(s, a, project_id, choices, seed)
            return envelope(
                request,
                dict(
                    project_id=project_id,
                    analysis_set_id=analysis.id,
                    release_id=release.id if release else None,
                    entity_id=eid,
                    actions=actions,
                    journeys=journeys,
                    is_truncated=truncated or journeys_truncated,
                    unknowns=graph["unknowns"]
                    + ["动作反查沿技术关系反向最多3跳；不穿过页面成员关系或共享文档串联其他动作"]
                    + (["流程反查最多检查200个当前可见流程，未检查部分可能仍有引用"] if journeys_truncated else []),
                ),
            )

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
