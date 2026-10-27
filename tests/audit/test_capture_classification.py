from dataclasses import replace
from fastapi.testclient import TestClient
from test_capture_bindings import captured, binding_payload
from vks_api.app import create_app
from vks_api.models import Artifact
from vks_api.trajectory_comparison import TrajectoryService


def test_current_capture_classification_hides_assets_metadata_states_and_comparison_before_counts(captured):
    c, scenes, scene, capture = captured
    saved = scenes.bind_capture(
        c.owner, scene["scenario_id"], binding_payload(c, scene, capture), scene["revision_no"], "bind-classification"
    )
    aid = capture["result"]["snapshots"][0]["dom_artifact_id"]
    token = c.s.create_token(
        c.owner, c.p, dict(name="Trace without source", scopes=["knowledge:read", "knowledge:edit", "trace:read"])
    )["token"]
    limited = replace(c.owner, scopes=c.owner.scopes - {"source:read"})
    app = create_app(
        c.s.data_dir, database_url=c.s.engine.url.render_as_string(hide_password=False), enable_worker=False
    )
    try:
        with TestClient(app) as api:
            headers = {"Authorization": "Bearer " + token}
            assert api.get(f"/api/v1/captures/{capture['id']}/artifacts/{aid}", headers=headers).status_code == 200
            with c.s.db() as db:
                art = db.get(Artifact, aid)
                art.metadata_json = dict(art.metadata_json, requires_source_read=True)
            assert api.get(f"/api/v1/captures/{capture['id']}/artifacts/{aid}", headers=headers).status_code == 404
            assert api.get(f"/api/v1/projects/{c.p}/captures", headers=headers).json()["data"] == []
            assert not c.s.workspace(limited, c.p, c.aid)["scenarios"]
            options = TrajectoryService(c.s).options(limited, c.p, c.aid)
            assert options["items"] == [] and options["is_truncated"] is False
            assert (
                c.s.workspace(c.owner, c.p, c.aid)["scenarios"][0]["entity_revision_id"] == saved["entity_revision_id"]
            )
    finally:
        app.state.service.engine.dispose()
