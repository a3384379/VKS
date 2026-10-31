
from test_journeys import store as store  # pytest fixture registration
from dataclasses import replace
from fastapi.testclient import TestClient
from test_journeys import journey
from vks_api.journeys import JourneyService
from vks_api.onboarding_report import report


def test_report_has_visible_denominators_candidates_not_confirmed_and_actionable_gaps(store):
    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    value = report(service, actor, pid, aid)
    assert value["counts"]["actions"] == len(w["actions"])
    assert value["counts"]["actions_with_valid_links"] == len(w["actions"])
    assert value["counts"]["excluded_relations"] == 0
    assert not any(g["code"] in ("UNREVIEWED_RELATIONS", "ACTION_EVIDENCE_GAP") for g in value["gaps"])
    assert value["source_vector"] == w["sources"]
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    visible = service.workspace(limited, pid, aid)
    assert report(service, limited, pid, aid)["counts"]["relations"] == len(visible["relations"])
    with TestClient(app) as c:
        c.cookies.set("vks_session", token)
        assert (
            c.get(f"/api/v2/projects/{pid}/onboarding/report", params=dict(analysis_set_id=aid)).json()["data"] == value
        )


def test_journey_is_immediately_readable_and_approval_inbox_is_removed(store):
    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    row = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "direct-flow")
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        assert client.get(f"/api/v2/projects/{pid}/review-inbox", params=dict(analysis_set_id=aid)).status_code == 404
        actual = client.get(f"/api/v2/projects/{pid}/journeys", params=dict(analysis_set_id=aid)).json()["data"]
        assert any(item["journey_id"] == row["journey_id"] for item in actual)
