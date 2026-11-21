"""Fast independent lists, exact import deduplication and reversible library deletion."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import event, func, select
from vks_api import material_parser
from vks_api.materials import MaterialTrash, PrepareMaterial, RestoreMaterialVersion, TrashMaterial
from vks_api.models import Artifact, Revision, uid
from vks_api.service import DomainError

from tests.interactive import test_materials
from tests.prototype.test_v3_workflow import _upload

materials = test_materials.materials


def form(aid, kind, target, deleted=True):
    return TrashMaterial(analysis_set_id=aid, kind=kind, target_id=target, deleted=deleted)


def version(service, owner, pid, aid):
    return test_materials.selected(service, owner, pid, aid)["selection_version"]


def test_package_list_is_independent_of_historical_snapshot_validation_and_write_lock(materials, monkeypatch):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Rules\n\nExact rule")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    manager.checkpoint(owner, pid, aid, "Version", uid())
    monkeypatch.setattr(service, "_saved_snapshot", lambda *a: pytest.fail("Package list must not wait for snapshots"))
    monkeypatch.setattr(service, "_revisions", lambda *a: pytest.fail("Package list must not load every body"))
    with service.db(), ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(manager.library, owner, pid, aid, False).result(timeout=3)
    assert len(result["packages"]) == 1 and not result["versions"]


def test_history_batches_exact_facts_but_rechecks_current_permissions(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    md = "# Guide\n\n" + "\n\n".join(f"## Rule {i}\n\nExact {i}" for i in range(140))
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", md)
    test_materials.activate(service, owner, manager, pid, aid, [package])
    for i in range(4):
        manager.checkpoint(owner, pid, aid, f"Snapshot {i}", uid())
    statements = []

    def count(*args):
        statements.append(args[2])

    event.listen(service.engine, "before_cursor_execute", count)
    try:
        history = manager.history(owner, pid, aid)
    finally:
        event.remove(service.engine, "before_cursor_execute", count)
    assert len(history["versions"]) == 4
    assert len(statements) < 90, len(statements)
    token = service.create_token(owner, pid, {"name": "Limited", "scopes": ["knowledge:read", "knowledge:edit"]})
    limited = service.authenticate(token["token"], "token")
    assert len(manager.history(limited, pid, aid)["versions"]) == 4
    with service.db() as db:
        root = db.get(Revision, package["revision_id"])
        member = db.get(Revision, root.metadata_json["material_package"]["members"][0])
        db.get(Artifact, member.artifact_id).sensitivity = "confidential"
    assert not manager.history(limited, pid, aid)["versions"]
    assert len(manager.history(owner, pid, aid)["versions"]) == 4


def test_recycle_packages_versions_and_restore_exact_composition(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Rules\n\nExact original")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    saved = manager.checkpoint(owner, pid, aid, "Original", uid())
    original = deepcopy(saved["manifest"])
    with pytest.raises(DomainError, match="请先停用"):
        manager.trash(
            owner, pid, form(aid, "package", package["package_id"]), str(version(service, owner, pid, aid)), uid()
        )
    test_materials.activate(service, owner, manager, pid, aid, remove=[package["package_id"]])
    expected = str(version(service, owner, pid, aid))
    key = uid()
    deleted = manager.trash(owner, pid, form(aid, "package", package["package_id"]), expected, key)
    assert deleted == manager.trash(owner, pid, form(aid, "package", package["package_id"]), expected, key)
    library = manager.library(owner, pid, aid)
    assert not library["packages"] and library["trash"][0]["id"] == package["package_id"]
    with pytest.raises(DomainError, match="回收站"):
        test_materials.activate(service, owner, manager, pid, aid, [package])
    manager.trash(owner, pid, form(aid, "version", saved["id"]), expected, uid())
    assert saved["id"] not in {v["id"] for v in manager.history(owner, pid, aid)["versions"]}
    with pytest.raises(DomainError):
        manager.restore(
            owner, pid, RestoreMaterialVersion(analysis_set_id=aid, release_id=saved["id"]), expected, uid()
        )
    manager.trash(owner, pid, form(aid, "version", saved["id"], False), expected, uid())
    restored = manager.restore(
        owner, pid, RestoreMaterialVersion(analysis_set_id=aid, release_id=saved["id"]), expected, uid()
    )
    assert (
        manager.library(owner, pid, restored["analysis_set_id"])["packages"][0]["active_revision_id"]
        == package["revision_id"]
    )
    with service.db() as db:
        assert db.get(MaterialTrash, (pid, "package", package["package_id"])) is None
        from vks_api.models import Release

        assert db.get(Release, saved["id"]).manifest_json == original


def test_trash_rejects_foreign_objects_readonly_credentials_and_stale_selection(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    other_pid, other_aid = projects[1]
    package = test_materials.stage(service, owner, manager, other_pid, other_aid, "documents", "# Private")
    with pytest.raises(DomainError):
        manager.trash(
            owner, pid, form(aid, "package", package["package_id"]), str(version(service, owner, pid, aid)), uid()
        )
    local = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Local")
    token = service.create_token(owner, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    limited = service.authenticate(token["token"], "token")
    with pytest.raises(DomainError):
        manager.trash(
            limited, pid, form(aid, "package", local["package_id"]), str(version(service, owner, pid, aid)), uid()
        )
    with pytest.raises(DomainError) as error:
        manager.trash(owner, pid, form(aid, "package", local["package_id"]), "0", uid())
    assert error.value.code == "VKS_VERSION_CONFLICT"


def test_same_inputs_are_reused_or_rejected_before_reparsing(materials, monkeypatch):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = "# Guide\n\n## Exact rule\n\nUnchanged."
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="guide.md")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with service.db() as db:
        before = db.scalar(select(func.count()).select_from(Revision))
    monkeypatch.setattr(material_parser, "parse", lambda *a: pytest.fail("Identical content must skip parsing"))
    unchanged = test_materials.stage(
        service, owner, manager, pid, aid, "documents", raw, package_id=package["package_id"], filename="renamed.md"
    )
    assert unchanged["unchanged"] and unchanged["revision_id"] == package["revision_id"]
    with pytest.raises(DomainError) as error:
        test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="copy.md")
    assert error.value.code == "VKS_DUPLICATE_MATERIAL"
    with service.db() as db:
        assert db.scalar(select(func.count()).select_from(Revision)) == before


def test_concurrent_new_imports_cannot_create_duplicate_packages(materials, monkeypatch):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = b"# Concurrent\n\nExact shared bytes."
    uploads = [_upload(service, owner, pid, raw, "same.md") for _ in range(2)]
    expected = str(version(service, owner, pid, aid))
    parse, barrier = material_parser.parse, Barrier(2)

    def together(*args):
        parsed = parse(*args)
        barrier.wait(timeout=4)
        return parsed

    monkeypatch.setattr(material_parser, "parse", together)

    def prepare(upload):
        try:
            return manager.prepare(
                owner,
                pid,
                PrepareMaterial(analysis_set_id=aid, kind="documents", name="Concurrent", artifact_ids=[upload]),
                expected,
                uid(),
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, uploads))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "VKS_DUPLICATE_MATERIAL" in results
    assert len(manager.library(owner, pid, aid, False)["packages"]) == 1


def test_adopted_document_paths_preserve_all_logical_identities_on_update(materials):
    from io import BytesIO
    from zipfile import ZipFile

    from vks_api.materials import ROOT
    from vks_api.models import Entity, Selection
    from vks_api.prototype_edit import _select

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]

    def archive():
        output = BytesIO()
        with ZipFile(output, "w") as zipfile:
            zipfile.writestr("handbook/one.md", "# First\n\nOriginal first.")
            zipfile.writestr("handbook/two.md", "# Second\n\nOriginal second.")
        return output.getvalue()

    package = test_materials.stage(service, owner, manager, pid, aid, "documents", archive(), filename="guide.zip")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with service.db() as db:
        from vks_api.models import Analysis

        analysis = db.get(Analysis, aid)
        original = db.get(Revision, package["revision_id"])
        metadata = deepcopy(original.metadata_json)
        docs = {}
        members = []
        for rid in metadata[ROOT]["members"]:
            row = db.get(Revision, rid)
            entity = db.get(Entity, row.entity_id)
            if entity.type == "document":
                art = db.get(Artifact, row.artifact_id)
                art.relative_path = art.relative_path.removeprefix("documents/")
                docmeta = dict(row.metadata_json)
                del docmeta["material_path"]
                row = manager.docs._revision(db, entity, art, row.name, row.content, row.locator, docmeta)
                docs[row.entity_id] = row.id
                _select(db, analysis, row)
            members.append(row.id)
        metadata[ROOT]["members"] = members
        metadata[ROOT].pop("upload_fingerprint")
        metadata[ROOT].pop("single_input_sha256")
        legacy = manager.docs._revision(
            db,
            db.get(Entity, original.entity_id),
            db.get(Artifact, original.artifact_id),
            original.name,
            original.content,
            original.locator,
            metadata,
        )
        _select(db, analysis, legacy)
    updated = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        archive(),
        package_id=package["package_id"],
        filename="guide.zip",
    )
    assert updated["changes"] == {"added": 0, "updated": 0, "reused": 2, "removed": 0}
    test_materials.activate(service, owner, manager, pid, aid, [updated])
    with service.db() as db:
        assert {eid: db.get(Selection, (aid, eid)).entity_revision_id for eid in docs} == docs


def test_updated_package_preserves_captured_manual_edits_when_source_body_is_unchanged(materials):
    from io import BytesIO
    from zipfile import ZipFile

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]

    def archive(second):
        output = BytesIO()
        with ZipFile(output, "w") as zipfile:
            zipfile.writestr("one.md", "# First\n\nOriginal source.")
            zipfile.writestr("two.md", "# Second\n\n" + second)
        return output.getvalue()

    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", archive("Before"), filename="guide.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    doc = next(d for d in test_materials.selected(service, owner, pid, aid)["documents"] if d["name"] == "First")
    service.save_document(
        owner,
        doc["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": doc["revision_id"],
            "markdown": "# First\n\nKeep my manual text.",
            "locked_section_ids": [],
        },
        "1",
    )
    test_materials.activate(service, owner, manager, pid, aid, remove=[package["package_id"]])
    captured = next(g for g in manager.library(owner, pid, aid)["packages"] if g["id"] == package["package_id"])[
        "versions"
    ][0]
    test_materials.activate(service, owner, manager, pid, aid, [captured])
    changed = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        archive("After"),
        package_id=package["package_id"],
        filename="guide.zip",
    )
    assert changed["changes"]["reused"] == 1 and changed["changes"]["updated"] == 1
    test_materials.activate(service, owner, manager, pid, aid, [changed])
    current = next(d for d in test_materials.selected(service, owner, pid, aid)["documents"] if d["id"] == doc["id"])
    assert "Keep my manual text." in service.get_document(owner, doc["id"], current["revision_id"])["markdown"]


def test_new_version_with_overlapping_document_paths_requires_update(materials):
    from io import BytesIO
    from zipfile import ZipFile

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]

    def archive(count, body):
        output = BytesIO()
        with ZipFile(output, "w") as zipfile:
            for i in range(count):
                zipfile.writestr(f"handbook/{i}.md", f"# Article {i}\n\n{body}")
        return output.getvalue()

    package = test_materials.stage(service, owner, manager, pid, aid, "documents", archive(6, "Old"), filename="v1.zip")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with pytest.raises(DomainError) as error:
        test_materials.stage(service, owner, manager, pid, aid, "documents", archive(8, "New"), filename="v2.zip")
    assert error.value.code == "VKS_MATERIAL_UPDATE_REQUIRED"
    assert error.value.details["package_id"] == package["package_id"]
    assert len(manager.library(owner, pid, aid, False)["packages"]) == 1
    changed = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        archive(8, "New"),
        package_id=package["package_id"],
        filename="v2.zip",
    )
    assert changed["changes"]["updated"] == 6 and changed["changes"]["added"] == 2


def test_http_material_reads_and_authentication_do_not_wait_for_mutation_lock(materials):
    web, service, _owner, _manager, projects = materials
    pid, aid = projects[0]
    assert web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).status_code == 200
    with service.db(), ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(
            web.get, f"/api/v3/projects/{pid}/materials?analysis_set_id={aid}&include_versions=false"
        ).result(timeout=3)
    assert response.status_code == 200
    assert response.json()["data"]["analysis_set_id"] == aid


def test_retry_migration_reuses_the_active_job_and_projects_current_scope(materials):
    from vks_api.models import Job

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Guide\n\nExact text.")
    result = test_materials.activate(service, owner, manager, pid, aid, [package])
    first = manager.retry_migration(owner, pid, aid, str(result["selection_version"]), uid())
    assert result["job_id"] is None
    second = manager.retry_migration(owner, pid, aid, str(result["selection_version"]), uid())
    assert second["id"] == first["id"]
    # Knowledge saves made by this same running migration may advance the
    # selection version. A retry still reuses it instead of creating work.
    again = manager.retry_migration(owner, pid, aid, "0", uid())
    assert again["id"] == first["id"]
    with service.db() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Job).where(Job.project_id == pid, Job.kind == "material_migrate")
            )
            == 1
        )
    current = manager.library(owner, pid, aid, False)
    assert current["jobs"][0]["current_scope"] is True
    test_materials.activate(service, owner, manager, pid, aid, remove=[package["package_id"]])
    after = manager.library(owner, pid, aid, False)
    assert next(j for j in after["jobs"] if j["id"] == first["id"])["current_scope"] is False


def test_purge_recycle_entries_preserves_facts_and_allows_reimport(materials):
    from vks_api.materials import PurgeMaterials
    from vks_api.models import Release

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = "# Purge sample\n\nExact immutable content."
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", raw)
    test_materials.activate(service, owner, manager, pid, aid, [package])
    saved = manager.checkpoint(owner, pid, aid, "Keep original facts", uid())
    manifest = deepcopy(saved["manifest"])
    test_materials.activate(service, owner, manager, pid, aid, remove=[package["package_id"]])
    expected = str(version(service, owner, pid, aid))
    targets = [{"kind": "package", "target_id": package["package_id"]}, {"kind": "version", "target_id": saved["id"]}]
    for item in targets:
        manager.trash(owner, pid, form(aid, item["kind"], item["target_id"]), expected, uid())
    with service.db() as db:
        facts = {table: db.scalar(select(func.count()).select_from(table)) for table in (Artifact, Revision, Release)}
    request = PurgeMaterials(analysis_set_id=aid, targets=targets)
    key = uid()
    assert manager.purge_trash(owner, pid, request, expected, key) == {"purged": 2}
    assert manager.purge_trash(owner, pid, request, expected, key) == {"purged": 2}
    assert not manager.library(owner, pid, aid)["trash"]
    assert not manager.library(owner, pid, aid)["packages"]
    assert saved["id"] not in {r["id"] for r in manager.history(owner, pid, aid)["versions"]}
    with pytest.raises(DomainError, match="无法"):
        manager.trash(owner, pid, form(aid, "package", package["package_id"], False), expected, uid())
    with service.db() as db:
        assert all(db.scalar(select(func.count()).select_from(table)) == count for table, count in facts.items())
        assert db.get(Release, saved["id"]).manifest_json == manifest
    imported = test_materials.stage(service, owner, manager, pid, aid, "documents", raw)
    assert imported["package_id"] != package["package_id"]


def test_purge_is_atomic_and_requires_recycle_membership_and_permissions(materials):
    from vks_api.materials import PurgeMaterials

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    a = test_materials.stage(service, owner, manager, pid, aid, "documents", "# One")
    b = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Two")
    expected = str(version(service, owner, pid, aid))
    manager.trash(owner, pid, form(aid, "package", a["package_id"]), expected, uid())
    request = PurgeMaterials(
        analysis_set_id=aid, targets=[{"kind": "package", "target_id": p["package_id"]} for p in (a, b)]
    )
    with pytest.raises(DomainError):
        manager.purge_trash(owner, pid, request, expected, uid())
    assert manager.library(owner, pid, aid)["trash"][0]["id"] == a["package_id"]
    token = service.create_token(owner, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    reader = service.authenticate(token["token"], "token")
    with pytest.raises(DomainError):
        manager.purge_trash(reader, pid, request, expected, uid())
    with pytest.raises(DomainError) as error:
        manager.purge_trash(owner, pid, request, "0", uid())
    assert error.value.code == "VKS_VERSION_CONFLICT"


def test_purge_checks_current_workspace_even_when_request_uses_another_draft(materials):
    from vks_api.materials import PurgeMaterials, StudioWorkspace

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Current workspace")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    saved = manager.checkpoint(owner, pid, aid, "Read-only original", uid())
    frozen = saved["analysis_set_id"]
    with pytest.raises(DomainError, match="此版本已保存"):
        manager.purge_trash(
            owner,
            pid,
            PurgeMaterials(analysis_set_id=frozen, targets=[{"kind": "package", "target_id": package["package_id"]}]),
            str(version(service, owner, pid, frozen)),
            uid(),
        )
    other = service.create_analysis(owner, pid, {"name": "Other editable draft"}, uid())
    with service.db() as db:
        assert db.get(StudioWorkspace, pid).analysis_set_id == aid
        # A retained recycle record must never allow another draft to hide a
        # package now used by the project's current workspace.
        db.add(MaterialTrash(project_id=pid, kind="package", target_id=package["package_id"], deleted_by=owner.id))
    request = PurgeMaterials(
        analysis_set_id=other["id"], targets=[{"kind": "package", "target_id": package["package_id"]}]
    )
    with pytest.raises(DomainError, match="正在使用"):
        manager.purge_trash(owner, pid, request, str(other["selection_version"]), uid())
    with service.db() as db:
        assert db.get(MaterialTrash, (pid, "purged_package", package["package_id"])) is None
        assert db.get(StudioWorkspace, pid).analysis_set_id == aid
    assert manager.library(owner, pid, aid)["packages"][0]["active_revision_id"] == package["revision_id"]


def test_full_access_version_directory_reads_metadata_without_manifests(materials, monkeypatch):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# Catalog\n\n" + "\n\n".join(f"## Rule {i}\n\nExact fact {i}." for i in range(600)),
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    saved = [manager.checkpoint(owner, pid, aid, f"Large version {i}", uid()) for i in range(6)]
    statements = []

    def capture(_conn, _cursor, sql, *_args):
        statements.append(sql)

    monkeypatch.setattr(
        service,
        "_saved_snapshot",
        lambda *_args: pytest.fail("Metadata directory must defer full snapshot verification"),
    )
    event.listen(service.engine, "before_cursor_execute", capture)
    try:
        history = manager.history(owner, pid, aid)
    finally:
        event.remove(service.engine, "before_cursor_execute", capture)
    assert {v["id"] for v in history["versions"]} == {v["id"] for v in saved}
    assert not any("manifest_json" in sql for sql in statements)
    assert not any("entity_revision.content" in sql for sql in statements)
    assert len(statements) < 15
