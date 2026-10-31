"""Exact target scope and entity-level frozen selection negative boundaries."""

import json
import zipfile
from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pytest
from sqlalchemy import select
from test_round2_integrity_regressions import (
    enqueue,
    publish,
    snapshot,
    synced,
)
from test_round2_integrity_regressions import isolated as regression_store
from vks_api.knowledge import KnowledgeService
from vks_api.models import Analysis, Job, Relation, Release, Revision, Selection
from vks_api.service import DomainError

from packages.analyzers.vks_ingest import BundleError, parse_bundle, source_digest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    yield from regression_store.__wrapped__(tmp_path, monkeypatch)


def unpack(raw):
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def repack(files, manifest):
    for row in manifest["artifacts"]:
        if row["path"] in files:
            row.update(sha256=sha256(files[row["path"]]).hexdigest(), size_bytes=len(files[row["path"]]))
    for field in ("uiManifest", "bindings", "selectionManifest"):
        if field in manifest and manifest[field]["path"] in files:
            manifest[field]["sha256"] = sha256(files[manifest[field]["path"]]).hexdigest()
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [row for row in manifest["artifacts"] if row["source_id"] == source["source_id"]]
        )
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in {**files, "manifest.json": json.dumps(manifest).encode()}.items():
            archive.writestr(path, content)
    return output.getvalue()


def scoped(raw, *, root="authorized", connector="authorized-connector"):
    files = unpack(raw)
    manifest = json.loads(files["manifest.json"])
    manifest["sources"][0].update(root=root, connector_ref=connector)
    return repack(files, manifest)


@pytest.mark.parametrize(
    "boundary",
    ["different_root", "different_connector", "missing_vector_scope", "missing_revision_scope", "mixed_roots"],
)
def test_target_scope_must_be_proven_independently_from_profile_and_cursor(isolated, boundary):
    service, owner, pid = isolated
    original = scoped(snapshot({"authorized/Keep.java": b"package demo; class Keep {}"}, "a", "authorized"))
    authorized_aid = synced(service, owner, pid, original, "authorized")
    target_root = "different" if boundary in {"different_root", "mixed_roots"} else "authorized"
    target_connector = "different-connector" if boundary == "different_connector" else "authorized-connector"
    target_raw = scoped(
        snapshot(
            {f"{target_root}/Other.java": b"package demo; class Other { int untouched(){return 9;} }"}, "b", "target"
        ),
        root=target_root,
        connector=target_connector,
    )
    with service.db() as db:
        aid = service._commit_bundle(db, pid, owner.id, parse_bundle(target_raw, "charge-demo"))["analysis_set_id"]
        if boundary == "missing_vector_scope":
            target = db.get(Analysis, aid)
            target.sources = [
                {key: value for key, value in row.items() if key != "source_scope"} for row in target.sources
            ]
        elif boundary == "missing_revision_scope":
            # Simulate an exact pre-upgrade revision, retaining its immutable
            # no-scope hash rather than changing an already saved revision.
            choice = db.scalar(select(Selection).where(Selection.analysis_set_id == aid))
            previous = db.get(Revision, choice.entity_revision_id)
            metadata = {key: value for key, value in previous.metadata_json.items() if key != "source_scope"}
            from vks_api.models import uid
            from vks_api.service import canonical, sha

            full = {
                "artifact_id": previous.artifact_id,
                "locator": previous.locator,
                "metadata": metadata,
                "content": previous.content,
                "name": previous.name,
                "schema_version": "1.0",
            }
            legacy = Revision(
                id=uid(),
                project_id=pid,
                entity_id=previous.entity_id,
                artifact_id=previous.artifact_id,
                name=previous.name,
                content=previous.content,
                content_sha256=sha(canonical(full)),
                semantic_content_sha256=previous.semantic_content_sha256,
                locator=previous.locator,
                metadata_json=metadata,
            )
            db.add(legacy)
            db.flush()
            choice.entity_revision_id = legacy.id
        elif boundary == "mixed_roots":
            service._commit_bundle(db, pid, owner.id, parse_bundle(original, "charge-demo"), aid)
    before = service.workspace(owner, pid, aid)
    with service.db() as db:
        immutable = {row.id: (row.content_sha256, deepcopy(row.metadata_json)) for row in service._revisions(db, aid)}
    next_raw = scoped(
        snapshot({"authorized/Keep.java": b"package demo; class Keep { int next(){return 1;} }"}, "c", "next")
    )
    queued = enqueue(
        service,
        owner,
        pid,
        next_raw,
        "wrong-target",
        analysis_set_id=aid,
        previous_snapshot_sha256=sha256(original).hexdigest(),
    )
    service.process_job(queued["job"]["id"])
    failed = service.get_job(owner, queued["job"]["id"])
    assert failed["state"] == "failed" and failed["error"]["code"] == "VKS_SYNC_SCOPE_CHANGED", failed
    current = service.workspace(owner, pid, aid)
    assert current["selected_analysis_set"] == before["selected_analysis_set"]
    assert {row["entity_revision_id"] for row in current["entities"]} == {
        row["entity_revision_id"] for row in before["entities"]
    }
    with service.db() as db:
        assert {row.id: (row.content_sha256, row.metadata_json) for row in service._revisions(db, aid)} == immutable
    # Failure did not establish a successful cursor for the wrong target.
    assert (
        synced(
            service,
            owner,
            pid,
            next_raw,
            "right-target",
            analysis_set_id=authorized_aid,
            previous_snapshot_sha256=sha256(original).hexdigest(),
        )
        == authorized_aid
    )


