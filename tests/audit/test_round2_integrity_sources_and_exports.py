"""Independent design audit: temporary stores and synthetic full snapshots only."""

from copy import deepcopy
from hashlib import sha256
import pytest
from sqlalchemy import select
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Evidence, Release, Revision
from vks_api.service import DomainError, Service
from vks_api.source_sync import SourceSyncService
from vks_api.versions import VersionService
from packages.analyzers.vks_ingest import parse_bundle
from packages.collector.snapshots import Snapshot, bundle_bytes


@pytest.fixture
def isolated(tmp_path):
    service = Service(tmp_path / "store", database_url=f"sqlite:///{tmp_path / 'round2.db'}")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Round two integrity"}, "project")
    yield (service, owner, project["id"])
    service.stop_worker()
    service.engine.dispose()


def upload(service, owner, pid, raw, name="snapshot.zip"):
    value = service.create_upload(
        owner,
        pid,
        {"filename": name, "size_bytes": len(raw), "sha256": sha256(raw).hexdigest(), "media_type": "application/zip"},
    )
    service.put_upload(owner, pid, value["upload_session_id"], raw)
    return value["artifact_id"]


def publish(service, owner, pid, aid, key):
    for relation in service.workspace(owner, pid, aid)["relations"]:
        if relation["excluded"] is False:
            service.set_relation_state(
                owner, relation["id"], {"excluded": "confirm" == "reject"}, relation["row_version"]
            )
    candidate = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, key + "-precheck")
    pass
    ticket = None
    return candidate


def sync(service, owner, pid, raw, key, **extra):
    queued = SourceSyncService(service).create(
        owner,
        pid,
        dict(
            artifact_id=upload(service, owner, pid, raw),
            profile_id="synthetic-authorized-profile",
            consent_sha256="a" * 64,
            snapshot_sha256=sha256(raw).hexdigest(),
            **extra,
        ),
        key,
    )
    service.process_job(queued["job"]["id"])
    result = service.get_job(owner, queued["job"]["id"])
    assert result["state"] == "succeeded", result
    return (queued, result["result"]["analysis_set_id"])


def test_full_source_sync_removes_deleted_code_from_new_draft_and_reports_the_deletion(isolated):
    service, owner, pid = isolated
    keep = b"package demo; class Keep { int value() { return 1; } }"
    removed = b"package demo; class Removed { int retired() { return 2; } }"
    raw1 = bundle_bytes(
        Snapshot("git", "a" * 40, {"Keep.java": keep, "Removed.java": removed}),
        "charge-demo",
        "first",
        source_key="backend",
    )
    _, aid1 = sync(service, owner, pid, raw1, "sync-first")
    previous = service.workspace(owner, pid, aid1)
    deleted = {row["entity_id"] for row in previous["entities"] if "Removed" in row["name"] or "retired" in row["name"]}
    assert deleted
    release = publish(service, owner, pid, aid1, "first")
    with service.db() as db:
        frozen_manifest = deepcopy(db.get(Release, release["id"]).manifest_json)
    draft = KnowledgeService(service).clone(owner, pid, aid1, "Second", "clone")
    raw2 = bundle_bytes(Snapshot("git", "b" * 40, {"Keep.java": keep}), "charge-demo", "second", source_key="backend")
    queued, aid2 = sync(
        service,
        owner,
        pid,
        raw2,
        "sync-second",
        analysis_set_id=draft["id"],
        previous_snapshot_sha256=sha256(raw1).hexdigest(),
    )
    assert queued["delta"]["removed"] == ["Removed.java"]
    assert aid2 == draft["id"]
    current = service.workspace(owner, pid, aid2)
    remaining = deleted & {row["entity_id"] for row in current["entities"]}
    with service.db() as db:
        report = VersionService(service).compare(db, pid, release["id"], aid2)
        assert db.get(Release, release["id"]).manifest_json == frozen_manifest
    assert deleted <= {row["entity_id"] for row in service.workspace(owner, pid, release_id=release["id"])["entities"]}
    classified_removed = {row["entity_id"] for row in report["changes"] if row["change_kind"] == "removed"}
    assert not remaining and deleted <= classified_removed, {
        "deleted_entity_ids": sorted(deleted),
        "still_selected": sorted(remaining),
        "removed_in_report": sorted(classified_removed),
        "source_vectors": report["source_vectors"],
    }


