from copy import deepcopy
from dataclasses import replace
import pytest
from test_capture_bindings import captured
from vks_api.trajectory_comparison import TrajectoryService
from vks_api.service import DomainError


def test_saved_roles_and_recordings_compare_exact_baselines_without_mutating_scene(captured):
    c, scenes, scene, capture = captured
    service = TrajectoryService(c.s)
    data = deepcopy(scene["dsl"])
    data["scenario_key"] = "second-role"
    data["name"] = "Read-only role"
    data["role"] = "auditor"
    data["preconditions"] = ["Synthetic permission denied"]
    data["expected_results"] = ["Read-only page"]
    other = scenes.create(c.owner, c.p, dict(analysis_set_id=c.aid, dsl=data), "other-role")
    before = scenes.list(c.owner, c.p, c.aid)
    row = service.compare(c.owner, c.p, "scenario:" + scene["scenario_id"], "scenario:" + other["scenario_id"], c.aid)
    assert row["differences"]["role_changed"] and row["differences"]["preconditions_changed"]
    assert row["left"]["coverage"] == "saved_declared_state_graph" and row["mode"] == "passive"
    assert (
        row["comparison_sha256"]
        == service.compare(c.owner, c.p, "scenario:" + scene["scenario_id"], "scenario:" + other["scenario_id"], c.aid)[
            "comparison_sha256"
        ]
    )
    observed = service.compare(c.owner, c.p, "capture:" + capture["id"], "scenario:" + other["scenario_id"], c.aid)
    assert observed["left"]["source_scenario_revision_id"] == scene["entity_revision_id"]
    assert observed["left"]["states"][0]["capture_id"] == capture["id"]
    assert observed["left"]["states"][0]["snapshot_index"] == 0
    assert observed["differences"]["shared_declared_actions"] == []
    assert scenes.list(c.owner, c.p, c.aid) == before
    assert any(item["key"] == "capture:" + capture["id"] for item in service.options(c.owner, c.p, c.aid)["items"])


def test_trajectory_comparison_filters_permissions_before_options_names_and_counts(captured):
    c, scenes, scene, capture = captured
    service = TrajectoryService(c.s)
    limited = replace(c.owner, scopes=c.owner.scopes - {"trace:read"})
    options = service.options(limited, c.p, c.aid)
    assert all(item["kind"] != "capture" for item in options["items"])
    with pytest.raises(DomainError):
        service.compare(limited, c.p, "capture:" + capture["id"], "scenario:" + scene["scenario_id"], c.aid)
    with pytest.raises(DomainError):
        service.compare(c.owner, c.p, "capture:missing", "scenario:" + scene["scenario_id"], c.aid)