def test_new_profile_does_not_authorize_unproven_target_but_verified_rebuild_does(isolated):
    service, owner, pid = isolated
    raw = scoped(snapshot({"authorized/Keep.java": b"package demo; class Keep {}"}, "a", "first"))
    aid = synced(service, owner, pid, raw, "first")
    release = publish(service, owner, pid, aid, "baseline")
    with service.db() as db:
        frozen = deepcopy(db.get(Release, release["id"]).manifest_json)
    draft = KnowledgeService(service).clone(owner, pid, aid, "Legacy draft", "clone")
    with service.db() as db:
        target = db.get(Analysis, draft["id"])
        target.sources = [{key: value for key, value in row.items() if key != "source_scope"} for row in target.sources]
    next_raw = scoped(
        snapshot({"authorized/Keep.java": b"package demo; class Keep { int next(){return 1;} }"}, "b", "second")
    )
    queued = enqueue(
        service,
        owner,
        pid,
        next_raw,
        "new-profile-old-target",
        analysis_set_id=draft["id"],
        profile_id="new-authorized-profile",
    )
    service.process_job(queued["job"]["id"])
    assert service.get_job(owner, queued["job"]["id"])["state"] == "failed"
    # Establish a fresh verified baseline from original materials; do not patch
    # provenance onto frozen old revisions or infer it from a new profile id.
    rebuilt = scoped(snapshot({"authorized/Keep.java": b"package demo; class Keep {}"}, "a", "rebuilt"))
    fresh = synced(service, owner, pid, rebuilt, "rebuilt", profile_id="new-authorized-profile")
    assert fresh != draft["id"]
    assert (
        synced(
            service,
            owner,
            pid,
            next_raw,
            "next",
            profile_id="new-authorized-profile",
            analysis_set_id=fresh,
            previous_snapshot_sha256=sha256(rebuilt).hexdigest(),
        )
        == fresh
    )
    with service.db() as db:
        assert db.get(Release, release["id"]).manifest_json == frozen


