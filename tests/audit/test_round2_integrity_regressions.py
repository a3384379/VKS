"""Behavior regressions for complete sync selection and frozen bundle identity.

VKS_ROUND2_POSTGRES_URL is an opt-in isolated PostgreSQL management connection.
Each case creates and drops only its own randomly named database.
"""

import json
import os
import zipfile
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from vks_api.documents import DocumentService
from vks_api.journeys import JourneyService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Analysis, Artifact, Evidence, Job, Relation, Release, Revision
from vks_api.reading import artifact_policy
from vks_api.service import DomainError, Service
from vks_api.source_sync import SourceSyncService
from vks_api.versions import VersionService
from packages.analyzers.vks_ingest import parse_bundle, source_digest
from packages.collector.snapshots import Snapshot, bundle_bytes


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    management = os.environ.get("VKS_ROUND2_POSTGRES_URL")
    admin = None
    if management:
        monkeypatch.setenv("VKS_AUTO_MIGRATE", "1")
        database = "vks_r2_" + uuid4().hex
        admin = create_engine(management, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        database_url = make_url(management).set(database=database).render_as_string(hide_password=False)
    else:
        database_url = f"sqlite:///{tmp_path / 'regression.db'}"
    service = None
    try:
        service = Service(tmp_path / "store", database_url=database_url)
        _, owner = service.login(service.bootstrap_token)
        project = service.create_project(owner, {"key": "charge-demo", "name": "Integrity regressions"}, "project")
        yield (service, owner, project["id"])
    finally:
        if service:
            service.stop_worker()
            service.engine.dispose()
        if admin:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            admin.dispose()


def snapshot(files, revision, baseline, *, source="backend", role="backend", provider="git"):
    return bundle_bytes(Snapshot(provider, revision * 40, files), "charge-demo", baseline, source_key=source, role=role)


def enqueue(service, owner, pid, raw, key, **extra):
    profile_id = extra.pop("profile_id", "round-two-profile")
    upload = service.create_upload(
        owner,
        pid,
        {
            "filename": "full.zip",
            "size_bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
            "media_type": "application/zip",
        },
    )
    service.put_upload(owner, pid, upload["upload_session_id"], raw)
    return SourceSyncService(service).create(
        owner,
        pid,
        dict(
            artifact_id=upload["artifact_id"],
            profile_id=profile_id,
            consent_sha256="a" * 64,
            snapshot_sha256=sha256(raw).hexdigest(),
            **extra,
        ),
        key,
    )


def synced(service, owner, pid, raw, key, **extra):
    queued = enqueue(service, owner, pid, raw, key, **extra)
    service.process_job(queued["job"]["id"])
    job = service.get_job(owner, queued["job"]["id"])
    assert job["state"] == "succeeded", job
    return job["result"]["analysis_set_id"]


def publish(service, owner, pid, aid, key):
    for relation in service.workspace(owner, pid, aid)["relations"]:
        if relation["excluded"] is False:
            service.set_relation_state(
                owner, relation["id"], {"excluded": "confirm" == "reject"}, relation["row_version"]
            )
    candidate = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, key + "-check")
    pass
    ticket = None
    return candidate


