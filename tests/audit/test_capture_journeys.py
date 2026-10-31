
from test_capture_bindings import captured as captured  # pytest fixture registration
from copy import deepcopy
from dataclasses import replace
import pytest
from vks_api.capture_proposals import CaptureJourneyService
from vks_api.journeys import JourneyService
from vks_api.service import DomainError


def payload(c, scenario, captures):
    return dict(
        analysis_set_id=c.aid,
        scenario_id=scenario["scenario_id"],
        scenario_revision_id=scenario["entity_revision_id"],
        capture_ids=[row["id"] for row in captures],
    )


def test_recorded_snapshot_proposal_is_passive_immutable_and_exactly_bound(captured):
    c, scenarios, scenario, capture = captured
    before = deepcopy(scenario)
    work = c.s.workspace(c.owner, c.p, c.aid)
    proposals = CaptureJourneyService(c.s)
    data = payload(c, scenario, [capture])
    row = proposals.create(c.owner, c.p, data, str(work["selected_analysis_set"]["selection_version"]), "proposal")
    assert (
        proposals.create(c.owner, c.p, data, str(work["selected_analysis_set"]["selection_version"]), "proposal") == row
    )
    nodes = [node for node in row["dsl"]["nodes"] if node.get("capture_ref")]
    assert row["dsl"]["semantics"]["kind"] == "operation_sequence"
    assert row["dsl"]["semantics"]["basis"] == "observed"
    assert row["dsl"]["semantics"]["source_refs"] == [
        dict(entity_id=scenario["scenario_id"], revision_id=scenario["entity_revision_id"])
    ]
    assert len(nodes) == len(capture["result"]["snapshots"])
    assert nodes[0]["capture_ref"]["capture_id"] == capture["id"]
    assert nodes[0]["binding"]["revision_id"] == scenario["entity_revision_id"]
    assert scenarios.get(c.owner, c.p, scenario["scenario_id"], scenario["scenario_revision_id"]) == before
    limited = replace(c.owner, scopes=c.owner.scopes - {"trace:read"})
    assert not JourneyService(c.s).list(limited, c.p, c.aid)
    assert all((r["excluded"] is False for r in c.s.workspace(c.owner, c.p, c.aid)["relations"]))