@pytest.mark.parametrize("trusted_scope", [True, False])
def test_reindex_retains_proven_scope_and_labels_without_authorizing_legacy_unknown_scope(isolated, trusted_scope):
    service, owner, pid = isolated
    raw = scoped(
        snapshot({"authorized/Keep.java": b"package demo; class Keep { int value(){return 1;} }"}, "a", "first")
    )
    files = unpack(raw)
    manifest = json.loads(files["manifest.json"])
    manifest["sources"][0]["version_labels"] = {"code_revision": "fixture-code"}
    raw = repack(files, manifest)
    aid = synced(service, owner, pid, raw, "first")
    with service.db() as db:
        target = db.get(Analysis, aid)
        expected_scope = deepcopy(target.sources[0]["source_scope"])
        immutable = {row.id: (row.content_sha256, deepcopy(row.metadata_json)) for row in service._revisions(db, aid)}
        if not trusted_scope:
            target.sources = [
                {key: value for key, value in row.items() if key != "source_scope"} for row in target.sources
            ]
    job = service.create_analysis_job(owner, aid, {"kind": "index"}, "index")
    service.process_job(job["id"])
    indexed = service.get_job(owner, job["id"])
    assert indexed["state"] == "succeeded", indexed
    with service.db() as db:
        vector = db.get(Analysis, aid).sources[0]
        assert vector["version_labels"] == {"code_revision": "fixture-code"}
        for rid, value in immutable.items():
            revision = db.get(Revision, rid)
            assert (revision.content_sha256, revision.metadata_json) == value
        if trusted_scope:
            assert vector["source_scope"] == expected_scope
            assert all(
                row.metadata_json["source_scope"] == dict(expected_scope, source_id="backend")
                for row in service._revisions(db, aid)
            )
        else:
            assert "source_scope" not in vector
            assert all("source_scope" not in row.metadata_json for row in service._revisions(db, aid))
    next_raw = scoped(snapshot({"authorized/Keep.java": b"package demo; class Keep {}"}, "b", "next"))
    queued = enqueue(
        service, owner, pid, next_raw, "next", analysis_set_id=aid, previous_snapshot_sha256=sha256(raw).hexdigest()
    )
    service.process_job(queued["job"]["id"])
    completed = service.get_job(owner, queued["job"]["id"])
    assert completed["state"] == ("succeeded" if trusted_scope else "failed"), completed
    if not trusted_scope:
        assert completed["error"]["code"] == "VKS_SYNC_SCOPE_CHANGED"


def partial_mixed_release(service, owner, pid):
    java = snapshot(
        {"src/Keep.java": b"package demo; class Keep { int selected(){return 1;} int omitted(){return 2;} }"},
        "a",
        "mixed",
    )
    docs = snapshot(
        {"docs/Rules.md": b"# Rules\n\n## Selected\nCurrent selected chapter.\n\n## Omitted\nRaw retained only.\n"},
        "a",
        "mixed",
        source="docs",
        role="documentation",
    )
    files = unpack(java)
    manifest = json.loads(files["manifest.json"])
    docs_files = unpack(docs)
    docs_manifest = json.loads(docs_files.pop("manifest.json"))
    files.update(docs_files)
    manifest["sources"].extend(docs_manifest["sources"])
    manifest["artifacts"].extend(docs_manifest["artifacts"])
    original = repack(files, manifest)
    aid = synced(service, owner, pid, original, "mixed")
    workspace = service.workspace(owner, pid, aid)
    keep = {
        row["entity_revision_id"]
        for row in workspace["entities"]
        if row["type"] == "symbol"
        and "omitted(" not in row["name"]
        or row["type"] == "section"
        and row["name"] == "Selected"
    }
    assert len(keep) == 3
    with service.db() as db:
        for choice in db.scalars(select(Selection).where(Selection.analysis_set_id == aid)):
            if choice.entity_revision_id not in keep:
                db.delete(choice)
        for relation in db.scalars(select(Relation).where(Relation.analysis_set_id == aid)):
            if relation.from_revision_id not in keep or relation.to_revision_id not in keep:
                db.delete(relation)
        db.get(Analysis, aid).selection_version += 1
    expected = {row["stable_key"]: row["type"] for row in service.workspace(owner, pid, aid)["entities"]}
    release = publish(service, owner, pid, aid, "partial")
    export = service.export(
        owner, pid, {"release_id": release["id"], "format": "vksbundle", "include_source": True}, "export"
    )
    service.process_job(export["id"])
    assert service.get_job(owner, export["id"])["state"] == "succeeded"
    exported, _ = service.download_export(owner, pid, export["id"])
    return original, exported, expected


