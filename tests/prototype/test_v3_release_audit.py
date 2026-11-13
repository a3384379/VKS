import json
import os
import sqlite3
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select
from vks_api import identity
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Evidence, Relation, Revision
from vks_api.prototype_read import PrototypeReader
from vks_api.reading import ReadingService
from vks_api.service import DomainError
from vks_api.versions import VersionService

from packages.analyzers.vks_ingest import parse_bundle
from scripts.build_prototype_demo import PAGE_KEY
from tests.prototype.test_raster_prototype import original
from tests.prototype.test_v3_permissions import edit_anchors, separate_action_artifact
from tests.prototype.test_v3_workflow import _binding, _headers
from tests.prototype.test_v3_workflow import workspace as _workspace_fixture
from tests.ui.test_frontend_browser import live_api

workspace = _workspace_fixture


@pytest.mark.asyncio
async def test_official_stdio_preserves_draft_cas_and_exact_snapshot(workspace):
    app, _, service, owner, pid, aid, _, grant = workspace
    version, body = _binding(service, owner, pid, aid)
    root = Path(__file__).resolve().parents[2]
    with live_api(app) as url:
        settings = StdioServerParameters(
            command=sys.executable,
            args=["-m", "vks_api.mcp_gateway"],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(root / "services/api"), str(root)]),
                "VKS_API_URL": url,
                "VKS_TOKEN": grant["token"],
            },
        )
        async with stdio_client(settings) as (reader, writer):
            async with ClientSession(reader, writer) as sdk:
                assert (await sdk.initialize()).serverInfo.version == "3.0.0-rc.1"
                arguments = {
                    **body,
                    "project_id": pid,
                    "action_key": "charge.add",
                    "selection_version": version,
                    "idempotency_key": "stdio-save",
                }
                saved = await sdk.call_tool("save_business_point_binding", arguments)
                assert not saved.isError, saved.structuredContent
                replay = await sdk.call_tool("save_business_point_binding", arguments)
                assert not replay.isError and replay.structuredContent["data"] == saved.structuredContent["data"]
                stale = await sdk.call_tool("save_business_point_binding", {**arguments, "idempotency_key": "stale"})
                assert stale.isError and stale.structuredContent["error"]["code"] == "VKS_VERSION_CONFLICT"
                snapshot = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
                query = {
                    "project_id": pid,
                    "release_id": snapshot["id"],
                    "page_key": PAGE_KEY,
                    "action_key": "charge.add",
                    "trigger_state_key": "list",
                }
                context = await sdk.call_tool("get_prototype_action_context", query)
                assert not context.isError, context.structuredContent
                sources = context.structuredContent["data"]["points"][0]["sources"]
                assert sources[0]["section_revision_id"] == body["section_revision_ids"][0]
                service.revoke_token(owner, pid, grant["id"])
                denied = await sdk.call_tool("get_prototype_action_context", query)
                assert denied.isError and denied.structuredContent["error"]["code"] == "VKS_UNAUTHENTICATED"