def test_method_deletion_breaks_retained_relations_preserves_history_and_other_source(isolated):
    service, owner, pid = isolated
    before = snapshot(
        {"Keep.java": b"package demo; class Keep { int keep(){return 1;} int removed(){return 2;} }"}, "a", "first"
    )
    aid = synced(service, owner, pid, before, "first")
    other = snapshot(
        {"Other.java": b"package other; class Other { int untouched(){return 3;} }"}, "c", "other", source="another"
    )
    with service.db() as db:
        service._commit_bundle(db, pid, owner.id, parse_bundle(other, "charge-demo"), aid)
    old = service.workspace(owner, pid, aid)
    removed = next((row for row in old["entities"] if "removed(" in row["name"]))
    others = {
        row["entity_revision_id"] for row in old["entities"] if "Other" in row["name"] or "untouched(" in row["name"]
    }
    assert others
    journeys = JourneyService(service)
    flow = journeys.create(
        owner,
        pid,
        {
            "analysis_set_id": aid,
            "dsl": {
                "schema_version": "2.0",
                "key": "source-reference",
                "title": "Synthetic removed method reference",
                "entry": "reference",
                "nodes": [
                    {
                        "key": "reference",
                        "kind": "reference",
                        "title": "Source",
                        "x": 0,
                        "y": 0,
                        "binding": {"entity_id": removed["entity_id"], "revision_id": removed["entity_revision_id"]},
                    },
                    {"key": "end", "kind": "end", "title": "End", "x": 100, "y": 0},
                ],
                "edges": [{"key": "next", "source": "reference", "target": "end", "label": ""}],
            },
        },
        "journey",
    )
    release = publish(service, owner, pid, aid, "baseline")
    with service.db() as db:
        frozen = deepcopy(db.get(Release, release["id"]).manifest_json)
    draft = KnowledgeService(service).clone(owner, pid, aid, "New", "clone")
    after = snapshot({"Keep.java": b"package demo; class Keep { int keep(){return 1;} }"}, "b", "second")
    target = synced(
        service,
        owner,
        pid,
        after,
        "second",
        analysis_set_id=draft["id"],
        previous_snapshot_sha256=sha256(before).hexdigest(),
    )
    current = service.workspace(owner, pid, target)
    assert removed["entity_id"] not in {row["entity_id"] for row in current["entities"]}
    assert others <= {row["entity_revision_id"] for row in current["entities"]}
    with service.db() as db:
        assert db.get(Revision, removed["entity_revision_id"])
        assert db.get(Release, release["id"]).manifest_json == frozen
        assert {row["source_id"] for row in db.get(Analysis, target).sources} == {"backend", "another"}
        assert sum((row["source_id"] == "backend" for row in db.get(Analysis, target).sources)) == 1
        broken = list(
            db.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == target, Relation.to_revision_id == removed["entity_revision_id"]
                )
            )
        )
        assert broken and all((row.validity_status == "broken" and row.excluded is False for row in broken))
        report = VersionService(service).compare(db, pid, release["id"], target)
        assert any(
            (row["entity_id"] == removed["entity_id"] and row["change_kind"] == "removed" for row in report["changes"])
        )
        assert {row.id for row in broken} <= {row["id"] for row in report["impacted_relations"]}
        assert report["impact_paths"]
        assert any(
            (
                row["journey_id"] == flow["journey_id"] and row["binding_entity_id"] == removed["entity_id"]
                for row in report["journey_impacts"]
            )
        )
    historical = service.workspace(owner, pid, release_id=release["id"])
    assert removed["entity_revision_id"] in {row["entity_revision_id"] for row in historical["entities"]}


@pytest.mark.parametrize("change", [{"source": "renamed"}, {"provider": "local"}, {"role": "documentation"}])
def test_same_profile_changed_logical_scope_rejects_without_advancing_cursor(isolated, change):
    service, owner, pid = isolated
    files = {"Keep.java": b"package demo; class Keep {}", "Old.java": b"package demo; class Old {}"}
    raw = snapshot(files, "a", "first")
    aid = synced(service, owner, pid, raw, "first")
    before = service.workspace(owner, pid, aid)
    changed = snapshot({"Keep.java": files["Keep.java"]}, "b", "changed", **change)
    with service.db() as db:
        jobs = set(db.scalars(select(Job.id)))
    with pytest.raises(DomainError) as error:
        enqueue(
            service,
            owner,
            pid,
            changed,
            "changed",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(raw).hexdigest(),
        )
    assert error.value.code == "VKS_SYNC_SCOPE_CHANGED"
    assert service.workspace(owner, pid, aid)["selected_analysis_set"] == before["selected_analysis_set"]
    with service.db() as db:
        assert set(db.scalars(select(Job.id))) == jobs
    valid = snapshot({"Keep.java": files["Keep.java"]}, "c", "valid")
    assert (
        synced(
            service, owner, pid, valid, "valid", analysis_set_id=aid, previous_snapshot_sha256=sha256(raw).hexdigest()
        )
        == aid
    )


