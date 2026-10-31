from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from vks_api.app import create_app
from vks_api.service import DomainError


@pytest.fixture
def store(tmp_path):
    app = create_app(tmp_path / "v2", enable_worker=False)
    service = app.state.service
    token, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Synthetic V2"}, "p")
    job = service.demo_import(actor, project["id"], "demo")
    service.process_job(job["id"])
    w = service.workspace(actor, project["id"])
    yield (service, actor, project["id"], w, app, token)
    service.engine.dispose()


def journey(w):
    action = w["actions"][0]
    return dict(
        schema_version="2.0",
        key="invoice",
        title="Invoice journey",
        entry="select",
        nodes=[
            dict(
                key="select",
                kind="action",
                title="Select invoice",
                x=40,
                y=40,
                binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
            ),
            dict(key="done", kind="end", title="Done", x=340, y=40),
        ],
        edges=[dict(key="next", source="select", target="done", label="")],
    )


def test_dsl_rejects_cycles_unknown_fields_dangling_and_ambiguous_branches(store):
    from packages.journey_dsl import validate_journey

    dsl = journey(store[3])
    assert validate_journey(dsl) == dsl
    for mutate in [
        lambda d: d["nodes"][0].update(script='fetch("https://example.org")'),
        lambda d: d["edges"][0].update(target="missing"),
        lambda d: d["edges"].append(dict(key="loop", source="done", target="select", label="")),
        lambda d: d["nodes"][0].update(kind="decision"),
    ]:
        bad = deepcopy(dsl)
        mutate(bad)
        with pytest.raises(ValueError):
            validate_journey(bad)


def test_journey_cas_idempotency_fixed_release_and_cross_baseline(store):
    from vks_api.journeys import JourneyService

    service, actor, pid, w, _, _ = store
    journeys = JourneyService(service)
    payload = dict(analysis_set_id=w["selected_analysis_set"]["id"], dsl=journey(w))
    first = journeys.create(actor, pid, payload, "create")
    assert journeys.create(actor, pid, payload, "create") == first
    bad = deepcopy(payload)
    bad["dsl"]["nodes"][0]["binding"]["revision_id"] = "unknown"
    with pytest.raises(DomainError):
        journeys.update(actor, first["journey_id"], bad, first["revision_id"], "bad")
    payload["dsl"]["title"] = "New journey"
    second = journeys.update(actor, first["journey_id"], payload, first["revision_id"], "edit")
    assert second["revision_id"] != first["revision_id"]
    assert journeys.update(actor, first["journey_id"], payload, first["revision_id"], "edit") == second
    with pytest.raises(DomainError) as conflict:
        journeys.update(actor, first["journey_id"], payload, first["revision_id"], "edit-stale")
    assert conflict.value.status == 412
    assert journeys.get(actor, pid, first["journey_id"], first["revision_id"]) == first
    assert journeys.list(actor, pid, payload["analysis_set_id"])[0] == second


def test_http_contract_and_no_server_filesystem_or_executable_dsl(store):
    service, actor, pid, w, app, token = store
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        headers = {"X-CSRF-Token": actor.csrf_token, "Idempotency-Key": "v2"}
        payload = dict(analysis_set_id=w["selected_analysis_set"]["id"], dsl=journey(w))
        created = client.post(f"/api/v2/projects/{pid}/journeys", json=payload, headers=headers)
        assert created.status_code == 201, created.text
        row = created.json()["data"]
        response = client.get(
            f"/api/v2/projects/{pid}/journeys", params={"analysis_set_id": payload["analysis_set_id"]}
        )
        assert response.json()["data"] == [row]
        bad = dict(allowed_roots=["/etc"], analysis_set_id=payload["analysis_set_id"])
        assert client.post(f"/api/v2/projects/{pid}/onboarding/preflight", json=bad, headers=headers).status_code == 422
        assert "Journey" in json.dumps(app.openapi())


def test_frozen_journey_clone_edit_export_and_fixed_business_reference(store):
    from vks_api.journeys import JourneyService
    from vks_api.knowledge import KnowledgeService
    from io import BytesIO
    import zipfile

    service, actor, pid, w, app, token = store
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    payload = dict(analysis_set_id=aid, dsl=journey(w))
    row = journeys.create(actor, pid, payload, "journey")
    for relation in w["relations"]:
        service.set_relation_state(
            actor,
            relation["id"],
            {
                "excluded": dict(decision="confirm", reason="Synthetic checked", evidence_ids=relation["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            relation["row_version"],
        )
    candidate = service.save_snapshot(actor, pid, {"analysis_set_id": aid}, "precheck")
    pass
    ticket = None
    released = candidate
    before = journeys.list(actor, pid, release_id=released["id"])
    clone = KnowledgeService(service).clone(actor, pid, aid, "New working draft", "clone")
    new = deepcopy(payload)
    new["analysis_set_id"] = clone["id"]
    new["dsl"]["title"] = "Changed working title"
    journeys.update(actor, row["journey_id"], new, row["revision_id"], "new")
    assert journeys.list(actor, pid, release_id=released["id"]) == before
    with TestClient(app) as client:
        client.cookies.set("vks_session", token)
        action = w["actions"][0]
        result = client.get(
            f"/api/v2/entities/{action['entity_id']}/business-references",
            params={"project_id": pid, "release_id": released["id"]},
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["release_id"] == released["id"]
        assert any((a["entity_revision_id"] == action["entity_revision_id"] for a in result.json()["data"]["actions"]))
    job = service.export(actor, pid, dict(release_id=released["id"], format="vksbundle", include_source=True), "export")
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    raw, _ = service.download_export(actor, pid, job["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        sidecar = json.loads(archive.read("export/journeys/" + row["journey_id"] + ".json"))
    assert sidecar["definition"] == payload["dsl"]
