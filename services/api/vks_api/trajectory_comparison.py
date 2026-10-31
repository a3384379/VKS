"""Compare exact saved roles/states, without replaying or inferring transitions."""

from typing import Any
from fastapi import Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from .models import Entity, Job, Revision
from .reading import ReadingService
from .scenarios import CaptureRecord, ScenarioRevision, ScenarioService
from .journey_replay import replay_references
from .service import DomainError, canonical, sha
from .v2_contracts import Envelope


class Options(BaseModel):
    items: list[dict[str, Any]]
    is_truncated: bool
    scope: str = "visible_items_in_recent_100_matching_captures"
    unknowns: list[str] = ["录制按最近 100 条匹配来源窗口检索；未显示的轨迹不计为已覆盖"]


class Comparison(BaseModel):
    project_id: str
    analysis_set_id: str
    release_id: str | None
    selection_version: int
    left: dict[str, Any]
    right: dict[str, Any]
    differences: dict[str, Any]
    source_vector: list[dict[str, Any]]
    comparison_sha256: str
    mode: str = "passive"
    unknowns: list[str]


class TrajectoryService:
    def __init__(self, service):
        self.service = service

    def _context(self, s, actor, pid, aid, rid):
        analysis, release = ReadingService(self.service).baseline(s, actor, pid, aid, rid)
        if not analysis:
            raise DomainError("VKS_NOT_FOUND", "未找到知识版本", 404)
        visible = [
            row for row in self.service._revisions(s, analysis.id) if self.service._visible_revision(s, actor, pid, row)
        ]
        scenes = {row.entity_id: row for row in visible if s.get(Entity, row.entity_id).type == "scenario"}
        keys = {s.get(Entity, row.entity_id).stable_key for row in visible}
        return analysis, release, scenes, keys

    def _side(self, s, actor, analysis, scenes, keys, choice):
        kind, separator, identifier = choice.partition(":")
        if not separator or kind not in ("scenario", "capture"):
            raise DomainError("VKS_INVALID_INPUT", "轨迹选择不合法")
        capture = None
        if kind == "capture":
            self.service.require(s, actor, analysis.project_id, "trace:read")
            capture = s.get(CaptureRecord, identifier)
            original = s.get(ScenarioRevision, capture.scenario_revision_id) if capture else None
            scene = scenes.get(original.scenario_id) if original else None
            if (
                not scene
                or not capture
                or capture.project_id != analysis.project_id
                or not ScenarioService(self.service)._capture_visible(s, actor, capture)
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到当前可见轨迹", 404)
            if not self.service._visible_revision(
                s, actor, analysis.project_id, s.get(Revision, original.entity_revision_id)
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到原始采集场景", 404)
            snapshots = capture.result.get("snapshots", [])
            if not snapshots:
                raise DomainError("VKS_INVALID_EVIDENCE", "录制没有可比较的快照")
            nodes = [
                dict(
                    capture_ref=dict(
                        scenario_id=scene.entity_id,
                        scenario_revision_id=scene.id,
                        capture_id=capture.id,
                        snapshot_index=i,
                    )
                )
                for i in range(len(snapshots))
            ]
            replay_references(self.service, s, actor, analysis, dict(nodes=nodes))
            dsl = original.dsl
            states = [
                dict(
                    key=f"snapshot-{i}",
                    label=row["name"],
                    evidence_origin="observed",
                    capture_id=capture.id,
                    snapshot_index=i,
                )
                for i, row in enumerate(snapshots)
            ]
            source_revision = original.entity_revision_id
        else:
            scene = scenes.get(identifier)
            if not scene:
                raise DomainError("VKS_NOT_FOUND", "未找到当前可见场景", 404)
            dsl = scene.metadata_json.get("dsl", {})
            ScenarioService(self.service)._validate(s, actor, analysis.project_id, analysis, dsl)
            source_revision = scene.id
            states = [
                dict(
                    key=row["stateKey"],
                    label=row["label"],
                    evidence_origin=row["evidenceOrigin"],
                    action_key=row.get("actionKey"),
                    page_key=row.get("pageKey"),
                    **(
                        dict(capture_id=row["captureRef"], snapshot_index=row.get("snapshotIndex", 0))
                        if row.get("captureRef")
                        else {}
                    ),
                )
                for row in dsl.get("explanation_graph", {}).get("states", [])
                if all(not row.get(f) or row[f] in keys for f in ("actionKey", "pageKey"))
            ]
        return dict(
            key=choice,
            scenario_id=scene.entity_id,
            scenario_revision_id=scene.id,
            source_scenario_revision_id=source_revision,
            capture_id=capture.id if capture else None,
            title=dsl.get("name", scene.name),
            role=dsl.get("role") or "未声明",
            preconditions=dsl.get("preconditions", []),
            expected_results=dsl.get("expected_results", []),
            states=states,
            state_count=len(states),
            evidence_origins=sorted({row["evidence_origin"] for row in states}),
            coverage="finite_observed_trajectory" if capture else "saved_declared_state_graph",
        )

    def options(self, actor, pid, aid=None, rid=None):
        with self.service.db() as s:
            analysis, release, scenes, keys = self._context(s, actor, pid, aid, rid)
            items = [
                dict(key="scenario:" + row.entity_id, scenario_id=row.entity_id, title=row.name, kind="scenario")
                for row in scenes.values()
            ]
            truncated = len(items) > 100
            items = items[:100]
            if self.service._can(s, actor, pid, "trace:read"):
                rows = s.scalars(
                    select(CaptureRecord)
                    .join(Job, CaptureRecord.job_id == Job.id)
                    .join(ScenarioRevision, CaptureRecord.scenario_revision_id == ScenarioRevision.id)
                    .where(
                        CaptureRecord.project_id == pid,
                        Job.state == "succeeded",
                        ScenarioRevision.scenario_id.in_(scenes),
                        CaptureRecord.result["baseline_sha256"].as_string()
                        == ScenarioService(self.service)._baseline_digest(s, analysis),
                    )
                    .order_by(CaptureRecord.created_at.desc())
                    .limit(100)
                )
                for row in rows:
                    try:
                        side = self._side(s, actor, analysis, scenes, keys, "capture:" + row.id)
                    except DomainError:
                        continue
                    items.append(
                        dict(
                            key=side["key"],
                            scenario_id=side["scenario_id"],
                            title=side["title"] + " · 录制 " + row.id[:8],
                            kind="capture",
                        )
                    )
            return dict(items=items, is_truncated=truncated)

    def compare(self, actor, pid, left, right, aid=None, rid=None):
        with self.service.db() as s:
            analysis, release, scenes, keys = self._context(s, actor, pid, aid, rid)
            sides = [self._side(s, actor, analysis, scenes, keys, value) for value in (left, right)]
            left_actions = {row["action_key"] for row in sides[0]["states"] if row.get("action_key")}
            right_actions = {row["action_key"] for row in sides[1]["states"] if row.get("action_key")}
            result = dict(
                project_id=pid,
                analysis_set_id=analysis.id,
                release_id=release.id if release else None,
                selection_version=analysis.selection_version,
                left=sides[0],
                right=sides[1],
                differences=dict(
                    role_changed=sides[0]["role"] != sides[1]["role"],
                    preconditions_changed=sides[0]["preconditions"] != sides[1]["preconditions"],
                    shared_declared_actions=sorted(left_actions & right_actions),
                    left_only_declared_actions=sorted(left_actions - right_actions),
                    right_only_declared_actions=sorted(right_actions - left_actions),
                ),
                source_vector=self.service._visible_sources(s, actor, pid, analysis.sources),
                mode="passive",
                unknowns=[
                    "角色和前置条件来自场景声明；录制快照不自动证明权限规则或后端执行因果",
                    "仅比较当前已保存范围，未覆盖角色或状态不能推断为相同；同名状态不自动合并",
                ],
            )
            result["comparison_sha256"] = sha(canonical(result))
            return result


def install_trajectory_comparison(app, service, actor, envelope):
    comparisons = TrajectoryService(service)

    @app.get("/api/v2/projects/{pid}/trajectory-options", response_model=Envelope[Options])
    def options(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, comparisons.options(a, pid, analysis_set_id, release_id))

    @app.get("/api/v2/projects/{pid}/trajectory-comparison", response_model=Envelope[Comparison])
    def compare(
        pid: str,
        request: Request,
        left: str = Query(..., max_length=80),
        right: str = Query(..., max_length=80),
        analysis_set_id: str | None = None,
        release_id: str | None = None,
        a=Depends(actor),
    ):
        return envelope(request, comparisons.compare(a, pid, left, right, analysis_set_id, release_id))