def test_ordinary_incremental_bundle_does_not_declare_absent_entities_deleted(isolated):
    service, owner, pid = isolated
    raw = snapshot(
        {"Keep.java": b"package demo; class Keep {}", "Old.java": b"package demo; class Old {}"}, "a", "first"
    )
    aid = synced(service, owner, pid, raw, "first")
    before = service.workspace(owner, pid, aid)
    missing = next((row["entity_revision_id"] for row in before["entities"] if "Old" in row["name"]))
    partial = snapshot({"Keep.java": b"package demo; class Keep { int newValue(){return 1;} }"}, "b", "increment")
    with service.db() as db:
        service._commit_bundle(db, pid, owner.id, parse_bundle(partial, "charge-demo"), aid)
    current = service.workspace(owner, pid, aid)
    assert missing in {row["entity_revision_id"] for row in current["entities"]}
    assert any(("newValue(" in row["name"] for row in current["entities"]))


def test_incomplete_parse_never_interprets_missing_symbols_as_deletions(isolated):
    service, owner, pid = isolated
    original = snapshot({"Keep.java": b"package demo; class Keep { int value(){return 1;} }"}, "a", "first")
    aid = synced(service, owner, pid, original, "first")
    before = service.workspace(owner, pid, aid)
    bad = snapshot(
        {"Keep.java": b"package demo; class Keep { int value( {", "Note.md": b"# Complete note\n"}, "b", "bad"
    )
    queued = enqueue(
        service, owner, pid, bad, "bad", analysis_set_id=aid, previous_snapshot_sha256=sha256(original).hexdigest()
    )
    service.process_job(queued["job"]["id"])
    job = service.get_job(owner, queued["job"]["id"])
    assert job["state"] == "failed" and job["error"]["code"] == "VKS_SYNC_INCOMPLETE", job
    assert service.workspace(owner, pid, aid)["selected_analysis_set"] == before["selected_analysis_set"]
    assert {row["entity_revision_id"] for row in service.workspace(owner, pid, aid)["entities"]} == {
        row["entity_revision_id"] for row in before["entities"]
    }
    recovered = snapshot({"Keep.java": b"package demo; class Keep {}"}, "c", "recovered")
    assert (
        synced(
            service,
            owner,
            pid,
            recovered,
            "recovered",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(original).hexdigest(),
        )
        == aid
    )


def test_per_source_deletion_storm_cannot_hide_behind_large_unchanged_sibling(isolated):
    service, owner, pid = isolated
    files = {f"backend/Note{i}.md": f"# Backend {i}\n".encode() for i in range(12)}
    sibling = {f"other/Note{i}.md": f"# Other {i}\n".encode() for i in range(40)}

    def combined(backend_files, baseline):
        bundles = [snapshot(backend_files, "a", baseline), snapshot(sibling, "a", baseline, source="another")]
        contents, sources, artifacts = ({}, [], [])
        for raw in bundles:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                sources.extend(manifest["sources"])
                artifacts.extend(manifest["artifacts"])
                contents.update({name: archive.read(name) for name in archive.namelist() if name != "manifest.json"})
        manifest.update(sources=sources, artifacts=artifacts)
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in {**contents, "manifest.json": json.dumps(manifest).encode()}.items():
                archive.writestr(name, raw)
        return output.getvalue()

    raw = combined(files, "first")
    aid = synced(service, owner, pid, raw, "first")
    before = service.workspace(owner, pid, aid)
    shrunken = combined({"backend/Note0.md": files["backend/Note0.md"]}, "shrunken")
    with pytest.raises(DomainError) as error:
        enqueue(
            service,
            owner,
            pid,
            shrunken,
            "shrunken",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(raw).hexdigest(),
        )
    assert error.value.code == "VKS_SYNC_SCOPE_ANOMALY"
    after = service.workspace(owner, pid, aid)
    assert after["selected_analysis_set"] == before["selected_analysis_set"]
    assert {row["entity_revision_id"] for row in after["entities"]} == {
        row["entity_revision_id"] for row in before["entities"]
    }


