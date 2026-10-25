from hashlib import sha256
from io import BytesIO
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from packages.collector.snapshots import Snapshot, bundle_bytes
from packages.analyzers.vks_ingest import parse_bundle, BundleError
from vks_api.app import create_app
from vks_api.models import Artifact
from sqlalchemy import select


def test_independent_bad_files_keep_valid_documents_and_restrict_raw_bytes(tmp_path):
    raw = bundle_bytes(
        Snapshot(
            "local",
            "fixed",
            {"docs/good.md": b"# Good\nVerified original\n", "docs/bad.md": b"\x80\x81", "api/bad.yaml": b"openapi: ["},
        ),
        "partial",
        "baseline",
        role="documentation",
    )
    with zipfile.ZipFile(BytesIO(raw)) as z:
        initial = {i.filename: z.read(i) for i in z.infolist()}
    manifest = json.loads(initial["manifest.json"])
    next(a for a in manifest["artifacts"] if a["path"] == "api/bad.yaml")["kind"] = "openapi"
    initial["manifest.json"] = json.dumps(manifest).encode()
    encoded = BytesIO()
    with zipfile.ZipFile(encoded, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in initial.items():
            z.writestr(name, data)
    raw = encoded.getvalue()
    parsed = parse_bundle(raw, "partial")
    assert any(e["type"] == "document" and e["display_name"] == "Good" for e in parsed.entities)
    assert len(parsed.issues) == 2 and all(
        a["sensitivity"] == "confidential" for a in parsed.artifacts if a["path"] != "docs/good.md"
    )
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    _, owner = s.login(s.bootstrap_token)
    p = s.create_project(owner, {"key": "partial", "name": "部分成功"}, "p")
    upload = s.create_upload(
        owner,
        p["id"],
        {
            "filename": "partial.zip",
            "size_bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
            "media_type": "application/zip",
        },
    )
    s.put_upload(owner, p["id"], upload["upload_session_id"], raw)
    job = s.import_bundle(owner, p["id"], {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]}, "import")
    s.process_job(job["id"])
    result = s.get_job(owner, job["id"])
    assert result["state"] == "succeeded" and result["completeness"] == "partial"
    assert len(s.workspace(owner, p["id"])["documents"]) == 1
    with s.db() as db:
        failed = list(db.scalars(select(Artifact).where(Artifact.relative_path == "docs/bad.md")))
        assert failed[0].sensitivity == "confidential" and failed[0].metadata_json["parse_status"] == "failed"
        assert (s.data_dir / "objects" / failed[0].object_key).read_bytes() == b"\x80\x81"
    # Transport integrity never becomes a partial warning.
    with zipfile.ZipFile(BytesIO(raw)) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    files["docs/good.md"] = b"tampered"
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    with pytest.raises(BundleError) as e:
        parse_bundle(out.getvalue(), "partial")
    assert e.value.code == "VKS_HASH_MISMATCH"


def test_http_upload_allows_at35_transport_and_rejects_over_limit(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    with TestClient(app) as c:
        session = c.post("/api/v1/auth/session", json={"bootstrap_token": s.bootstrap_token}).json()["data"]
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "p"}
        p = c.post("/api/v1/projects", headers=headers, json={"key": "upload-large", "name": "100MiB负载"}).json()[
            "data"
        ]
        response = c.post(
            f"/api/v1/projects/{p['id']}/upload-sessions",
            headers=headers,
            json={
                "filename": "at35.zip",
                "size_bytes": 60 * 1024 * 1024,
                "sha256": "0" * 64,
                "media_type": "application/zip",
            },
        )
        assert response.status_code == 201, response.text
        denied = c.post(
            f"/api/v1/projects/{p['id']}/upload-sessions",
            headers=headers,
            json={
                "filename": "too-large.zip",
                "size_bytes": 128 * 1024 * 1024 + 1,
                "sha256": "0" * 64,
                "media_type": "application/zip",
            },
        )
        assert denied.status_code == 422
        oversized = c.put(
            "/api/v1/not-an-upload", headers={**headers, "Content-Length": str(128 * 1024 * 1024 + 1)}, content=b""
        )
        assert oversized.status_code == 413


def test_import_keeps_extractor_provenance_and_reimport_only_tightens_consent(tmp_path):
    from vks_api.models import Evidence

    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1"},
        "paths": {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}},
    }
    raw = bundle_bytes(
        Snapshot(
            "local",
            "fixed",
            {
                "docs/rules.md": b"# Rules\nGET /items is declared.\n",
                "api/spec.json": json.dumps(spec).encode(),
            },
        ),
        "provenance",
        "v1",
        role="documentation",
    )
    with zipfile.ZipFile(BytesIO(raw)) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    app = create_app(tmp_path, enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "provenance", "name": "来源版本"}, "p")
    for index, allowed in enumerate((True, False, True)):
        manifest = json.loads(files["manifest.json"])
        manifest.update(allow_platform_llm=allowed, import_key=f"consent-{index}", baseline_key=f"v{index}")
        files["manifest.json"] = json.dumps(manifest).encode()
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        data = stream.getvalue()
        upload = service.create_upload(
            owner,
            project["id"],
            {
                "filename": "fixed.zip",
                "size_bytes": len(data),
                "sha256": sha256(data).hexdigest(),
                "media_type": "application/zip",
            },
        )
        service.put_upload(owner, project["id"], upload["upload_session_id"], data)
        job = service.import_bundle(
            owner,
            project["id"],
            {
                "kind": "bundle",
                "artifact_ids": [upload["artifact_id"]],
            },
            f"i-{index}",
        )
        service.process_job(job["id"])
        assert service.get_job(owner, job["id"])["state"] == "succeeded", json.dumps(
            service.get_job(owner, job["id"]), ensure_ascii=False
        )
        with service.db() as db:
            artifact = db.scalar(select(Artifact).where(Artifact.relative_path == "docs/rules.md"))
            assert artifact.metadata_json["allow_platform_llm"] is (index == 0)
            evidence = db.scalar(select(Evidence).where(Evidence.stable_key.like("md.api.%")))
            dto = service.evidence_dto(evidence)
            assert dto["extractor_version"] == "vks-markdown-api-reference-1"
            assert not any(key.startswith("_") for key in dto["locator"])