def test_mixed_partial_release_restores_only_selected_symbols_and_chapters_with_raw_bytes(isolated):
    service, owner, pid = isolated
    original, exported, expected = partial_mixed_release(service, owner, pid)
    parsed = parse_bundle(exported, "charge-demo")
    assert {row["stable_key"]: row["type"] for row in parsed.entities} == expected
    assert not any(
        row["type"] == "document" or row["display_name"] == "Omitted" or "omitted(" in row["display_name"]
        for row in parsed.entities
    )
    originals = {name: raw for name, raw in unpack(original).items() if name != "manifest.json"}
    assert all(
        any(row["content"] == raw and row["sha256"] == sha256(raw).hexdigest() for row in parsed.artifacts)
        for raw in originals.values()
    )
    assert all(row["from_key"] in expected and row["to_key"] in expected for row in parsed.relations)
    with service.db() as db:
        aid = service._commit_bundle(db, pid, owner.id, parsed)["analysis_set_id"]
    restored = service.workspace(owner, pid, aid)
    assert {row["stable_key"]: row["type"] for row in restored["entities"]} == expected
    assert all((row["excluded"] is False) for row in restored["relations"])
    with service.db() as db:
        before_jobs = set(db.scalars(select(Job.id)))
    with pytest.raises(DomainError) as error:
        enqueue(service, owner, pid, exported, "not-a-full-sync", previous_snapshot_sha256=sha256(original).hexdigest())
    assert error.value.code == "VKS_SYNC_SCOPE_CHANGED"
    with service.db() as db:
        assert set(db.scalars(select(Job.id))) == before_jobs


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_reference",
        "missing_field",
        "duplicate_manifest",
        "malformed_selection",
        "unknown_key",
        "wrong_type",
        "wrong_hash",
        "wrong_source",
        "wrong_artifact",
        "duplicate_key",
    ],
)
def test_selection_manifest_rejects_forged_or_ambiguous_entity_references(isolated, tamper):
    service, owner, pid = isolated
    _, exported, _ = partial_mixed_release(service, owner, pid)
    files = unpack(exported)
    manifest = json.loads(files["manifest.json"])
    path = manifest["selectionManifest"]["path"]
    selection = json.loads(files[path])
    row = next(row for row in selection["entities"] if row["entityType"] == "symbol")
    expected_error = "VKS_REFERENCE_INVALID"
    if tamper == "missing_reference":
        manifest["selectionManifest"]["path"] = "missing.json"
    elif tamper == "missing_field":
        manifest.pop("selectionManifest")
    elif tamper == "duplicate_manifest":
        other_path = "export/ambiguous-selection.json"
        files[other_path] = files[path]
        record = next(value for value in manifest["artifacts"] if value["path"] == path)
        manifest["artifacts"].append(dict(record, path=other_path))
    elif tamper == "malformed_selection":
        selection.pop("kind")
        expected_error = "VKS_SCHEMA_INVALID"
    elif tamper == "unknown_key":
        row["logicalKey"] = "does.not.exist"
    elif tamper == "wrong_type":
        row["entityType"] = "document"
    elif tamper == "wrong_hash":
        row["sha256"] = "0" * 64
    elif tamper == "wrong_source":
        row["sourceId"] = "docs"
    elif tamper == "wrong_artifact":
        artifact = next(value for value in manifest["artifacts"] if value["kind"] == "markdown")
        row.update(artifactPath=artifact["path"], sha256=artifact["sha256"], sourceId=artifact["source_id"])
    else:
        selection["entities"].append(dict(row))
        expected_error = "VKS_DUPLICATE_LOGICAL_KEY"
    files[path] = json.dumps(selection).encode()
    with pytest.raises(BundleError) as error:
        parse_bundle(repack(files, manifest), "charge-demo")
    assert error.value.code == expected_error


def test_explicit_binding_cannot_reintroduce_unselected_section_endpoint(isolated):
    service, owner, pid = isolated
    original, exported, _ = partial_mixed_release(service, owner, pid)
    source = parse_bundle(original, "charge-demo")
    selected = next(row for row in source.entities if row["type"] == "section" and row["display_name"] == "Selected")
    omitted = next(row for row in source.entities if row["type"] == "section" and row["display_name"] == "Omitted")
    files = unpack(exported)
    manifest = json.loads(files["manifest.json"])
    artifact = next(row for row in manifest["artifacts"] if row["kind"] == "markdown")
    binding = {
        "schemaVersion": "1.0",
        "projectKey": manifest["project_key"],
        "baselineKey": manifest["baseline_key"],
        "evidence": [
            {
                "evidenceKey": "selected.evidence",
                "evidenceKind": "manual",
                "artifactPath": artifact["path"],
                "sha256": artifact["sha256"],
                "locator": {"kind": "file_lines", "startLine": 1, "endLine": 1},
            }
        ],
        "bindings": [
            {
                "fromLogicalKey": selected["stable_key"],
                "toLogicalKey": omitted["stable_key"],
                "relationType": "depends_on",
                "evidenceRefs": ["selected.evidence"],
            }
        ],
    }
    path = "export/escape-bindings.json"
    files[path] = json.dumps(binding).encode()
    manifest["artifacts"].append(
        {
            "source_id": "export-metadata",
            "kind": "binding_manifest",
            "path": path,
            "sha256": "0" * 64,
            "size_bytes": 0,
            "media_type": "application/json",
            "sensitivity": "internal",
        }
    )
    manifest["bindings"] = {"path": path, "sha256": "0" * 64}
    with pytest.raises(BundleError) as error:
        parse_bundle(repack(files, manifest), "charge-demo")
    assert error.value.code == "VKS_REFERENCE_INVALID"