def test_missing_file_storm_does_not_delete_selections_or_advance_source_cursor(isolated):
    service, owner, pid = isolated
    files = {f"Note{i}.md": f"# Note {i}\nFixed original material\n".encode() for i in range(12)}
    raw1 = bundle_bytes(Snapshot("git", "c" * 40, files), "charge-demo", "storm-first", source_key="backend")
    _, aid = sync(service, owner, pid, raw1, "storm-first")
    before = service.workspace(owner, pid, aid)
    raw2 = bundle_bytes(
        Snapshot("git", "d" * 40, {"Note0.md": files["Note0.md"]}), "charge-demo", "storm-second", source_key="backend"
    )
    with pytest.raises(DomainError) as exc:
        sync(
            service,
            owner,
            pid,
            raw2,
            "storm-second",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(raw1).hexdigest(),
        )
    assert exc.value.code == "VKS_SYNC_SCOPE_ANOMALY"
    after = service.workspace(owner, pid, aid)
    assert after["selected_analysis_set"] == before["selected_analysis_set"]
    assert {row["entity_revision_id"] for row in after["entities"]} == {
        row["entity_revision_id"] for row in before["entities"]
    }


def test_export_reimports_edited_document_without_reintroducing_unselected_history(isolated):
    service, owner, pid = isolated
    job = service.demo_import(owner, pid, "demo")
    service.process_job(job["id"])
    workspace = service.workspace(owner, pid)
    aid = workspace["selected_analysis_set"]["id"]
    document = workspace["documents"][0]
    docs = DocumentService(service)
    old = docs.read_revision(owner, document["entity_id"], document["entity_revision_id"])
    saved = docs.save_draft(
        owner,
        document["entity_id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": old["entity_revision_id"],
            "markdown": old["markdown"].replace("申请金额必须大于零", "申请金额至少为一元"),
            "locked_section_ids": [],
        },
        old["row_version"],
    )
    assert saved["markdown"] != old["markdown"]
    workflow_payload = {
        "analysis_set_id": aid,
        "revision_id": saved["entity_revision_id"],
        "reason": "Inspect edited synthetic document",
    }
    for relation in service.workspace(owner, pid, aid)["relations"]:
        if relation["validity_status"] == "stale":
            with service.db() as db:
                artifacts = {
                    db.get(Revision, relation[field]).artifact_id for field in ("from_revision_id", "to_revision_id")
                }
                extra = list(
                    db.scalars(
                        select(Evidence.id).where(Evidence.analysis_set_id == aid, Evidence.artifact_id.in_(artifacts))
                    )
                )
            VersionService(service).revalidate(
                owner,
                relation["id"],
                {
                    "analysis_set_id": aid,
                    "evidence_ids": list(dict.fromkeys([*relation["evidence_ids"], *extra])),
                    "reason": "Inspect exact current endpoints",
                },
                relation["row_version"],
            )
    release = publish(service, owner, pid, aid, "edited")
    export = service.export(
        owner, pid, {"release_id": release["id"], "format": "vksbundle", "include_source": True}, "export"
    )
    service.process_job(export["id"])
    result = service.get_job(owner, export["id"])
    assert result["state"] == "succeeded", result
    raw, _ = service.download_export(owner, pid, export["id"])
    bundle = parse_bundle(raw, "charge-demo")
    documents = [row for row in bundle.entities if row["type"] == "document"]
    assert len(documents) == 1 and documents[0]["content"] == saved["markdown"], [
        {"stable_key": row["stable_key"], "content": row["content"]} for row in documents
    ]
