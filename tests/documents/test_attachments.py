from hashlib import sha256
from io import BytesIO
import json
import zipfile
import pytest
from PIL import Image
from vks_api.service import Service, DomainError
from vks_api.attachments import resolve
from packages.analyzers.vks_ingest import source_digest


def test_document_image_resolves_fixed_snapshot_and_blocks_external_cross_snapshot_source(tmp_path):
    service = Service(tmp_path)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "附件测试"}, "project")
    with zipfile.ZipFile("fixtures/charge-demo.vksbundle.zip") as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    image = BytesIO()
    Image.new("RGB", (32, 32), "#eef4ff").save(image, format="PNG")
    files["docs/images/fixture.png"] = image.getvalue()
    manifest = json.loads(files["manifest.json"])
    manifest["artifacts"].append(
        {
            "source_id": "docs",
            "kind": "screenshot",
            "path": "docs/images/fixture.png",
            "media_type": "image/png",
            "size_bytes": len(image.getvalue()),
            "sha256": sha256(image.getvalue()).hexdigest(),
            "sensitivity": "public",
        }
    )
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [a for a in manifest["artifacts"] if a["source_id"] == source["source_id"]]
        )
    files["manifest.json"] = json.dumps(manifest).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for path, raw in files.items():
            z.writestr(path, raw)
    with service.db() as s:
        result = service._commit_bundle(s, project["id"], actor.id, service._parse(output.getvalue(), project["key"]))
    w = service.workspace(actor, project["id"])
    doc = w["documents"][0]
    raw, media, disposition, name = resolve(service, actor, doc["id"], doc["entity_revision_id"], "images/fixture.png")
    assert raw == image.getvalue() and media == "image/png" and disposition == "inline"
    edited = service.save_document(
        actor,
        doc["id"],
        {
            "analysis_set_id": result["analysis_set_id"],
            "base_revision_id": doc["entity_revision_id"],
            "markdown": doc["content"] + "\n新增说明",
            "locked_section_ids": [],
        },
        doc["row_version"],
    )
    assert resolve(service, actor, doc["id"], edited["entity_revision_id"], "images/fixture.png")[0] == raw
    for path in (
        "https://example.invalid/image.png",
        "//example.invalid/x",
        "../../.vks/bootstrap-token",
        "images/fixture.png?remote=1",
        "file:///etc/passwd",
        "images\\fixture.png",
    ):
        with pytest.raises(DomainError):
            resolve(service, actor, doc["id"], edited["entity_revision_id"], path)
    # A document path is never permission to access another source snapshot.
    with pytest.raises(DomainError) as e:
        resolve(service, actor, doc["id"], edited["entity_revision_id"], "../backend/InvoiceService.java")
    assert e.value.status == 404
