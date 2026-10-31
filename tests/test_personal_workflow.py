"""Personal knowledge workflows have no approval or content masking gates."""

from hashlib import sha256

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from vks_api.app import create_app


def test_removed_modules_have_no_routes_or_runtime_tables(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    try:
        spec = app.openapi()
        paths = set(spec["paths"])
        assert not any(
            part in path
            for path in paths
            for part in ("/review", "/submit", "capture-approvals", "release-candidates", "confirm-plan")
        )
        assert "requires_document_review" not in __import__("json").dumps(spec)
        assert not {
            "review_decision",
            "document_review",
            "document_workflow",
            "capture_approval",
            "release_candidate",
            "update_proposal",
        } & set(inspect(app.state.service.engine).get_table_names())
        columns = {row["name"] for row in inspect(app.state.service.engine).get_columns("relation")}
        assert "review_status" not in columns and "excluded" in columns
    finally:
        app.state.service.engine.dispose()


def test_imported_relations_are_immediately_readable_and_snapshot_needs_no_signoff(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    service = app.state.service
    _, actor = service.login(service.bootstrap_token)
    try:
        project = service.create_project(actor, {"key": "charge-demo", "name": "Personal"}, "project")
        job = service.demo_import(actor, project["id"], "demo")
        service.process_job(job["id"])
        workspace = service.workspace(actor, project["id"])
        aid = workspace["selected_analysis_set"]["id"]
        assert workspace["relations"] and all("review_status" not in row for row in workspace["relations"])
        with TestClient(app) as client:
            session = client.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token}).json()[
                "data"
            ]
            result = client.post(
                f"/api/v2/projects/{project['id']}/snapshots",
                json={"analysis_set_id": aid, "change_note": "Personal snapshot"},
                headers={"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "snapshot"},
            )
            assert result.status_code == 201, result.text
            saved = result.json()["data"]
            assert (
                saved["manifest_sha256"]
                == sha256(
                    __import__("json")
                    .dumps(saved["manifest"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    .encode()
                ).hexdigest()
            )
    finally:
        service.engine.dispose()


def test_source_export_preserves_normal_contact_and_path_text(tmp_path):
    from types import SimpleNamespace
    from vks_api.exports import safe_original
    from vks_api.service import Service

    service = Service(tmp_path)
    raw = "联系 dev@example.com，路径 /Users/dev/project，测试手机号 13812345678".encode()
    key = service._put_object(raw)
    artifact = SimpleNamespace(kind="markdown", sha256=key, object_key=key, metadata_json={})
    try:
        assert safe_original(service, artifact) == raw
    finally:
        service.engine.dispose()


def test_legacy_migration_keeps_snapshot_bytes_and_excluded_user_choices(tmp_path):
    import copy
    import json
    from sqlalchemy import text
    from vks_api.models import Release
    from vks_api.service import Service
    from vks_api.personal_migration import RETIRED

    service = Service(tmp_path)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Legacy"}, "project")
    job = service.demo_import(actor, project["id"], "demo")
    service.process_job(job["id"])
    workspace = service.workspace(actor, project["id"])
    aid = workspace["selected_analysis_set"]["id"]
    excluded = workspace["relations"][0]
    service.set_relation_state(actor, excluded["id"], {"excluded": True}, excluded["row_version"])
    snapshot = service.save_snapshot(actor, project["id"], {"analysis_set_id": aid}, "save")
    with service.db() as db:
        release = db.get(Release, snapshot["id"])
        legacy = copy.deepcopy(release.manifest_json)
        for row in legacy["relations"]:
            row["review_status"] = "confirmed"
            row.pop("excluded")
        release.manifest_json = legacy
        release.manifest_sha256 = sha256(
            json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        expected_hash = release.manifest_sha256
    with service.engine.begin() as db:
        db.exec_driver_sql("ALTER TABLE relation ADD COLUMN review_status VARCHAR(20) NOT NULL DEFAULT 'candidate'")
        db.exec_driver_sql("UPDATE relation SET review_status=CASE WHEN excluded THEN 'rejected' ELSE 'candidate' END")
        db.exec_driver_sql("ALTER TABLE relation DROP COLUMN excluded")
        db.exec_driver_sql("CREATE TABLE review_decision (id VARCHAR(36) PRIMARY KEY)")
    service.engine.dispose()
    migrated = Service(tmp_path)
    try:
        with migrated.db() as db:
            stored = db.get(Release, snapshot["id"])
            assert stored.manifest_json == legacy and stored.manifest_sha256 == expected_hash
            rows = db.execute(text("SELECT id,excluded FROM relation")).all()
            assert [rid for rid, value in rows if value] == [excluded["id"]]
        assert not set(RETIRED) & set(inspect(migrated.engine).get_table_names())
        visible = migrated.workspace(actor, project["id"], release_id=snapshot["id"])
        assert visible["relations"] and all("review_status" not in row for row in visible["relations"])
    finally:
        migrated.engine.dispose()


def test_legacy_capture_keeps_input_digest_and_waiting_plans_resume_without_ticket(tmp_path, monkeypatch):
    from vks_api.models import Analysis, Job
    from vks_api.scenarios import CaptureRecord, ScenarioService
    from vks_api.service import Service
    from sqlalchemy import select

    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    service = Service(tmp_path)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "capture-migration", "name": "Capture"}, "project")
    analysis = service.create_analysis(actor, project["id"], {"name": "Draft", "sources": []}, "analysis")
    scenes = ScenarioService(service)
    scenes.create_environment(
        actor,
        project["id"],
        {
            "key": "local",
            "name": "Local",
            "kind": "mock",
            "base_origin": "http://127.0.0.1:8080",
            "allowed_origins": ["http://127.0.0.1:8080"],
            "allowed_cidrs": ["127.0.0.1/32"],
            "allow_loopback": True,
            "allowed_write_paths": [],
        },
    )
    scenes.create_dataset(
        actor,
        project["id"],
        {"key": "personal", "name": "Personal", "synthetic": False, "data": {"contact": "dev@example.test"}},
    )
    scene = scenes.create(
        actor,
        project["id"],
        {
            "analysis_set_id": analysis["id"],
            "dsl": {
                "schema_version": "1.0",
                "scenario_key": "local.read",
                "name": "Read",
                "environment_ref": "local",
                "dataset_ref": "personal",
                "risk_level": "read_only",
                "timeout_ms": 3000,
                "steps": [{"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 1000, "risk": "read_only"}],
            },
        },
        "scene",
    )
    accepted = scenes.request_capture(
        actor,
        scene["scenario_id"],
        {
            "analysis_set_id": analysis["id"],
            "scenario_revision_id": scene["scenario_revision_id"],
            "environment_ref": "local",
        },
        "capture",
    )
    with service.db() as db:
        record = db.scalar(select(CaptureRecord).where(CaptureRecord.job_id == accepted["job"]["id"]))
        capture_id, digest = record.id, record.input_sha256
        pending = service._new_job(
            db,
            actor,
            project["id"],
            "knowledge_task",
            {"analysis_set_id": analysis["id"], "task_kind": "knowledge_plan"},
        )
        pending.state, pending.stage = "paused", "PLAN_REVIEW"
        pending_id = pending.id
        db.get(Analysis, analysis["id"]).state = "ready_for_review"
    with service.engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE capture_approval (id VARCHAR(36) PRIMARY KEY, manifest_sha256 VARCHAR(64))")
        db.exec_driver_sql("INSERT INTO capture_approval VALUES (?,?)", ("legacy-ticket", digest))
        db.exec_driver_sql(
            "ALTER TABLE capture_session ADD COLUMN approval_id VARCHAR(36) NOT NULL DEFAULT 'legacy-ticket'"
        )
        db.exec_driver_sql(
            "ALTER TABLE capture_session ADD COLUMN redaction_version VARCHAR(64) NOT NULL DEFAULT 'legacy'"
        )
        db.exec_driver_sql("ALTER TABLE capture_session DROP COLUMN input_sha256")
    service.engine.dispose()
    migrated = Service(tmp_path)
    try:
        with migrated.db() as db:
            record = db.get(CaptureRecord, capture_id)
            assert record.input_sha256 == digest and record.state == "queued"
            assert db.get(Job, pending_id).state == "queued"
            assert db.get(Job, pending_id).stage == "QUEUED"
            assert db.get(Analysis, analysis["id"]).state == "draft"
        columns = {row["name"] for row in inspect(migrated.engine).get_columns("capture_session")}
        assert not {"approval_id", "redaction_version"} & columns
    finally:
        migrated.engine.dispose()


def test_standalone_first_boot_registers_every_model_before_migration(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    program = """
from sqlalchemy import inspect
from vks_api.service import Service
service = Service(__import__('sys').argv[1])
assert {'source_snapshot', 'capture_session', 'scenario_revision', 'project_member', 'model_policy', 'job_checkpoint'} <= set(inspect(service.engine).get_table_names())
_, actor = service.login(service.bootstrap_token)
project = service.create_project(actor, {'key': 'charge-demo', 'name': 'Standalone'}, 'project')
job = service.demo_import(actor, project['id'], 'demo')
service.process_job(job['id'])
assert service.get_job(actor, job['id'])['state'] == 'succeeded'
workspace = service.workspace(actor, project['id'])
assert workspace['actions'] and workspace['relations']
service.save_snapshot(actor, project['id'], {'analysis_set_id': workspace['selected_analysis_set']['id']}, 'save')
service.engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "standalone")],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "services/api") + os.pathsep + str(root)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
