"""Ordinary Markdown ZIP assets remain immutable data with current permissions."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from vks_api.material_parser import parse
from vks_api.materials import ROOT, readable
from vks_api.models import Artifact, Revision
from vks_api.service import DomainError

from tests.interactive import test_materials

materials = test_materials.materials


def package(image):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("manual/guide.md", "# 普通手册\n\n![流程图](../assets/flow.png)\n")
        archive.writestr("assets/flow.png", image)
        archive.writestr("assets/source.js", b'throw new Error("SOURCE_MUST_NOT_EXECUTE")')
    return output.getvalue()


def test_markdown_assets_retained_as_non_executed_exact_version_artifacts(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    original = b"original diagram image bytes"
    first = test_materials.stage(
        service, owner, manager, pid, aid, "documents", package(original), filename="manual.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [first])
    with service.db() as db:
        root = db.get(Revision, first["revision_id"])
        attachments = [db.get(Artifact, x) for x in root.metadata_json[ROOT]["attachment_artifact_ids"]]
        assert {x.relative_path for x in attachments} == {"documents/assets/flow.png", "documents/assets/source.js"}
        assert all(x.snapshot_id == db.get(Artifact, root.artifact_id).snapshot_id for x in attachments)
        assert all(x.media_type == "application/octet-stream" for x in attachments)
        image = next(a for a in attachments if a.relative_path.endswith("flow.png"))
        image_id = image.id
        snapshot = image.snapshot_id
        prior_members = root.metadata_json[ROOT]["members"]
    assert service.artifact_bytes(owner, pid, image_id)[0] == original
    second = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        package(b"updated diagram bytes"),
        package_id=first["package_id"],
        filename="manual.zip",
    )
    with service.db() as db:
        root = db.get(Revision, second["revision_id"])
        # Markdown revision identities are retained even though attachments
        # belong to this new package revision's exact source snapshot.
        assert root.metadata_json[ROOT]["members"] == prior_members
        attachments = [db.get(Artifact, x) for x in root.metadata_json[ROOT]["attachment_artifact_ids"]]
        new_image = next(a for a in attachments if a.relative_path.endswith("flow.png"))
        assert new_image.snapshot_id != snapshot and new_image.id != image_id
        new_id = new_image.id
    assert service.artifact_bytes(owner, pid, image_id)[0] == original
    assert service.artifact_bytes(owner, pid, new_id)[0] == b"updated diagram bytes"


def test_package_names_and_warnings_inherit_retained_attachment_restrictions(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    staged = test_materials.stage(
        service, owner, manager, pid, aid, "documents", package(b"image"), filename="manual.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    token = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read", "knowledge:edit"]})
    reader = service.authenticate(token["token"], "token")
    with service.db() as db:
        root = db.get(Revision, staged["revision_id"])
        assert readable(service, db, reader, pid, root)
        asset = db.get(Artifact, root.metadata_json[ROOT]["attachment_artifact_ids"][0])
        asset.metadata_json = {**asset.metadata_json, "requires_source_read": True}
    with service.db() as db:
        assert not readable(service, db, reader, pid, db.get(Revision, staged["revision_id"]))
        assert readable(service, db, owner, pid, db.get(Revision, staged["revision_id"]))
    assert not manager.library(reader, pid, aid)["packages"]


@pytest.mark.parametrize("path", ["../outside.png", "/absolute.png", "nested/../../outside.png", "a\\b.png"])
def test_direct_document_asset_names_use_archive_path_validation(path):
    with pytest.raises(DomainError) as exc:
        parse("documents", [("guide.md", b"# Normal document"), (path, b"inert data")])
    assert exc.value.code == "VKS_INVALID_ARCHIVE"
