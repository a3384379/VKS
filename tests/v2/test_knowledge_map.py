from dataclasses import replace
from fastapi.testclient import TestClient
from test_journeys import store
from packages.collector.snapshots import Snapshot, bundle_bytes


def add_map(service, actor, pid, aid, count=150):
    content = (
        '<main data-vks-page="ui.page.map-many"><h1>Map fixture</h1>'
        + "".join(f'<button data-vks-action="ui.action.map-{i}">Map action {i}</button>' for i in range(count))
        + "</main>"
    ).encode()
    raw = bundle_bytes(
        Snapshot("local", "synthetic-map", {"map.html": content}),
        "charge-demo",
        "map-fixture",
        source_key="map-ui",
        role="prototype",
    )
    parsed = service._parse(raw, "charge-demo")
    with service.db() as s:
        service._commit_bundle(s, pid, actor.id, parsed, aid)
    return next(row for row in service.workspace(actor, pid, aid)["pages"] if row["stable_key"] == "ui.page.map-many")


def test_bounded_map_api_returns_more_than_13_nodes_and_truncates_after_permissions(store):
    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    page = add_map(service, actor, pid, aid)
    with TestClient(app) as c:
        c.cookies.set("vks_session", token)
        path = f"/api/v2/entities/{page['entity_id']}/knowledge-map"
        params = dict(project_id=pid, analysis_set_id=aid, depth=1, max_nodes=100)
        row = c.get(path, params=params)
        assert row.status_code == 200, row.text
        data = row.json()["data"]
        assert len(data["nodes"]) == 100 and data["truncated"] is True
        trusted = c.get(path, params=dict(params, confirmed_only=True)).json()["data"]
        assert len(trusted["nodes"]) == 1 and not trusted["edges"] and not trusted["truncated"]
        assert c.get(path, params=dict(params, max_nodes=101)).status_code == 422