def test_draft_prototype_context_reads_only_requested_selections(workspace, monkeypatch):
    _, web, _, _, pid, aid, _, grant = workspace

    def unexpected_workspace_scan(*_args, **_kwargs):
        raise AssertionError("Action context must use the selected page, action and related endpoints")

    monkeypatch.setattr(ReadingService, "selections", unexpected_workspace_scan)
    context = web.get(
        f"/api/v3/projects/{pid}/prototype/actions/charge.add/context",
        params={"analysis_set_id": aid, "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=_headers(grant, 0, "read"),
    )
    assert context.status_code == 200, context.text
    assert context.json()["data"]["documents"][0]["section_key"] == "charge.guide.create"
    empty = web.get(
        f"/api/v3/projects/{pid}/prototype/actions/charge.add/context",
        params={"analysis_set_id": aid, "page_key": PAGE_KEY, "trigger_state_key": ""},
        headers=_headers(grant, 0, "read"),
    )
    assert empty.status_code == 422


def test_large_draft_edge_selection_respects_sqlite_bind_limit(workspace):
    _, _, service, _, _, aid, _, _ = workspace
    reading = ReadingService(service)
    with service.db() as s:
        raw = s.connection().connection.driver_connection
        previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        try:
            rows = reading.edge_rows(s, aid, None, frontier=set())
            assert rows == []
            edges = list(s.scalars(select(Relation).where(Relation.analysis_set_id == aid)))
            first = edges[0]
            selected = {first.from_revision_id, first.to_revision_id, *(str(i) for i in range(1000))}
            visible = reading.edge_rows(s, aid, None, selected=selected)
            assert {row["id"] for row in visible} == {
                row.id for row in edges if {row.from_revision_id, row.to_revision_id} <= selected
            }
        finally:
            raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


def test_version_compares_binding_only_changes_without_exposing_restricted_sources(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    endpoint = f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points"
    version, body = _binding(service, owner, pid, aid)
    saved = web.post(endpoint, json=body, headers=_headers(grant, version, "original-binding"))
    assert saved.is_success, saved.text
    binding = saved.json()["data"]
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    draft = KnowledgeService(service).clone(owner, pid, aid, "Change binding scope", "clone")
    report = service.diff(owner, pid, release["id"], draft["id"])
    assert report["binding_changes"] == []
    changed = web.post(
        endpoint,
        json={
            **body,
            "analysis_set_id": draft["id"],
            "point": None,
            "point_revision_id": binding["point_revision_id"],
            "binding_version": 1,
            "state_keys": [],
            "priority": 12,
        },
        headers=_headers(grant, draft["selection_version"], "change-binding-scope"),
    )
    assert changed.is_success, changed.text
    report = service.diff(owner, pid, release["id"], draft["id"])
    assert report["changes"] == []
    (change,) = report["binding_changes"]
    assert change["relation_type"] == "has_business_point" and change["change_kind"] == "modified"
    assert change["before"]["prototype_scope"] == {"states": ["list"], "priority": 0}
    assert change["after"]["prototype_scope"] == {"states": [], "priority": 12}

    token = service.create_token(owner, pid, {"name": "Limited editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    limited = service.authenticate(token["token"], "token")
    assert len(service.diff(limited, pid, release["id"], draft["id"])["binding_changes"]) == 1
    with service.db() as s:
        source = s.get(Revision, body["section_revision_ids"][0])
        s.get(Artifact, source.artifact_id).sensitivity = "confidential"
    assert service.diff(limited, pid, release["id"], draft["id"])["binding_changes"] == []
    account = identity.create_user(
        service,
        owner,
        {"username": "audit-editor", "display_name": "Audit editor", "password": "local-synthetic-password"},
    )
    service.set_member(
        owner,
        pid,
        {"user_id": account["id"], "role": "editor", "status": "active"},
        service.get_project(owner, pid)["row_version"],
    )
    _, human = identity.password_login(service, "audit-editor", "local-synthetic-password")
    with pytest.raises(DomainError) as hidden:
        VersionService(service).revalidate(
            human,
            change["after"]["id"],
            {"analysis_set_id": draft["id"], "evidence_ids": []},
            change["after"]["row_version"],
        )
    assert hidden.value.status == 404


def test_binding_addition_omits_hidden_identity_from_previous_snapshot(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    _, old_action = separate_action_artifact(web, service, owner, pid, aid, grant)
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    draft = KnowledgeService(service).clone(owner, pid, aid, "Changed action", "clone")["id"]

    def rename(anchors):
        next(row for row in anchors if row["actionKey"] == "charge.add")["label"] = "Public new name"

    edit_anchors(web, service, owner, pid, draft, grant, "rename-draft", rename)
    version, body = _binding(service, owner, pid, draft)
    response = web.post(
        f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points",
        json=body,
        headers=_headers(grant, version, "new-binding"),
    )
    assert response.status_code == 200, response.text
    with service.db() as s:
        s.get(Artifact, old_action["artifact_id"]).sensitivity = "confidential"
    limited = service.authenticate(grant["token"], "token")
    visible = service.diff(limited, pid, release["id"], draft)["binding_changes"]
    assert not any(change["relation_type"] == "has_business_point" for change in visible)
    full = service.diff(owner, pid, release["id"], draft)["binding_changes"]
    assert any(change["relation_type"] == "has_business_point" and change["change_kind"] == "added" for change in full)


def test_binding_diff_omits_changes_to_hidden_auxiliary_evidence(workspace):
    _, _, service, owner, pid, aid, _, grant = workspace
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    draft = KnowledgeService(service).clone(owner, pid, aid, "Extra evidence", "clone")["id"]
    with service.db() as s:
        edge = s.scalar(
            select(Relation).where(Relation.analysis_set_id == draft, Relation.relation_type == "documents")
        )
        origin = s.get(Artifact, s.get(Evidence, edge.evidence_ids[0]).artifact_id)
        private = Artifact(
            project_id=pid,
            snapshot_id=origin.snapshot_id,
            relative_path="private-annotation.json",
            sha256=origin.sha256,
            size_bytes=origin.size_bytes,
            kind="fixture",
            media_type="application/json",
            sensitivity="confidential",
            object_key=origin.object_key,
        )
        s.add(private)
        s.flush()
        proof = Evidence(
            project_id=pid,
            analysis_set_id=draft,
            artifact_id=private.id,
            stable_key="private-annotation",
            evidence_kind="manual",
            content_sha256=private.sha256,
        )
        s.add(proof)
        s.flush()
        edge.evidence_ids = [*edge.evidence_ids, proof.id]
    limited = service.authenticate(grant["token"], "token")
    assert not service.diff(limited, pid, release["id"], draft)["binding_changes"]
    assert service.diff(owner, pid, release["id"], draft)["binding_changes"][0]["change_kind"] == "modified"


def test_saving_a_second_snapshot_keeps_explicit_default(workspace):
    _, _, service, owner, pid, aid, _, _ = workspace
    first = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "first")
    draft = KnowledgeService(service).clone(owner, pid, aid, "Second draft", "clone")["id"]
    second = service.save_snapshot(owner, pid, {"analysis_set_id": draft}, "second")
    project = service.get_project(owner, pid)
    assert project["current_release_id"] == first["id"]
    updated = service.patch_project(owner, pid, {"current_release_id": second["id"]}, project["row_version"])
    assert updated["current_release_id"] == second["id"]


def test_portable_source_vectors_require_whole_source_scope_at_build_and_download(workspace):
    _, _, service, owner, pid, aid, _, _ = workspace
    page = service.workspace(owner, pid, aid)["pages"][0]
    with service.db() as s:
        origin = s.get(Artifact, page["artifact_id"])
        sibling = Artifact(
            project_id=pid,
            snapshot_id=origin.snapshot_id,
            relative_path="sibling.json",
            sha256=origin.sha256,
            size_bytes=origin.size_bytes,
            kind="fixture",
            media_type="application/json",
            object_key=origin.object_key,
        )
        s.add(sibling)
        s.flush()
        sibling_id, source_id = sibling.id, sibling.snapshot_id
    snapshot = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    token = service.create_token(
        owner, pid, {"name": "Portable reader", "scopes": ["knowledge:read", "knowledge:export"]}
    )
    actor = service.authenticate(token["token"], "token")

    def run(key):
        job = service.export(actor, pid, {"release_id": snapshot["id"], "format": "vksbundle"}, key)
        service.process_job(job["id"])
        assert service.get_job(actor, job["id"])["state"] == "succeeded"
        return job["id"]

    old = run("before-reclassification")
    with service.db() as s:
        s.get(Artifact, sibling_id).sensitivity = "confidential"
    with pytest.raises(DomainError) as denied:
        service.download_export(actor, pid, old)
    assert denied.value.status == 403
    new = run("after-reclassification")
    data, _ = service.download_export(actor, pid, new)
    with ZipFile(BytesIO(data)) as archive:
        provenance = json.loads(archive.read("export/provenance.json"))
        assert source_id not in {source["source_snapshot_id"] for source in provenance["sources"]}
        assert len(provenance["prototype_originals"]) == 3


def test_portable_release_preserves_original_provenance_and_does_not_revive_stale_links(workspace):
    _, _, service, owner, pid, aid, raw, _ = workspace
    current = service.workspace(owner, pid, aid)
    page = next(row for row in current["pages"] if row["stable_key"] == PAGE_KEY)
    actions = {row["entity_revision_id"]: row["stable_key"] for row in current["actions"]}
    with service.db() as s:
        origin = s.get(Artifact, page["artifact_id"])
        protected = s.get(Revision, next(rid for rid, key in actions.items() if key == "charge.edit"))
        # An authored action can have its own classification, independent of the page surface.
        action_input = Artifact(
            project_id=pid,
            snapshot_id=origin.snapshot_id,
            relative_path="restricted-action-input.json",
            sha256=origin.sha256,
            size_bytes=origin.size_bytes,
            kind="fixture",
            media_type="application/json",
            sensitivity="confidential",
            object_key=origin.object_key,
        )
        s.add(action_input)
        s.flush()
        protected.artifact_id = action_input.id
        for edge in s.scalars(
            select(Relation).where(Relation.analysis_set_id == aid, Relation.relation_type == "documents")
        ):
            if actions[edge.from_revision_id] == "charge.add":
                edge.validity_status = "stale"

    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    job = service.export(
        owner, pid, {"release_id": release["id"], "format": "vksbundle", "include_source": True}, "portable"
    )
    service.process_job(job["id"])
    data, _ = service.download_export(owner, pid, job["id"])
    with ZipFile(BytesIO(data)) as archive:
        provenance = json.loads(archive.read("export/provenance.json"))
        originals = {item["original_relative_path"]: item for item in provenance["prototype_originals"]}
        captured = originals["views/list.png"]
        assert captured["source_snapshot_id"] in {source["source_snapshot_id"] for source in provenance["sources"]}
        assert any(entry["path"] == captured["path"] and entry["original"] for entry in provenance["files"])
        assert archive.read(captured["path"]) == original(raw)
    parsed = parse_bundle(data, "prototype-demo")
    assert not any(row["type"] == "documents" and row["from_key"] == "charge.add" for row in parsed.relations)
    artifacts = {row["path"]: row for row in parsed.artifacts}
    action = next(row for row in parsed.entities if row["stable_key"] == "charge.edit")
    assert artifacts[action["artifact_path"]]["sensitivity"] == "confidential"
    # The unmodified frozen release retains the outdated explanation for maintenance.
    assert PrototypeReader(service).context(
        owner, pid, PAGE_KEY, "charge.add", "list", None, release["id"], "outdated"
    )["documents"]
