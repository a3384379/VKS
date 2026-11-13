"""Real API tests for the new DOM links, using synthetic offline packages only."""

from base64 import b64encode
from gzip import compress

import pytest
from sqlalchemy import select
from vks_api.knowledge import ImportService
from vks_api.models import Artifact
from vks_api.prototype import preview_fingerprint

from packages.analyzers.vks_ingest import canonical_json
from tests.prototype import test_v3_workflow
from tests.prototype.test_v3_workflow import _headers, _upload

workspace = test_v3_workflow.workspace


@pytest.fixture
def interactive(workspace):
    _app, web, service, owner, pid, aid, _, grant = workspace
    payload = canonical_json(
        {
            "routeMap": {"/demo": "page.html"},
            "pages": {"page.html": {"title": "Synthetic page", "html": '<button id="add">Add</button>'}},
            "resources": [],
            "repoAssets": {},
        }
    )
    raw = (
        b'<script id="prototypePackPayload" type="application/octet-stream">'
        + b64encode(compress(payload))
        + b"""</script><title>&#25910;&#36153;&#39033;&#31649;&#29702;</title><script>window.__prototypePackPage={path:"page.html"};</script><button id="add" onclick="document.querySelector('#dialog').hidden=false">Add</button><div id="dialog" role="dialog" hidden><input aria-label="Code" placeholder="Code"><button onclick="document.querySelector('#dialog').hidden=true">Close</button></div>"""
    )
    payload = {
        "kind": "prototype",
        "artifact_ids": [_upload(service, owner, pid, raw, "prototype.html")],
        "analysis_set_id": aid,
    }
    parsed = ImportService(service).prepare(actor=owner, pid=pid, payload=payload)
    selected = service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"]
    with service.db() as db:
        job = service._new_job(
            db,
            owner,
            pid,
            "import",
            {
                **payload,
                "_prototype_preview": {
                    "selection_version": selected,
                    "digest": preview_fingerprint(parsed, payload, selected),
                },
            },
        )
        jid = job.id
    service.process_job(jid)
    assert service.get_job(owner, jid)["state"] == "succeeded"
    imported = aid
    with service.db() as db:
        art = db.scalar(
            select(Artifact).where(Artifact.project_id == pid, Artifact.relative_path == "offline/original.html")
        )
        package_id = art.id
    section = next(row for row in service.workspace(owner, pid, imported)["entities"] if row["type"] == "section")
    yield web, service, owner, pid, imported, grant, package_id, section, raw


def root(pid):
    return f"/api/v3/projects/{pid}/interactive"


def form(aid, package, section):
    return {
        "analysis_set_id": aid,
        "package_id": package,
        "target": {
            "route": "/page.html",
            "frame_path": ["actualPage"],
            "selector": "#add",
            "label": "Add",
            "state": "",
            "open_steps": [],
        },
        "point": {"title": "Add rule", "summary": "Synthetic persistent knowledge"},
        "section_revision_ids": [section["entity_revision_id"]],
    }


