"""Frozen VKS scenario format and semantic validation. Never executes source text."""

from __future__ import annotations
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/scenario.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


class ScenarioError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_dsl(
    dsl, *, project_key=None, baseline_key=None, action_keys=None, action_ids=None, capture_validator=None
):
    from copy import deepcopy

    dsl = deepcopy(dsl)
    dsl.pop("approval_ref", None)  # compatibility for immutable pre-personal snapshots
    for step in dsl.get("steps", []):
        step.pop("redact", None)  # legacy immutable definitions
    errors = sorted(VALIDATOR.iter_errors(dsl), key=lambda x: str(list(x.path)))
    if errors:
        raise ScenarioError(
            "VKS_SCENARIO_INVALID", "场景字段不符合正式 DSL 契约：" + ".".join(map(str, errors[0].absolute_path))
        )

    keys = [s["step_key"] for s in dsl["steps"]]
    if len(keys) != len(set(keys)):
        raise ScenarioError("VKS_SCENARIO_INVALID", "步骤逻辑键必须唯一")
    if sum(s["timeout_ms"] for s in dsl["steps"]) > dsl["timeout_ms"]:
        raise ScenarioError("VKS_SCENARIO_INVALID", "各步骤超时总和不能超过场景总预算")
    for step in dsl["steps"]:
        if step["kind"] == "goto":
            path = unquote(step["path"])
            if path.startswith("//") or "\\" in path or urlsplit(path).netloc or any(ord(c) < 32 for c in path):
                raise ScenarioError("VKS_SCENARIO_INVALID", "跳转仅允许登记环境下的相对路径")
        if step.get("action_entity_id") and action_ids is not None and step["action_entity_id"] not in action_ids:
            raise ScenarioError("VKS_SCENARIO_INVALID", "步骤动作不属于当前版本")
    graph = dsl.get("explanation_graph")
    if not graph:
        return dsl
    if (project_key is not None and graph["projectKey"] != project_key) or (
        baseline_key is not None and graph["baselineKey"] != baseline_key
    ):
        raise ScenarioError("VKS_SCENARIO_INVALID", "讲解状态图不属于当前项目和版本")
    state_keys = [s["stateKey"] for s in graph["states"]]
    transition_keys = [t["transitionKey"] for t in graph["transitions"]]
    if len(state_keys) != len(set(state_keys)) or len(transition_keys) != len(set(transition_keys)):
        raise ScenarioError("VKS_SCENARIO_INVALID", "状态与转换键必须唯一")
    for item in [*graph["states"], *graph["transitions"]]:
        if item["evidenceOrigin"] == "observed" and (capture_validator is None or not capture_validator(item)):
            raise ScenarioError("VKS_INVALID_EVIDENCE", "运行观察必须指向当前基线中经过核验的采集与证据")
    for transition in graph["transitions"]:
        if transition["fromState"] not in state_keys or transition["toState"] not in state_keys:
            raise ScenarioError("VKS_SCENARIO_INVALID", "状态转换引用了不存在的状态")
        if action_keys is not None and transition["actionKey"] not in action_keys:
            raise ScenarioError("VKS_SCENARIO_INVALID", "状态转换动作不属于当前版本")
        if transition["scenarioRef"] != dsl["scenario_key"]:
            raise ScenarioError("VKS_SCENARIO_INVALID", "状态转换必须属于当前场景")
    return dsl
