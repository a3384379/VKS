"""Independent audit regressions across read, export and mutation boundaries."""

from io import BytesIO
from pathlib import Path
import json
import zipfile

import pytest

from packages.analyzers.vks_ingest import parse_bundle
from vks_api.service import Service, DomainError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def published(tmp_path):
    service = Service(tmp_path / "store")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Access audit", "description": ""}, "create")
    bundle = parse_bundle((ROOT / "fixtures/charge-demo.vksbundle.zip").read_bytes(), "charge-demo")
    # Mark the imported MD confidential using the legitimate import metadata,
    # before committing it. No marker is inserted into logs or secret fields.
    for item in bundle.artifacts:
        if item["kind"] == "markdown":
            item["sensitivity"] = "confidential"
    with service.db() as session:
        result = service._commit_bundle(session, project["id"], owner.id, bundle)
    aid = result["analysis_set_id"]
    workspace = service.workspace(owner, project["id"], aid)
    for edge in workspace["relations"]:
        service.review_relation(
            owner,
            edge["id"],
            {
                "decision": "confirm",
                "reason": "Audit fixture: accept synthetic candidate",
                "evidence_ids": edge["evidence_ids"],
            },
            edge["row_version"],
        )
    candidate = service.request_release(owner, project["id"], {"analysis_set_id": aid}, "release-candidate")
    ticket = service.confirm_release(owner, candidate["candidate_id"])
    release = service.publish(
        owner,
        candidate["candidate_id"],
        {
            **{k: ticket[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")},
            "change_note": "Synthetic access audit release",
        },
        "publish",
    )
    token = service.create_token(
        owner,
        project["id"],
        {"name": "Business reader", "scopes": ["knowledge:read", "knowledge:export"], "expires_in_days": 1},
    )
    reader = service.authenticate(token["token"], "token")
    yield service, owner, reader, project, aid, release, workspace
    service.engine.dispose()


def test_confidential_document_cannot_leak_through_workspace_or_search(published):
    service, owner, reader, project, aid, release, _ = published
    workspace = service.workspace(reader, project["id"], release_id=release["id"])
    assert not workspace["documents"], "Confidential Markdown is returned to a reader without source:read"
    assert not any(e["type"] == "section" for e in workspace["entities"])
    assert (
        service.search(reader, project["id"], {"release_id": release["id"], "query": "申请金额必须大于零", "limit": 50})
        == []
    )


def test_confidential_document_revision_endpoint_requires_sensitive_permission(published):
    service, owner, reader, project, aid, release, workspace = published
    document = workspace["documents"][0]
    with pytest.raises(DomainError) as exc:
        service.get_document(reader, document["entity_id"], document["entity_revision_id"])
    assert exc.value.status in (403, 404)


def test_business_export_omits_confidential_body_and_hidden_relation_ids(published):
    service, owner, reader, project, aid, release, workspace = published
    job = service.export(
        reader,
        project["id"],
        {"release_id": release["id"], "format": "markdown_bundle", "include_source": False},
        "export",
    )
    service.process_job(job["id"])
    raw, _ = service.download_export(reader, project["id"], job["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        texts = "\n".join(archive.read(name).decode() for name in archive.namelist())
        assert "申请金额必须大于零" not in texts
        manifest = json.loads(archive.read("knowledge-manifest.json"))
        visible = {
            e["entity_revision_id"]
            for e in service.workspace(reader, project["id"], release_id=release["id"])["entities"]
        }
        assert all(r["from_revision_id"] in visible and r["to_revision_id"] in visible for r in manifest["relations"])


def test_proposal_endpoint_rejects_invalid_relation_endpoint_types(tmp_path):
    service = Service(tmp_path / "store")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Relation types"}, "create")
    bundle = parse_bundle((ROOT / "fixtures/charge-demo.vksbundle.zip").read_bytes(), "charge-demo")
    with service.db() as session:
        result = service._commit_bundle(session, project["id"], owner.id, bundle)
    workspace = service.workspace(owner, project["id"], result["analysis_set_id"])
    action = workspace["actions"][0]
    section = next(e for e in workspace["entities"] if e["type"] == "section")
    evidence_ids = workspace["relations"][0]["evidence_ids"]
    with pytest.raises(DomainError):
        service.propose_links(
            owner,
            project["id"],
            {
                "analysis_set_id": result["analysis_set_id"],
                "candidates": [
                    {
                        "from_revision_id": action["entity_revision_id"],
                        "to_revision_id": section["entity_revision_id"],
                        "relation_type": "maps_to_sql",
                        "evidence_ids": evidence_ids,
                        "reason": "This invalid type pair must be rejected",
                        "causal_basis": "not_established",
                    }
                ],
            },
            "invalid-pair",
        )
    service.engine.dispose()


def test_two_application_instances_cannot_both_accept_same_review_version(tmp_path):
    """A Python mutex alone does not coordinate separate API/worker processes."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from sqlalchemy import select
    from vks_api.models import Review

    first = Service(tmp_path / "store")
    _, owner = first.login(first.bootstrap_token)
    project = first.create_project(owner, {"key": "charge-demo", "name": "Two reviewer race"}, "p")
    bundle = parse_bundle((ROOT / "fixtures/charge-demo.vksbundle.zip").read_bytes(), "charge-demo")
    with first.db() as session:
        imported = first._commit_bundle(session, project["id"], owner.id, bundle)
    edge = first.workspace(owner, project["id"], imported["analysis_set_id"])["relations"][0]
    second = Service(tmp_path / "store")
    barrier = Barrier(2)
    for service in (first, second):
        original_version = service._version

        def gated_version(value, expected, original=original_version):
            original(value, expected)
            try:
                barrier.wait(timeout=0.3)
            except BrokenBarrierError:
                # Correct DB-level serialization may keep the second transaction
                # outside this gate until the first commit has completed.
                pass

        service._version = gated_version

    def review(service, decision):
        try:
            return service.review_relation(
                owner,
                edge["id"],
                {"decision": decision, "reason": "concurrent fixture review", "evidence_ids": edge["evidence_ids"]},
                1,
            )
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review, first, "confirm"), pool.submit(review, second, "reject")]
        results = [f.result(timeout=10) for f in futures]
    assert sum(isinstance(r, dict) for r in results) == 1, "Both stale writes were accepted"
    assert [r.status for r in results if isinstance(r, DomainError)] == [412]
    with first.db() as session:
        assert len(list(session.scalars(select(Review)))) == 1
    first.engine.dispose()
    second.engine.dispose()


def test_missing_uploaded_object_is_structured_failure_without_zombie_running(tmp_path):
    from hashlib import sha256

    service = Service(tmp_path / "store")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Storage fault"}, "p")
    content = (ROOT / "fixtures/minimal.vksbundle.zip").read_bytes()
    digest = sha256(content).hexdigest()
    upload = service.create_upload(
        owner,
        project["id"],
        {"filename": "bundle.zip", "size_bytes": len(content), "sha256": digest, "media_type": "application/zip"},
    )
    service.put_upload(owner, project["id"], upload["upload_session_id"], content)
    job = service.import_bundle(
        owner, project["id"], {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]}, "import"
    )
    (service.data_dir / "objects" / digest).unlink()
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "failed"
    assert result["error"]["code"].startswith("VKS_")
    service.engine.dispose()


def test_simultaneous_first_boot_keeps_one_owner_identity(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError

    store = tmp_path / "store"
    store.mkdir()
    (store / "bootstrap-token").write_text("synthetic-bootstrap-token-for-test")
    migrated = Service(store)
    migrated.engine.dispose()
    (store / "owner-id").unlink()
    original_exists = Path.exists
    barrier = Barrier(2)

    def gated_exists(path):
        exists = original_exists(path)
        if path == store / "owner-id" and not exists:
            try:
                barrier.wait(timeout=0.3)
            except BrokenBarrierError:
                pass
        return exists

    monkeypatch.setattr(Path, "exists", gated_exists)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(Service, store) for _ in range(2)]
        services = [future.result(timeout=10) for future in futures]
    assert services[0].owner_id == services[1].owner_id == (store / "owner-id").read_text().strip()
    for service in services:
        service.engine.dispose()
