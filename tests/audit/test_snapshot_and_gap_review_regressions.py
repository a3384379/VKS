"""Current permissions apply to snapshot replays; healthy links need no repair."""

from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from vks_api.app import create_app
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Artifact, Relation, Release, Revision
from vks_api.service import DomainError


@pytest.fixture
def store(tmp_path):
    app = create_app(tmp_path / "review-regressions", enable_worker=False)
    service = app.state.service
    token, actor = service.login(service.bootstrap_token)
    try:
        project = service.create_project(actor, {"key": "charge-demo", "name": "Review regressions"}, "project")
        job = service.demo_import(actor, project["id"], "demo")
        service.process_job(job["id"])
        yield service, actor, project["id"], service.workspace(actor, project["id"]), app, token
    finally:
        service.engine.dispose()


@pytest.mark.parametrize("permission", ["source:read", "trace:read"])
@pytest.mark.parametrize("key", ["original", "another-key"])
def test_snapshot_existing_and_idempotent_returns_recheck_current_revision_permissions(store, permission, key):
    service, owner, pid, workspace, _, _ = store
    aid = workspace["selected_analysis_set"]["id"]
    if permission == "trace:read":
        with service.db() as db:
            revision = db.get(Revision, workspace["actions"][0]["entity_revision_id"])
            artifact = db.get(Artifact, revision.artifact_id)
            artifact.metadata_json = dict(artifact.metadata_json, requires_trace_read=True)
    payload = {"analysis_set_id": aid}
    saved = service.save_snapshot(owner, pid, payload, "original")
    assert service.save_snapshot(owner, pid, payload, key) == saved
    limited = replace(owner, scopes=owner.scopes - {permission})
    with pytest.raises(DomainError) as denied:
        service.save_snapshot(limited, pid, payload, key)
    assert denied.value.status == 403
    with service.db() as db:
        assert db.scalar(select(func.count()).select_from(Release)) == 1
        assert db.get(Release, saved["id"]).manifest_json == saved["manifest"]


@pytest.mark.parametrize("key", ["original", "another-key"])
def test_snapshot_replays_recheck_relation_provenance_after_classification_changes(store, key):
    service, owner, pid, workspace, _, _ = store
    payload = {"analysis_set_id": workspace["selected_analysis_set"]["id"]}
    saved = service.save_snapshot(owner, pid, payload, "original")
    limited = replace(owner, scopes=owner.scopes - {"trace:read"})
    assert service.save_snapshot(limited, pid, payload, key) == saved
    with service.db() as db:
        relation = db.get(Relation, workspace["relations"][0]["id"])
        relation.metadata_json = dict(relation.metadata_json, generation_kind="model")
    with pytest.raises(DomainError) as denied:
        service.save_snapshot(limited, pid, payload, key)
    assert denied.value.status == 403
    with service.db() as db:
        assert db.get(Release, saved["id"]).manifest_json == saved["manifest"]


def test_machine_editor_cannot_retrieve_an_existing_restricted_snapshot_over_http(store):
    service, owner, pid, workspace, app, _ = store
    payload = {"analysis_set_id": workspace["selected_analysis_set"]["id"]}
    service.save_snapshot(owner, pid, payload, "original")
    token = service.create_token(owner, pid, {"name": "Limited editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    with TestClient(app) as client:
        response = client.post(
            f"/api/v2/projects/{pid}/snapshots",
            json=payload,
            headers={"Authorization": "Bearer " + token["token"], "Idempotency-Key": "limited-editor"},
        )
    assert response.status_code == 403, response.text
    assert "manifest" not in response.json().get("data", {})


@pytest.mark.parametrize("outdated", [False, True])
def test_evidence_gap_task_reports_only_active_stale_or_broken_relations(store, outdated):
    service, owner, pid, workspace, _, _ = store
    expected = set()
    if outdated:
        with service.db() as db:
            for row, validity, excluded in zip(
                workspace["relations"][:4], ("stale", "broken", "valid", "stale"), (False, False, True, True)
            ):
                relation = db.get(Relation, row["id"])
                relation.validity_status, relation.excluded = validity, excluded
                if not excluded and validity != "valid":
                    expected.add(relation.id)
    tasks = KnowledgeTaskService(service)
    payload = TaskInput(analysis_set_id=workspace["selected_analysis_set"]["id"], task_kind="evidence_gaps").model_dump()
    job = tasks.create(owner, pid, payload, "gap-report")
    service.process_job(job["id"])
    result = tasks.get(owner, job["id"])
    assert result["state"] == "succeeded", result
    assert set(result["report"]["outdated_relation_ids"]) == expected
