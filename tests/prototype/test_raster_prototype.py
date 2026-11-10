from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vks_api.app import create_app
from vks_api.models import Artifact

from packages.analyzers.vks_ingest import BundleError, parse_bundle
from scripts.build_prototype_demo import PAGE_KEY, build_bundle


def changed_state(page):
    page["states"][0]["viewport"]["width"] += 1


def foreign_state(page):
    page["states"][0]["anchors"][0]["targetStateKey"] = "another-page-state"


def overlap(page):
    page["states"][0]["anchors"][1]["rect"] = page["states"][0]["anchors"][0]["rect"]


def bad_hash(page):
    page["states"][0]["surface"]["sha256"] = "0" * 64


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (changed_state, "VKS_LOCATOR_INVALID"),
        (foreign_state, "VKS_REFERENCE_INVALID"),
        (overlap, "VKS_LOCATOR_CONFLICT"),
        (bad_hash, "VKS_REFERENCE_INVALID"),
    ],
)
def test_rejects_invalid_raster_manifests(edit, code):
    with pytest.raises(BundleError) as exc:
        parse_bundle(build_bundle(edit=edit), "prototype-demo")
    assert exc.value.code == code


@pytest.fixture
def site(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    try:
        yield app
    finally:
        app.state.service.stop_worker()
        app.state.service.engine.dispose()


def import_project(service, owner, pid, raw, key, *, expect_missing=True):
    upload = service.create_upload(
        owner,
        pid,
        {
            "filename": key + ".vksbundle.zip",
            "size_bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
            "media_type": "application/zip",
        },
    )
    service.put_upload(owner, pid, upload["upload_session_id"], raw)
    payload = {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]}
    preview = service.preflight(owner, pid, payload)
    assert preview["valid"]
    if expect_missing:
        assert any(row["code"] == "VKS_PROTOTYPE_SURFACE_MISSING" for row in preview["issues"])
    job = service.import_bundle(owner, pid, payload, key)
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded", result
    return result["result"]["analysis_set_id"]


def original(raw, state="list"):
    with ZipFile(BytesIO(raw)) as archive:
        return archive.read(f"views/{state}.png")


def test_images_use_exact_selected_revision_and_current_source_permissions(site):
    service = site.state.service
    with TestClient(site) as web:
        login = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
        assert login.is_success
        _, owner = service.login(service.bootstrap_token)
        pid = service.create_project(owner, {"key": "prototype-demo", "name": "Prototype"}, "prototype")["id"]
        raw = build_bundle()
        draft = import_project(service, owner, pid, raw, "v1")
        workspace = service.workspace(owner, pid, draft)
        first = workspace["pages"][0]
        assert {r["validity_status"] for r in workspace["relations"]} == {"valid"}
        assert len(workspace["relations"]) == 3
        assert all(r["causal_basis"] == "not_established" for r in workspace["relations"])
        released = service.save_snapshot(owner, pid, {"analysis_set_id": draft}, "v1-snapshot")
        reader = service.create_token(
            owner, pid, {"name": "reader", "scopes": ["knowledge:read"], "expires_in_days": 1}
        )
        headers = {"Authorization": "Bearer " + reader["token"]}
        base = f"/api/v3/projects/{pid}/prototype/pages/{PAGE_KEY}/states"
        export = service.export(owner, pid, {"release_id": released["id"], "format": "vksbundle"}, "v1-export")
        service.process_job(export["id"])
        export_status = service.get_job(owner, export["id"])
        assert export_status["state"] == "succeeded", export_status
        exported, _ = service.download_export(owner, pid, export["id"])
        parsed = parse_bundle(exported, "prototype-demo")
        assert any(row["stable_key"] == PAGE_KEY for row in parsed.entities)
        assert {row["content"] for row in parsed.artifacts if row["kind"] == "screenshot"} == {
            original(raw, state) for state in ("list", "create-drawer", "edit-drawer")
        }
        query = {"release_id": released["id"], "page_revision_id": first["entity_revision_id"]}
        read = web.get(base + "/list/image", params=query, headers=headers)
        assert read.status_code == 200
        assert read.headers["content-type"] == "image/png"
        assert read.headers["cache-control"] == "no-store"
        assert read.content == original(raw)
        assert web.get(base + "/sync/image", params=query, headers=headers).status_code == 404
        assert (
            web.get(base + "/list/image", params={"page_revision_id": first["entity_revision_id"]}).status_code == 422
        )
        assert web.get(base + "/list/image", params={**query, "analysis_set_id": draft}).status_code == 422
        raw2 = build_bundle(baseline="prototype-v2", accent="#592764", restricted=True)
        draft2 = import_project(service, owner, pid, raw2, "v2")
        second = service.workspace(owner, pid, draft2)["pages"][0]
        assert first["id"] == second["id"]
        assert first["entity_revision_id"] != second["entity_revision_id"]
        draft_params = {"analysis_set_id": draft2, "page_revision_id": second["entity_revision_id"]}
        assert web.get(base + "/list/image", params=draft_params).content == original(raw2)
        assert web.get(base + "/list/image", params=draft_params, headers=headers).status_code == 403
        assert (
            web.get(
                base + "/list/image", params={**draft_params, "page_revision_id": first["entity_revision_id"]}
            ).status_code
            == 409
        )
        second_release = service.save_snapshot(owner, pid, {"analysis_set_id": draft2}, "v2-snapshot")
        second_params = {"release_id": second_release["id"], "page_revision_id": second["entity_revision_id"]}
        assert web.get(base + "/list/image", params=second_params, headers=headers).status_code == 404
        visible = web.get(
            f"/api/v1/projects/{pid}/workspace", params={"release_id": second_release["id"]}, headers=headers
        )
        assert visible.status_code == 200
        assert not visible.json()["data"]["pages"] and not visible.json()["data"]["actions"]
        assert web.get(base + "/list/image", params=query, headers=headers).content == original(raw)

        # Restrict a second surface after snapshot creation. The old page and
        # all of its actions now inherit that exact source's current policy.
        with service.db() as db:
            sidecar = db.get(Artifact, first["artifact_id"])
            art = db.scalar(
                select(Artifact).where(
                    Artifact.snapshot_id == sidecar.snapshot_id, Artifact.relative_path == "views/edit-drawer.png"
                )
            )
            art.sensitivity = "confidential"
        assert web.get(base + "/list/image", params=query, headers=headers).status_code == 404
        assert web.get(base + "/list/image", params=query).content == original(raw)


def test_v3_image_openapi_contract(site):
    path = "/api/v3/projects/{pid}/prototype/pages/{page_key}/states/{state_key}/image"
    endpoint = site.openapi()["paths"][path]["get"]
    assert endpoint["responses"]["200"]["content"]["image/png"]["schema"]["format"] == "binary"
    assert next(row for row in endpoint["parameters"] if row["name"] == "page_revision_id")["required"]