@pytest.mark.parametrize("case", ["no_trace", "wrong_revision", "wrong_capture", "stale_analysis", "duplicate_capture"])
def test_capture_proposal_rejects_invalid_context_without_saving(captured, case):
    c, scenarios, scenario, capture = captured
    data = payload(c, scenario, [capture])
    actor = c.owner
    version = str(c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"])
    if case == "no_trace":
        actor = replace(actor, scopes=actor.scopes - {"trace:read"})
    elif case == "wrong_revision":
        data["scenario_revision_id"] = "missing"
    elif case == "wrong_capture":
        data["capture_ids"] = ["missing"]
    elif case == "stale_analysis":
        version = "0"
    elif case == "duplicate_capture":
        data["capture_ids"] *= 2
    with pytest.raises(DomainError):
        CaptureJourneyService(c.s).create(actor, c.p, data, version, "invalid")
    assert not JourneyService(c.s).list(c.owner, c.p, c.aid)


def test_multiple_recordings_become_finite_candidate_branches(captured):
    c, scenarios, scenario, first = captured
    approval = dict(
        analysis_set_id=c.aid,
        scenario_revision_id=scenario["scenario_revision_id"],
        reason="Synthetic second trajectory",
    )
    job = scenarios.request_capture(
        c.owner,
        scenario["scenario_id"],
        dict(
            scenario_revision_id=scenario["scenario_revision_id"],
            environment_ref="audit",
            analysis_set_id=approval["analysis_set_id"],
        ),
        "second",
    )
    c.s.process_job(job["job"]["id"])
    second = next((row for row in scenarios.captures(c.owner, c.p) if row["id"] != first["id"]))
    version = str(c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"])
    row = CaptureJourneyService(c.s).create(c.owner, c.p, payload(c, scenario, [first, second]), version, "branches")
    assert row["dsl"]["semantics"]["kind"] == "observation_comparison"
    assert next((n for n in row["dsl"]["nodes"] if n["key"] == row["dsl"]["entry"]))["kind"] == "decision"
    assert len([e for e in row["dsl"]["edges"] if e["source"] == row["dsl"]["entry"]]) == 2


def test_replay_journey_clone_rebinds_draft_and_preserves_frozen_release(captured):
    from vks_api.knowledge import KnowledgeService
    from vks_api.models import Analysis
    from vks_api.versions import VersionService

    c, scenarios, scenario, capture = captured
    version = str(c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"])
    journeys = JourneyService(c.s)
    row = CaptureJourneyService(c.s).create(c.owner, c.p, payload(c, scenario, [capture]), version, "freeze-proposal")
    for relation in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.set_relation_state(
            c.owner,
            relation["id"],
            {
                "excluded": dict(
                    decision="confirm", reason="Synthetic baseline", evidence_ids=relation["evidence_ids"]
                )["decision"]
                == "reject"
            },
            relation["row_version"],
        )
    candidate = c.s.save_snapshot(c.owner, c.p, {"analysis_set_id": c.aid}, "freeze")
    pass
    ticket = None
    release = candidate
    frozen = journeys.list(c.owner, c.p, release_id=release["id"])
    clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "Replay working draft", "clone")
    copied = journeys.list(c.owner, c.p, clone["id"])[0]
    selected_scene = scenarios.list(c.owner, c.p, clone["id"])[0]
    assert copied["dsl"]["semantics"]["source_refs"][0]["revision_id"] == selected_scene["entity_revision_id"]
    assert copied["revision_id"] != row["revision_id"]
    for node in copied["dsl"]["nodes"]:
        if node.get("capture_ref"):
            assert node["capture_ref"]["scenario_revision_id"] == selected_scene["entity_revision_id"]
            assert node["capture_ref"]["capture_id"] == capture["id"]
            assert node["binding"]["revision_id"] == selected_scene["entity_revision_id"]
    assert journeys.list(c.owner, c.p, release_id=release["id"]) == frozen
    assert journeys.get(c.owner, c.p, row["journey_id"], row["revision_id"]) == row
    with c.s.db() as db:
        assert journeys.assert_publishable(db, db.get(Analysis, clone["id"])) == []
        diff = VersionService(c.s).compare(db, c.p, release["id"], clone["id"])
        assert any((item["binding_entity_id"] == scenario["scenario_id"] for item in diff["journey_impacts"]))


def test_saved_state_graph_creates_passive_branch_candidate_and_rejects_cycles(captured):
    c, scenarios, scenario, capture = captured
    dsl = deepcopy(scenario["dsl"])
    graph = dsl["explanation_graph"]
    graph["states"].append(dict(stateKey="other", label="Other declared branch", evidenceOrigin="declared"))
    graph["transitions"] = [
        dict(
            transitionKey=f"to-{key}",
            fromState="declared",
            toState=key,
            conditionLabel="Review branch",
            evidenceOrigin="declared",
            actionKey=c.w["actions"][0]["stable_key"],
            scenarioRef=dsl["scenario_key"],
        )
        for key in ("simulated", "other")
    ]
    saved = scenarios.update(
        c.owner, scenario["scenario_id"], dict(analysis_set_id=c.aid, dsl=dsl), scenario["revision_no"]
    )
    version = str(c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"])
    row = CaptureJourneyService(c.s).create(c.owner, c.p, payload(c, saved, []), version, "states")
    assert row["dsl"]["semantics"]["kind"] == "operation_sequence"
    assert row["dsl"]["semantics"]["basis"] == "declared"
    assert {n["state_ref"]["state_key"] for n in row["dsl"]["nodes"] if n.get("state_ref")} == {
        "declared",
        "simulated",
        "other",
    }
    assert len([n for n in row["dsl"]["nodes"] if n["kind"] == "decision"]) == 1
    assert all(("capture_ref" not in n for n in row["dsl"]["nodes"]))
    assert scenarios.get(c.owner, c.p, scenario["scenario_id"], saved["scenario_revision_id"])["dsl"] == dsl
    dsl["explanation_graph"]["transitions"].append(
        dict(
            transitionKey="loop",
            fromState="simulated",
            toState="declared",
            conditionLabel="Loop",
            evidenceOrigin="declared",
            actionKey=c.w["actions"][0]["stable_key"],
            scenarioRef=dsl["scenario_key"],
        )
    )
    latest = scenarios.update(
        c.owner, scenario["scenario_id"], dict(analysis_set_id=c.aid, dsl=dsl), saved["revision_no"]
    )
    version = str(c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"])
    with pytest.raises(DomainError):
        CaptureJourneyService(c.s).create(c.owner, c.p, payload(c, latest, []), version, "cycle")
    assert len(JourneyService(c.s).list(c.owner, c.p, c.aid)) == 1
