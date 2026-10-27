from dataclasses import replace
from fastapi.testclient import TestClient
from test_journeys import store, journey
from vks_api.journeys import JourneyService
from vks_api.onboarding_report import report


def test_report_has_visible_denominators_candidates_not_confirmed_and_actionable_gaps(store):
    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    value = report(service, actor, pid, aid)
    assert value["counts"]["actions"] == len(w["actions"])
    assert value["counts"]["actions_with_confirmed_valid_links"] == 0
    assert value["counts"]["candidate_relations"] == len(w["relations"])
    assert next(g for g in value["gaps"] if g["code"] == "ACTION_EVIDENCE_GAP")["denominator"] == len(w["actions"])
    assert value["source_vector"] == w["sources"]
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    visible = service.workspace(limited, pid, aid)
    assert report(service, limited, pid, aid)["counts"]["relations"] == len(visible["relations"])
    with TestClient(app) as c:
        c.cookies.set("vks_session", token)
        assert (
            c.get(f"/api/v2/projects/{pid}/onboarding/report", params=dict(analysis_set_id=aid)).json()["data"] == value
        )


def test_review_inbox_excludes_exact_approved_journey(store):
    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    row = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "inbox-flow")
    with TestClient(app) as c:
        c.cookies.set("vks_session", token)
        path = f"/api/v2/projects/{pid}/review-inbox"
        assert any(
            i["subject_id"] == row["journey_id"] for i in c.get(path, params=dict(analysis_set_id=aid)).json()["data"]
        )
        journeys.review(
            actor,
            row["journey_id"],
            dict(
                analysis_set_id=aid, revision_id=row["revision_id"], decision="approve", reason="Synthetic inbox review"
            ),
            row["revision_id"],
            "inbox-review",
        )
        assert not any(
            i["subject_id"] == row["journey_id"] for i in c.get(path, params=dict(analysis_set_id=aid)).json()["data"]
        )