def current(web, pid, aid, grant):
    r = web.get(root(pid) + "/catalog", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read"))
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_persistent_bidirectional_binding_idempotency_and_conflicts(interactive):
    web, service, owner, pid, aid, grant, package, section, raw = interactive
    catalog = current(web, pid, aid, grant)
    assert catalog["packages"][0]["id"] == package
    v = catalog["selection_version"]
    body = form(aid, package, section)
    response = web.post(root(pid) + "/bindings", json=body, headers=_headers(grant, v, "bind"))
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    assert web.post(root(pid) + "/bindings", json=body, headers=_headers(grant, v, "bind")).json()["data"] == saved
    assert web.post(root(pid) + "/bindings", json=body, headers=_headers(grant, v, "stale")).status_code == 409
    links = web.get(
        root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")
    ).json()["data"]
    assert len(links) == 1
    binding = links[0]
    assert binding["target"] == body["target"]
    assert binding["summary"] == body["point"]["summary"]
    assert binding["sections"][0]["revision_id"] == section["entity_revision_id"]
    doc = web.get(
        root(pid) + "/documents/" + binding["sections"][0]["document_revision_id"],
        params={"analysis_set_id": aid},
        headers=_headers(grant, 0, "read"),
    ).json()["data"]
    assert any(s["revision_id"] == section["entity_revision_id"] for s in doc["sections"])
    body.update(
        binding_id=binding["id"],
        binding_version=binding["binding_version"],
        point_revision_id=binding["point_revision_id"],
    )
    body["point"]["summary"] = "Revised explanation"
    updated = web.post(
        root(pid) + "/bindings", json=body, headers=_headers(grant, saved["selection_version"], "update")
    )
    assert updated.status_code == 200, updated.text
    # A frozen snapshot retains the earlier exact point and citations.
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    frozen = web.get(root(pid) + "/bindings", params={"release_id": release["id"]}, headers=_headers(grant, 0, "read"))
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["data"][0]["summary"] == "Revised explanation"
    assert (
        web.post(
            root(pid) + "/bindings",
            json=body,
            headers=_headers(grant, updated.json()["data"]["selection_version"], "frozen"),
        ).status_code
        == 409
    )
    response = web.get(
        root(pid) + f"/packages/{package}/content",
        params={"release_id": release["id"]},
        headers=_headers(grant, 0, "read"),
    )
    assert response.content == raw and response.headers["content-type"] == "application/octet-stream"
    reader = service.create_token(owner, pid, {"name": "snapshot-reader", "scopes": ["knowledge:read"]})
    response = web.get(
        root(pid) + f"/packages/{package}/content",
        params={"release_id": release["id"]},
        headers=_headers(reader, 0, "reader"),
    )
    assert response.status_code == 200, response.text
    assert response.content == raw


def test_cross_project_exact_section_and_revocation(interactive):
    web, service, owner, pid, aid, grant, package, section, _ = interactive
    v = current(web, pid, aid, grant)["selection_version"]
    body = form(aid, package, section)
    body["section_revision_ids"] = [package]
    assert web.post(root(pid) + "/bindings", json=body, headers=_headers(grant, v, "bad-section")).status_code == 404
    other = service.create_project(owner, {"key": "other", "name": "Other"}, "other")["id"]
    assert web.get(
        root(other) + f"/packages/{package}/content",
        params={"analysis_set_id": aid},
        headers=_headers(grant, 0, "read"),
    ).status_code in (403, 404)
    # Entire package is withheld if a contained selected material becomes private.
    with service.db() as db:
        member = db.scalar(
            select(Artifact).where(Artifact.project_id == pid, Artifact.relative_path == "offline/prototype.json")
        )
        member.sensitivity = "confidential"
    assert current(web, pid, aid, grant)["packages"] == []
    assert (
        web.get(
            root(pid) + f"/packages/{package}/content",
            params={"analysis_set_id": aid},
            headers=_headers(grant, 0, "read"),
        ).status_code
        == 404
    )
    service.revoke_token(owner, pid, grant["id"])
    assert (
        web.get(root(pid) + "/catalog", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")).status_code
        == 401
    )


def test_remove_restore_is_versioned_and_does_not_duplicate(interactive):
    web, _, _, pid, aid, grant, package, section, _ = interactive
    v = current(web, pid, aid, grant)["selection_version"]
    saved = web.post(
        root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, v, "bind")
    ).json()["data"]
    query = {"analysis_set_id": aid}
    link = web.get(root(pid) + "/bindings", params=query, headers=_headers(grant, 0, "read")).json()["data"][0]
    body = {"analysis_set_id": aid, "binding_version": link["binding_version"], "excluded": True}
    result = web.post(
        root(pid) + f"/bindings/{link['id']}/exclude",
        json=body,
        headers=_headers(grant, saved["selection_version"], "remove"),
    )
    assert result.status_code == 200, result.text
    link = web.get(root(pid) + "/bindings", params=query, headers=_headers(grant, 0, "read")).json()["data"][0]
    assert link["excluded"]
    body.update(binding_version=link["binding_version"], excluded=False)
    result = web.post(
        root(pid) + f"/bindings/{link['id']}/exclude",
        json=body,
        headers=_headers(grant, result.json()["data"]["selection_version"], "restore"),
    )
    assert result.status_code == 200, result.text
    links = web.get(root(pid) + "/bindings", params=query, headers=_headers(grant, 0, "read")).json()["data"]
    assert len(links) == 1 and not links[0]["excluded"]
