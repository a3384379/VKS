"""R01/R02: immutable document membership and authoritative explicit identities."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import json
import zipfile

import pytest
from sqlalchemy import select

from packages.analyzers.vks_ingest import canonical_json, source_digest
from packages.collector.snapshots import Snapshot, bundle_bytes
from vks_api.documents import DocumentService, canonical, digest
from vks_api.knowledge import KnowledgeService
from vks_api.models import Analysis, Artifact, Entity, Evidence, Release, Review, Revision, Selection, uid
from vks_api.service import DomainError, Service

SAMPLE = Path(__file__).resolve().parents[2] / "docs/design/contracts/examples/minimal-explicit-binding"


@pytest.fixture
def store(tmp_path):
    service = Service(tmp_path / "objects", database_url=f"sqlite:///{tmp_path / 'reaudit.db'}")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Synthetic document reaudit"}, "project")
    yield SimpleNamespace(s=service, owner=owner, p=project["id"], aid=None)
    service.stop_worker()
    service.engine.dispose()


def commit(c, bundle, aid=None):
    with c.s.db() as db:
        result = c.s._commit_bundle(db, c.p, c.owner.id, bundle, aid or c.aid)
    c.aid = result["analysis_set_id"]
    return c.s.workspace(c.owner, c.p, c.aid)


def markdown(c, text):
    return c.s._parse(bundle_bytes(Snapshot("local", "fixed-history", {"docs/history.md": text.encode()}),
        "charge-demo", "history", source_key="history-docs", role="documentation"), "charge-demo")


def publish(c):
    for relation in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.review_relation(c.owner, relation["id"], dict(decision="confirm", reason="Synthetic review",
            evidence_ids=relation["evidence_ids"]), relation["row_version"])
    candidate = c.s.request_release(c.owner, c.p, {"analysis_set_id": c.aid}, "release-" + c.aid)
    assert not candidate["blockers"], candidate
    ticket = c.s.confirm_release(c.owner, candidate["candidate_id"])
    return c.s.publish(c.owner, candidate["candidate_id"], dict(ticket, change_note="Synthetic frozen baseline"), "publish-" + c.aid)


def explicit_bundle(c, swapped=False, path="docs/invoice-rules.md", logical_source="docs", baseline="explicit-demo-v1", as_bytes=False):
    files = {str(p.relative_to(SAMPLE)): p.read_bytes() for p in SAMPLE.rglob("*") if p.is_file()}
    old_path = "docs/invoice-rules.md"
    files.pop(old_path)
    files[path] = b"# Repeat\nSame body\n# Repeat\nSame body\n"
    content_hash = sha256(files[path]).hexdigest()
    keys = ["doc.repeat.first", "doc.repeat.second"]
    if swapped:
        keys.reverse()
    ui = json.loads(files["knowledge/ui-manifest.json"])
    ui["baselineKey"] = baseline
    ui["documentSections"] = [dict(sectionKey=key, artifactPath=path, sha256=content_hash,
        headingPath=["Repeat"], startLine=1 + index * 2, endLine=2 + index * 2) for index, key in enumerate(keys)]
    files["knowledge/ui-manifest.json"] = canonical_json(ui)
    bindings = json.loads(files["knowledge/bindings.json"])
    bindings["baselineKey"] = baseline
    bindings["evidence"] = bindings["evidence"][:1] + [dict(evidenceKey="evidence." + key, evidenceKind="static",
        artifactPath=path, sha256=content_hash, locator=dict(kind="document_section", sectionKey=key,
            startLine=1 + index * 2, endLine=2 + index * 2), claim="Synthetic explicit section") for index, key in enumerate(keys)]
    bindings["bindings"] = [dict(fromLogicalKey="ui.action.invoice.rules", toLogicalKey=key,
        relationType="documents", evidenceRefs=["evidence.invoice-dom", "evidence." + key], reason="Synthetic explicit link")
        for key in keys]
    files["knowledge/bindings.json"] = canonical_json(bindings)
    manifest = json.loads(files["manifest.json"])
    manifest["baseline_key"] = baseline
    for source in manifest["sources"]:
        if source["source_id"] == "docs":
            source["source_id"] = logical_source
    for artifact in manifest["artifacts"]:
        if artifact["path"] == old_path:
            artifact["path"] = path
            artifact["source_id"] = logical_source
        raw = files[artifact["path"]]
        artifact.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    for field in ("uiManifest", "bindings"):
        manifest[field]["sha256"] = sha256(files[manifest[field]["path"]]).hexdigest()
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest([a for a in manifest["artifacts"] if a["source_id"] == source["source_id"]])
    files["manifest.json"] = canonical_json(manifest)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output.getvalue() if as_bytes else c.s._parse(output.getvalue(), "charge-demo")


def test_r01_fixed_document_revision_membership_survives_clone_reimport_and_edit(store):
    c = store
    parsed = markdown(c, "# One\nOriginal one\n# Two\nOriginal two\n")
    workspace = commit(c, parsed)
    document = workspace["documents"][0]
    documents = DocumentService(c.s)
    before = documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    assert len(before["sections"]) == 2
    release = publish(c)
    with c.s.db() as db:
        frozen = deepcopy(db.get(Release, release["id"]).manifest_json)
    clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "New draft", "clone")
    commit(c, parsed, clone["id"])
    assert documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"]) == before
    current = c.s.workspace(c.owner, c.p, c.aid)["documents"][0]
    dto = documents.read_revision(c.owner, current["entity_id"], current["entity_revision_id"])
    documents.save_draft(c.owner, current["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=current["entity_revision_id"], markdown="# One\nNew body\n# Three\nNew chapter\n", locked_section_ids=[]), dto["row_version"])
    assert documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"]) == before
    assert len(c.s.workspace(c.owner, c.p, release_id=release["id"])["entities"]) == len(workspace["entities"])
    with c.s.db() as db:
        assert db.get(Release, release["id"]).manifest_json == frozen


def test_r02_explicit_duplicate_sections_reuse_keys_and_keep_correct_evidence_after_swap(store):
    c = store
    original = explicit_bundle(c)
    workspace = commit(c, original)
    document = workspace["documents"][0]
    documents = DocumentService(c.s)
    before = documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    last_document_revision = document["entity_revision_id"]
    identities = {e["stable_key"]: e["entity_id"] for e in workspace["entities"] if e["type"] == "section"}
    assert set(identities) == {"doc.repeat.first", "doc.repeat.second"}
    for relation in workspace["relations"]:
        c.s.review_relation(c.owner, relation["id"], dict(decision="confirm", reason="Synthetic review",
            evidence_ids=relation["evidence_ids"]), relation["row_version"])
    with c.s.db() as db:
        reviews = {r.id: deepcopy(r.evidence_ids) for r in db.scalars(select(Review))}
        old_evidence = {e.id: deepcopy(e.locator) for e in db.scalars(select(Evidence))}
    for bundle, first_line in ((original, 1), (explicit_bundle(c, swapped=True), 3)):
        current = commit(c, bundle)
        sections = {e["stable_key"]: e for e in current["entities"] if e["type"] == "section"}
        assert {key: e["entity_id"] for key, e in sections.items()} == identities
        assert sections["doc.repeat.first"]["locator"]["startLine"] == first_line
        current_document = current["documents"][0]
        assert current_document["content"] == document["content"]
        if first_line == 3:
            assert current_document["entity_revision_id"] != last_document_revision
        last_document_revision = current_document["entity_revision_id"]
        assert documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"]) == before
        revisions = {e["entity_revision_id"]: e for e in current["entities"]}
        evidence = {e["id"]: e for e in current["evidence"]}
        for relation in current["relations"]:
            target = revisions[relation["to_revision_id"]]
            assert relation["review_status"] == "confirmed"
            anchors = [evidence[eid] for eid in relation["evidence_ids"] if evidence[eid]["locator"]["relative_path"].endswith(".md")]
            assert anchors and all(e["locator"]["line_start"] == target["locator"]["startLine"] for e in anchors)
    with c.s.db() as db:
        assert {r.id: r.evidence_ids for r in db.scalars(select(Review))} == reviews
        assert all(db.get(Evidence, eid).locator == locator for eid, locator in old_evidence.items())


def test_r02_explicit_key_cannot_be_claimed_by_another_document(store):
    c = store
    commit(c, explicit_bundle(c))
    with pytest.raises(DomainError) as error:
        commit(c, explicit_bundle(c, path="docs/impostor.md"))
    assert error.value.status == 409


def legacy_document(c):
    """An isolated legacy row predating the server-owned membership field."""
    workspace = commit(c, markdown(c, "# One\nOriginal one\n# Two\nOriginal two\n"))
    document = workspace["documents"][0]
    with c.s.db() as db:
        revision = db.get(Revision, document["entity_revision_id"])
        revision.metadata_json = {k: v for k, v in revision.metadata_json.items() if k != "section_revision_ids"}
        revision.content_sha256 = digest(canonical(dict(artifact_id=revision.artifact_id, name=revision.name,
            content=revision.content, locator=revision.locator, metadata=revision.metadata_json, schema_version="1.0")))
    return document


def test_r01_legacy_revision_recovers_frozen_selection_without_modifying_history(store):
    c = store
    document = legacy_document(c)
    documents = DocumentService(c.s)
    before = documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    release = publish(c)
    clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "Legacy upgrade", "clone")
    commit(c, markdown(c, before["markdown"]), clone["id"])
    assert documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"]) == before
    with c.s.db() as db:
        assert "section_revision_ids" not in db.get(Revision, document["entity_revision_id"]).metadata_json
        assert len(db.get(Release, release["id"]).manifest_json["selections"]) == 3


@pytest.mark.parametrize("published", [False, True])
def test_r01_ambiguous_legacy_history_reports_a_gap_instead_of_selecting_latest(store, published):
    c = store
    document = legacy_document(c)
    documents = DocumentService(c.s)
    before = documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    if published:
        publish(c)
        clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "Conflicting legacy release", "clone")
        c.aid = clone["id"]
    with c.s.db() as db:
        old = db.get(Revision, before["sections"][0]["entity_revision_id"])
        extra = documents._revision(db, db.get(Entity, old.entity_id), db.get(Artifact, old.artifact_id), old.name,
            old.content, old.locator, dict(old.metadata_json, legacy_duplicate=True))
        if published:
            db.get(Selection, (c.aid, old.entity_id)).entity_revision_id = extra.id
            manifest = c.s._manifest(db, db.get(Analysis, c.aid))
            db.add(Release(id=uid(), project_id=c.p, analysis_set_id=c.aid, release_no="conflicting-legacy",
                manifest_json=manifest, manifest_sha256=digest(canonical(manifest)), published_by=c.owner.id,
                change_note="Synthetic historical conflict; not a new valid publication"))
    with pytest.raises(DomainError) as error:
        documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    assert error.value.code == "VKS_DOCUMENT_SECTIONS_UNRESOLVED" and error.value.status == 409


def test_r01_fixed_membership_blocks_publication_of_inconsistent_selections(store):
    c = store
    workspace = commit(c, markdown(c, "# One\nBody\n"))
    section = next(e for e in workspace["entities"] if e["type"] == "section")
    with c.s.db() as db:
        db.delete(db.get(Selection, (c.aid, section["entity_id"])))
        db.flush()
        assert DocumentService.assert_publishable(db, db.get(Analysis, c.aid))


def test_r01_generated_and_edited_documents_both_store_immutable_membership(audit_store, monkeypatch):
    from test_batch_one import generate, generated, model_stub
    c = audit_store
    model_stub(c, monkeypatch)
    assert generate(c, c.owner)["state"] == "succeeded"
    document = generated(c)[0]
    documents = DocumentService(c.s)
    before = documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    assert document["metadata"].get("section_revision_ids") == [row["entity_revision_id"] for row in before["sections"]]
    saved = documents.save_draft(c.owner, document["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=before["entity_revision_id"], markdown=before["markdown"] + "\n## Added\nSynthetic addition\n",
        locked_section_ids=[]), before["row_version"])
    with c.s.db() as db:
        assert db.get(Revision, saved["entity_revision_id"]).metadata_json["section_revision_ids"] == [row["entity_revision_id"] for row in saved["sections"]]
    assert documents.read_revision(c.owner, document["entity_id"], document["entity_revision_id"]) == before


def test_r02_explicit_key_cannot_change_logical_source(store):
    c = store
    commit(c, explicit_bundle(c))
    with pytest.raises(DomainError) as error:
        commit(c, explicit_bundle(c, logical_source="another-doc-source"))
    assert error.value.status == 409


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "foreign_project"])
def test_r01_malformed_membership_does_not_fall_back_to_artifact_scan(store, invalid):
    c = store
    workspace = commit(c, markdown(c, "# One\nOriginal\n"))
    document = workspace["documents"][0]
    section = next(e for e in workspace["entities"] if e["type"] == "section")
    ids = ["missing"] if invalid == "missing" else [section["entity_revision_id"], section["entity_revision_id"]]
    if invalid == "foreign_project":
        other = c.s.create_project(c.owner, {"key": "foreign", "name": "Foreign synthetic project"}, "foreign")
        parsed = c.s._parse(bundle_bytes(Snapshot("local", "foreign", {"foreign.md": b"# Foreign\nPrivate synthetic body\n"}),
            "foreign", "foreign", role="documentation"), "foreign")
        with c.s.db() as db:
            imported = c.s._commit_bundle(db, other["id"], c.owner.id, parsed)
        foreign = c.s.workspace(c.owner, other["id"], imported["analysis_set_id"])
        ids = [next(e["entity_revision_id"] for e in foreign["entities"] if e["type"] == "section")]
    with c.s.db() as db:
        row = db.get(Revision, document["entity_revision_id"])
        row.metadata_json = dict(row.metadata_json, section_revision_ids=ids, section_count=len(ids))
    with pytest.raises(DomainError) as error:
        DocumentService(c.s).read_revision(c.owner, document["entity_id"], document["entity_revision_id"])
    assert error.value.code == "VKS_DOCUMENT_SECTIONS_UNRESOLVED" and error.value.status == 409
    assert "Private synthetic body" not in error.value.message


@pytest.mark.asyncio
async def test_r02_real_mcp_sdk_imports_keep_explicit_operation_bindings(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from mcp.shared.memory import create_connected_server_and_client_session
    from vks_api.app import create_app
    from vks_api.mcp_gateway import APIBridge, create_server

    app = create_app(tmp_path / "sdk", enable_worker=False)
    service = app.state.service
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Synthetic SDK identities"}, "project")
    token = service.create_token(owner, project["id"], {"name": "Synthetic importer",
        "scopes": ["knowledge:read", "knowledge:edit", "source:import"]})
    machine = service.authenticate(token["token"], "token")
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    try:
        with TestClient(app) as web:
            async with create_connected_server_and_client_session(create_server(bridge)) as mcp:
                async def call(name, args):
                    result = await mcp.call_tool(name, args)
                    assert not result.isError, result.content
                    return result.structuredContent["data"]
                identities, historical_document, historical_detail = None, None, None
                for index in range(3):
                    raw = explicit_bundle(None, swapped=index == 2, baseline=f"sdk-explicit-{index}", as_bytes=True)
                    upload = service.create_upload(machine, project["id"], dict(filename="explicit.zip", size_bytes=len(raw),
                        sha256=sha256(raw).hexdigest(), media_type="application/zip"))
                    service.put_upload(machine, project["id"], upload["upload_session_id"], raw)
                    imported = await call("import_bundle", dict(project_id=project["id"], artifact_id=upload["artifact_id"],
                        idempotency_key=f"sdk-import-{index}"))
                    service.run_pending()
                    job = await call("get_job", dict(project_id=project["id"], job_id=imported["id"]))
                    assert job["state"] == "succeeded", job
                    aid = next(row["id"] for row in job["result_refs"] if row["kind"] == "analysis_set")
                    workspace = service.workspace(owner, project["id"], aid)
                    if historical_document is None:
                        historical_document = workspace["documents"][0]
                    response = web.get(f"/api/v1/documents/{historical_document['entity_id']}/revisions/{historical_document['entity_revision_id']}",
                        headers={"Authorization": "Bearer " + token["token"]})
                    assert response.is_success, response.text
                    detail = response.json()["data"]
                    assert len(detail["sections"]) == 2
                    assert historical_detail is None or detail == historical_detail
                    historical_detail = detail
                    current = {e["stable_key"]: e["entity_id"] for e in workspace["entities"] if e["type"] == "section"}
                    assert set(current) == {"doc.repeat.first", "doc.repeat.second"}
                    assert identities is None or identities == current
                    identities = current
                    graph = await call("trace_business_flow", dict(project_id=project["id"], analysis_set_id=aid,
                        start_entity_id=workspace["actions"][0]["entity_id"], relation_types=["documents"], depth=1))
                    assert {node["entity_id"] for node in graph["nodes"] if node["type"] == "section"} == set(identities.values())
    finally:
        service.stop_worker()
        service.engine.dispose()
