"""Validate passive replay references without running any captured instructions."""

from .models import Artifact, Entity, Job, Selection, Revision
from .scenarios import CaptureRecord, ScenarioRevision, ScenarioService
from .service import DomainError


def replay_references(service, s, actor, analysis, dsl):
    revisions, artifacts = {}, {}
    for node in dsl["nodes"]:
        ref = node.get("state_ref") or node.get("capture_ref")
        if not ref:
            continue
        choice = s.get(Selection, (analysis.id, ref["scenario_id"]))
        scenario = s.get(Revision, ref["scenario_revision_id"])
        if (
            not choice
            or choice.entity_revision_id != ref["scenario_revision_id"]
            or not scenario
            or scenario.entity_id != ref["scenario_id"]
            or scenario.project_id != analysis.project_id
            or s.get(Entity, scenario.entity_id).type != "scenario"
            or actor is not None
            and not service._visible_revision(s, actor, analysis.project_id, scenario)
        ):
            raise DomainError("VKS_NOT_FOUND", "回放场景不属于当前可见版本", 404)
        revisions[scenario.id] = scenario
        if node.get("state_ref"):
            states = scenario.metadata_json.get("dsl", {}).get("explanation_graph", {}).get("states", [])
            state = next((state for state in states if state["stateKey"] == ref["state_key"]), None)
            if not state:
                raise DomainError("VKS_NOT_FOUND", "回放状态不存在", 404)
            ScenarioService(service)._validate(s, actor, analysis.project_id, analysis, scenario.metadata_json["dsl"])
            binding = node.get("binding")
            if binding:
                bound = s.get(Revision, binding["revision_id"])
                entity = s.get(Entity, bound.entity_id)
                if entity.type == "action" and state.get("actionKey") and entity.stable_key != state["actionKey"]:
                    raise DomainError("VKS_INVALID_INPUT", "动作与状态的准确绑定不一致")
                if entity.type == "page" and state.get("pageKey") and entity.stable_key != state["pageKey"]:
                    raise DomainError("VKS_INVALID_INPUT", "页面与状态的准确绑定不一致")
        else:
            if actor is not None:
                service.require(s, actor, analysis.project_id, "trace:read")
            capture = s.get(CaptureRecord, ref["capture_id"])
            source = s.get(ScenarioRevision, capture.scenario_revision_id) if capture else None
            job = s.get(Job, capture.job_id) if capture else None
            if (
                not capture
                or capture.project_id != analysis.project_id
                or capture.state != "succeeded"
                or not job
                or job.state != "succeeded"
                or not source
                or source.scenario_id != scenario.entity_id
                or capture.result.get("baseline_sha256") != ScenarioService(service)._baseline_digest(s, analysis)
            ):
                raise DomainError("VKS_INVALID_EVIDENCE", "录制与当前知识来源基线不一致", 409)
            snapshots = capture.result.get("snapshots", [])
            if not 0 <= ref["snapshot_index"] < len(snapshots):
                raise DomainError("VKS_INVALID_EVIDENCE", "录制快照不存在")
            for field in ("dom_artifact_id", "png_artifact_id"):
                art = s.get(Artifact, snapshots[ref["snapshot_index"]].get(field))
                if (
                    not art
                    or art.project_id != analysis.project_id
                    or art.snapshot_id != capture.id
                    or art.kind != "capture"
                ):
                    raise DomainError("VKS_INVALID_EVIDENCE", "录制快照制品归属不一致")
                from .reading import artifact_policy

                if actor is not None and (
                    art.sensitivity == "confidential" or artifact_policy(s, art).get("requires_source_read")
                ):
                    service.require(s, actor, analysis.project_id, "source:read")
                artifacts[art.id] = art
    return list(revisions.values()), list(artifacts.values())