def test_same_scope_complete_byte_inventory_with_partial_entity_mask_is_not_full_sync(isolated):
    service, owner, pid = isolated
    original = scoped(
        snapshot(
            {
                "authorized/Keep.java": b"package demo; class Keep { int selected(){return 1;} int omitted(){return 2;} }"
            },
            "a",
            "first",
        )
    )
    aid = synced(service, owner, pid, original, "first")
    before = service.workspace(owner, pid, aid)
    parsed = parse_bundle(original, "charge-demo")
    files = unpack(original)
    manifest = json.loads(files["manifest.json"])
    manifest.update(baseline_key="partial", import_key="partial")
    by_path = {row["path"]: row for row in manifest["artifacts"]}
    selection = {
        "kind": "fixed_entity_selection",
        "schemaVersion": "1.0",
        "projectKey": "charge-demo",
        "baselineKey": "partial",
        "entities": [
            {
                "logicalKey": row["stable_key"],
                "entityType": row["type"],
                "artifactPath": row["artifact_path"],
                "sha256": by_path[row["artifact_path"]]["sha256"],
                "sourceId": "backend",
            }
            for row in parsed.entities
            if "omitted(" not in row["display_name"]
        ],
    }
    path = "authorized/selection.json"
    files[path] = json.dumps(selection).encode()
    manifest["artifacts"].append(
        {
            "source_id": "backend",
            "kind": "fixture",
            "path": path,
            "sha256": "0" * 64,
            "size_bytes": 0,
            "media_type": "application/json",
            "sensitivity": "internal",
        }
    )
    manifest["selectionManifest"] = {"path": path, "sha256": "0" * 64}
    partial = repack(files, manifest)
    constrained = parse_bundle(partial, "charge-demo")
    assert constrained.sources[0]["provider"] == parsed.sources[0]["provider"]
    assert constrained.sources[0]["root"] == parsed.sources[0]["root"]
    assert constrained.sources[0]["connector_ref"] == parsed.sources[0]["connector_ref"]
    assert len(constrained.entities) == len(parsed.entities) - 1
    assert next(row["content"] for row in constrained.artifacts if row["kind"] == "source") == next(
        row["content"] for row in parsed.artifacts if row["kind"] == "source"
    )
    with service.db() as db:
        imported = service._commit_bundle(db, pid, owner.id, constrained)
        jobs = set(db.scalars(select(Job.id)))
    assert len(service.workspace(owner, pid, imported["analysis_set_id"])["entities"]) == len(constrained.entities)
    with pytest.raises(DomainError) as error:
        enqueue(
            service,
            owner,
            pid,
            partial,
            "partial-is-not-full",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(original).hexdigest(),
        )
    assert error.value.code == "VKS_SYNC_SCOPE_CHANGED"
    after = service.workspace(owner, pid, aid)
    assert after["selected_analysis_set"] == before["selected_analysis_set"]
    assert after["sources"] == before["sources"]
    assert {row["entity_revision_id"] for row in after["entities"]} == {
        row["entity_revision_id"] for row in before["entities"]
    }
    with service.db() as db:
        assert set(db.scalars(select(Job.id))) == jobs
    full_next = scoped(
        snapshot(
            {
                "authorized/Keep.java": b"package demo; class Keep { int selected(){return 3;} int omitted(){return 2;} }"
            },
            "b",
            "next",
        )
    )
    assert (
        synced(
            service,
            owner,
            pid,
            full_next,
            "next-full",
            analysis_set_id=aid,
            previous_snapshot_sha256=sha256(original).hexdigest(),
        )
        == aid
    )
