"""Independent negative boundaries for round-two source and export remediation."""

import json
import zipfile
from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pytest
from sqlalchemy import select
from test_round2_integrity_regressions import enqueue, publish, snapshot, synced
from test_round2_integrity_regressions import isolated as isolated  # noqa: PLC0414 - pytest fixture registration
from test_round2_integrity_sources_and_exports import upload
from vks_api.models import Analysis, Job, Relation, Selection
from vks_api.service import DomainError

from packages.analyzers.vks_ingest import parse_bundle


def rooted_bundle(files, revision, baseline, root):
    raw = snapshot(files, revision, baseline)
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    manifest["sources"][0]["root"] = root
    contents["manifest.json"] = json.dumps(manifest).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in contents.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_complete_sync_cannot_delete_target_with_unproven_different_source_root(isolated):
    service, owner, pid = isolated
    original = rooted_bundle({"authorized/Keep.java": b"package one; class Keep {}"}, "a", "authorized", "authorized")
    synced(service, owner, pid, original, "authorized")
    unrelated = rooted_bundle(
        {"different/Other.java": b"package two; class Other { int untouched(){return 9;} }"},
        "b",
        "different",
        "different",
    )
    imported = service.import_bundle(
        owner, pid, {"kind": "bundle", "artifact_ids": [upload(service, owner, pid, unrelated)]}, "different"
    )
    service.process_job(imported["id"])
    result = service.get_job(owner, imported["id"])
    assert result["state"] == "succeeded", result
    aid = result["result"]["analysis_set_id"]
    before = service.workspace(owner, pid, aid)
    source_before = deepcopy(before["selected_analysis_set"])
    after = rooted_bundle(
        {"authorized/Keep.java": b"package one; class Keep { int next(){return 2;} }"},
        "c",
        "next-authorized",
        "authorized",
    )
    queued = enqueue(
        service,
        owner,
        pid,
        after,
        "different-target",
        analysis_set_id=aid,
        previous_snapshot_sha256=sha256(original).hexdigest(),
    )
    service.process_job(queued["job"]["id"])
    completed = service.get_job(owner, queued["job"]["id"])
    current = service.workspace(owner, pid, aid)
    missing = {row["entity_revision_id"] for row in before["entities"]} - {
        row["entity_revision_id"] for row in current["entities"]
    }
    assert completed["state"] == "failed" and current["selected_analysis_set"] == source_before and not missing, {
        "job_state": completed["state"],
        "error": completed.get("error"),
        "target_revision_ids_deleted": sorted(missing),
        "authorized_source_root": "authorized",
        "target_source_root": "different",
    }


def test_legacy_success_without_source_scope_or_per_source_manifest_fails_closed(isolated):
    service, owner, pid = isolated
    raw = snapshot({"Keep.java": b"package demo; class Keep {}"}, "a", "legacy")
    aid = synced(service, owner, pid, raw, "first")
    before = service.workspace(owner, pid, aid)
    with service.db() as db:
        saved = db.scalar(select(Job).where(Job.kind == "import", Job.state == "succeeded"))
        metadata = dict(saved.input_json["_source_sync"])
        metadata.pop("source_scope")
        metadata.pop("source_manifests")
        saved.input_json = dict(saved.input_json, _source_sync=metadata)
        original_job_ids = set(db.scalars(select(Job.id)))
    next_raw = snapshot({"Keep.java": b"package demo; class Keep { int changed(){return 1;} }"}, "b", "next")
    with pytest.raises(DomainError) as exc:
        enqueue(
            service,
            owner,
            pid,
            next_raw,
            "legacy-update",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(raw).hexdigest(),
        )
    assert exc.value.code == "VKS_SYNC_SCOPE_CHANGED"
    assert service.workspace(owner, pid, aid)["selected_analysis_set"] == before["selected_analysis_set"]
    with service.db() as db:
        assert set(db.scalars(select(Job.id))) == original_job_ids


def test_export_of_partially_selected_source_artifact_cannot_resurrect_unselected_symbols(isolated):
    service, owner, pid = isolated
    raw = snapshot(
        {"Keep.java": b"package demo; class Keep { int selected(){return 1;} int unselected(){return 2;} }"},
        "a",
        "partial-selection",
    )
    aid = synced(service, owner, pid, raw, "first")
    before = service.workspace(owner, pid, aid)
    omitted = next(row for row in before["entities"] if "unselected(" in row["name"])
    # A selection is defined per immutable entity revision, not per artifact.
    # Seed a valid partial selection in this isolated store; release precheck
    # itself must verify that this state is accepted before export is exercised.
    with service.db() as db:
        db.delete(db.get(Selection, (aid, omitted["entity_id"])))
        for relation in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            if omitted["entity_revision_id"] in (relation.from_revision_id, relation.to_revision_id):
                db.delete(relation)
        db.get(Analysis, aid).selection_version += 1
    selected = service.workspace(owner, pid, aid)
    expected = {row["stable_key"] for row in selected["entities"]}
    assert omitted["stable_key"] not in expected
    release = publish(service, owner, pid, aid, "partial")
    job = service.export(
        owner, pid, {"release_id": release["id"], "format": "vksbundle", "include_source": True}, "export"
    )
    service.process_job(job["id"])
    finished = service.get_job(owner, job["id"])
    assert finished["state"] == "succeeded", finished
    exported, _ = service.download_export(owner, pid, job["id"])
    parsed = parse_bundle(exported, "charge-demo")
    unexpected = {row["stable_key"] for row in parsed.entities} - expected
    assert not unexpected, {
        "unexpected_keys": sorted(unexpected),
        "omitted_source_symbol": omitted["stable_key"],
        "selected_keys": sorted(expected),
    }