def test_edited_export_roundtrip_pins_current_identity_sections_and_archives_exact_evidence(isolated):
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
    payload = {"analysis_set_id": aid, "revision_id": saved["entity_revision_id"], "reason": "Inspect synthetic edit"}
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
                    "reason": "Inspect exact endpoints",
                },
                relation["row_version"],
            )
    release = publish(service, owner, pid, aid, "edited")
    with service.db() as db:
        current_art = db.get(Artifact, db.get(Revision, saved["entity_revision_id"]).artifact_id)
        historical = db.get(Artifact, db.get(Revision, old["entity_revision_id"]).artifact_id)
        expected_bytes = (service.data_dir / "objects" / historical.object_key).read_bytes()
        historical_id, historical_hash = (historical.id, historical.sha256)
        expected_evidence = {
            ev.id: deepcopy(ev.locator)
            for ev in db.scalars(
                select(Evidence).where(Evidence.analysis_set_id == aid, Evidence.artifact_id == historical_id)
            )
        }
        current_art.metadata_json = dict(current_art.metadata_json, requires_source_read=True)
    draft = KnowledgeService(service).clone(owner, pid, aid, "Later", "later")
    latest = docs.read_revision(owner, document["entity_id"], saved["entity_revision_id"])
    docs.save_draft(
        owner,
        document["entity_id"],
        {
            "analysis_set_id": draft["id"],
            "base_revision_id": latest["entity_revision_id"],
            "markdown": saved["markdown"] + "\nLater unrelated draft.\n",
            "locked_section_ids": [],
        },
        latest["row_version"],
    )
    export = service.export(
        owner, pid, {"release_id": release["id"], "format": "vksbundle", "include_source": True}, "export"
    )
    service.process_job(export["id"])
    assert service.get_job(owner, export["id"])["state"] == "succeeded"
    raw, _ = service.download_export(owner, pid, export["id"])
    parsed = parse_bundle(raw, "charge-demo")
    documents = [row for row in parsed.entities if row["type"] == "document"]
    assert [(row["stable_key"], row["content"]) for row in documents] == [(document["stable_key"], saved["markdown"])]
    historical_artifact = next((row for row in parsed.artifacts if row["sha256"] == historical_hash))
    assert historical_artifact["kind"] == "fixture" and historical_artifact["content"] == expected_bytes
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        provenance = json.loads(archive.read("export/provenance.json"))
    archived = {row["id"]: row for row in provenance["evidence"]}
    assert expected_evidence and expected_evidence.keys() <= archived.keys()
    for key, locator in expected_evidence.items():
        assert archived[key]["locator"] == locator
        assert archived[key]["artifact_sha256"] == historical_hash
        assert archived[key]["artifact_path"] == historical_artifact["path"]
    for source in parsed.sources:
        assert source["content_sha256"] == source_digest(
            [row for row in parsed.artifacts if row["source_id"] == source["source_id"]]
        )
    with service.db() as db:
        result = service._commit_bundle(db, pid, owner.id, parsed)
    restored = service.workspace(owner, pid, result["analysis_set_id"])
    assert len(restored["documents"]) == 1
    restored_doc = restored["documents"][0]
    restored_read = docs.read_revision(owner, restored_doc["entity_id"], restored_doc["entity_revision_id"])
    assert restored_read["markdown"] == saved["markdown"]
    assert [(row["stable_key"], row["content"]) for row in restored_read["sections"]] == [
        (row["stable_key"], row["content"]) for row in saved["sections"]
    ]
    assert all(
        (row["excluded"] is False and row["causal_basis"] != "correlated_runtime" for row in restored["relations"])
    )
    with service.db() as db:
        revision = db.get(Revision, restored_doc["entity_revision_id"])
        assert artifact_policy(db, db.get(Artifact, revision.artifact_id))["requires_source_read"]
