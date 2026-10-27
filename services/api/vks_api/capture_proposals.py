"""Turn finite saved observations or a passive state graph into reviewable journeys."""

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from .models import Entity, Revision, Selection, uid
from .scenarios import CaptureRecord, ScenarioService
from .journeys import JourneyService
from .journey_replay import replay_references
from .service import DomainError
from .v2_contracts import Envelope, JourneyOutput


class CaptureJourneyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    scenario_id: str
    scenario_revision_id: str
    capture_ids: list[str] = Field(default_factory=list, max_length=10)


def state_node(key, title, scenario, state_key=None, capture=None, index=0, binding=None):
    node = dict(
        key=key,
        kind="reference",
        title=title[:200],
        x=40,
        y=40,
        binding={"entity_id": scenario.entity_id, "revision_id": scenario.id},
    )
    baseline = dict(scenario_id=scenario.entity_id, scenario_revision_id=scenario.id)
    if state_key is not None:
        node["state_ref"] = dict(baseline, state_key=state_key)
    if capture is not None:
        node["capture_ref"] = dict(baseline, capture_id=capture.id, snapshot_index=index)
    if binding:
        node.update(kind="action", binding={"entity_id": binding.entity_id, "revision_id": binding.id})
    return node


class CaptureJourneyService:
    def __init__(self, service):
        self.service = service

    def create(self, actor, pid, payload, version, key):
        journeys = JourneyService(self.service)
        with self.service.db() as s:
            analysis = journeys._analysis(s, actor, pid, payload["analysis_set_id"])

            def save():
                self.service._version(analysis.selection_version, version)
                selected = s.get(Selection, (analysis.id, payload["scenario_id"]))
                scene = s.get(Revision, payload["scenario_revision_id"])
                if (
                    not selected
                    or selected.entity_revision_id != payload["scenario_revision_id"]
                    or not scene
                    or scene.entity_id != payload["scenario_id"]
                    or scene.project_id != pid
                    or s.get(Entity, scene.entity_id).type != "scenario"
                    or not self.service._visible_revision(s, actor, pid, scene)
                ):
                    raise DomainError("VKS_NOT_FOUND", "未找到当前版本的可见场景修订", 404)
                ids = payload.get("capture_ids", [])
                if len(set(ids)) != len(ids):
                    raise DomainError("VKS_INVALID_INPUT", "录制轨迹不能重复")
                if ids:
                    self.service.require(s, actor, pid, "trace:read")
                    nodes, edges, entry = self._captures(s, actor, analysis, scene, ids)
                else:
                    nodes, edges, entry = self._states(s, actor, analysis, scene)
                for index, node in enumerate(nodes):
                    node.update(x=40 + (index % 4) * 240, y=40 + (index // 4) * 140)
                dsl = dict(
                    schema_version="2.0",
                    key="proposal-" + uid(),
                    title=(scene.name + " · 流程候选")[:200],
                    entry=entry,
                    nodes=nodes,
                    edges=edges,
                )
                dsl, refs = journeys._validate(s, actor, analysis, dsl)
                entity = Entity(id=uid(), project_id=pid, type="journey", stable_key=dsl["key"])
                s.add(entity)
                s.flush()
                row = journeys._save(s, actor, analysis, entity, dsl, refs)
                self.service.audit(
                    s,
                    actor,
                    pid,
                    "journey.propose_from_capture" if ids else "journey.propose_from_states",
                    row["revision_id"],
                    metadata={"scenario_revision_id": scene.id, "capture_ids": ids, "causal_basis": "not_established"},
                )
                return row

            return self.service.idem(
                s, actor, pid, "journey.propose_from_states", key, dict(payload, expected_version=str(version)), save
            )

    def _captures(self, s, actor, analysis, scene, ids):
        nodes, edges, starts = [], [], []
        for track, cid in enumerate(ids):
            capture = s.get(CaptureRecord, cid)
            if not capture or capture.project_id != analysis.project_id or capture.state != "succeeded":
                raise DomainError("VKS_NOT_FOUND", "未找到可用录制", 404)
            snapshots = capture.result.get("snapshots", [])
            if not snapshots:
                raise DomainError("VKS_INVALID_EVIDENCE", "录制没有已脱敏快照")
            previous = None
            for index, snapshot in enumerate(snapshots):
                node = state_node(f"track-{track}-state-{index}", snapshot["name"], scene, capture=capture, index=index)
                # Validates capture source scenario, exact bytes baseline and artifact ownership.
                replay_references(self.service, s, actor, analysis, {"nodes": [node]})
                nodes.append(node)
                if previous:
                    edges.append(
                        dict(
                            key=f"track-{track}-edge-{index}",
                            source=previous,
                            target=node["key"],
                            label="快照顺序 · 待核对",
                        )
                    )
                else:
                    starts.append((node["key"], f"录制轨迹 {track + 1}"))
                previous = node["key"]
            edges.append(dict(key=f"track-{track}-end", source=previous, target="end", label=""))
        nodes.append(dict(key="end", kind="end", title="结束", x=40, y=40))
        return self._entry(nodes, edges, starts)

    def _states(self, s, actor, analysis, scene):
        dsl = scene.metadata_json.get("dsl", {})
        ScenarioService(self.service)._validate(s, actor, analysis.project_id, analysis, dsl)
        graph = dsl.get("explanation_graph", {})
        states = graph.get("states", [])
        if not states:
            raise DomainError("VKS_INVALID_INPUT", "场景尚无被动状态，请先选择已完成录制或编排状态")
        if len(states) > 60:
            raise DomainError("VKS_LIMIT_EXCEEDED", "状态超过流程候选范围，请拆分为有限轨迹")
        selected = {s.get(Entity, r.entity_id).stable_key: r for r in self.service._revisions(s, analysis.id)}
        keys = {state["stateKey"]: f"state-{i}" for i, state in enumerate(states)}
        nodes = [
            state_node(
                keys[state["stateKey"]],
                state["label"],
                scene,
                state_key=state["stateKey"],
                binding=selected.get(state.get("actionKey")),
            )
            for state in states
        ]
        edges = []
        incoming = set()
        for index, state in enumerate(states):
            transitions = [row for row in graph.get("transitions", []) if row["fromState"] == state["stateKey"]]
            parent = keys[state["stateKey"]]
            if len(transitions) > 1:
                decision = f"branch-{index}"
                nodes.append(dict(key=decision, kind="decision", title="选择已声明分支", x=40, y=40))
                edges.append(dict(key=f"decision-{index}", source=parent, target=decision, label=""))
                parent = decision
            for i, transition in enumerate(transitions):
                target = keys[transition["toState"]]
                incoming.add(target)
                edges.append(
                    dict(
                        key=f"transition-{index}-{i}",
                        source=parent,
                        target=target,
                        label=(transition["conditionLabel"][:140] + f" ({i + 1})")
                        if len(transitions) > 1
                        else transition["conditionLabel"][:160],
                    )
                )
            if not transitions:
                edges.append(dict(key=f"end-{index}", source=parent, target="end", label=""))
        starts = [
            (keys[state["stateKey"]], f"状态入口 {i + 1} · {state['label'][:120]}")
            for i, state in enumerate(states)
            if keys[state["stateKey"]] not in incoming
        ]
        if not starts:
            raise DomainError("VKS_INVALID_INPUT", "循环状态需拆为有限录制轨迹，不能自动假定执行顺序")
        nodes.append(dict(key="end", kind="end", title="结束", x=40, y=40))
        return self._entry(nodes, edges, starts)

    @staticmethod
    def _entry(nodes, edges, starts):
        if len(starts) == 1:
            return nodes, edges, starts[0][0]
        nodes.insert(0, dict(key="entry", kind="decision", title="选择已保存轨迹或状态", x=40, y=40))
        edges[:0] = [
            dict(key=f"entry-{i}", source="entry", target=key, label=label) for i, (key, label) in enumerate(starts)
        ]
        return nodes, edges, "entry"


def install_capture_proposals(app, service, actor, envelope):
    proposals = CaptureJourneyService(service)

    @app.post(
        "/api/v2/projects/{pid}/journey-proposals",
        status_code=201,
        response_model=Envelope[JourneyOutput],
        response_model_exclude_none=True,
    )
    def propose(
        pid: str,
        payload: CaptureJourneyInput,
        request: Request,
        a=Depends(actor),
        if_match: str = Header(...),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, proposals.create(a, pid, payload.model_dump(), if_match, idempotency_key))
