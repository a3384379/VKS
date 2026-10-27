from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_journeys import store
from packages.collector.agent import AgentClient, LocalCollectorAgent, authorize_profile
from packages.collector.snapshots import CollectionError
from vks_api.models import Job


def setup(store, tmp_path, *, scopes=None):
    service, actor, pid, w, app, _ = store
    root = tmp_path / "资料"
    root.mkdir()
    (root / "rules.md").write_bytes(b"\xef\xbb\xbf# Original\r\nOriginal bytes\r\n")
    (root / ".env").write_text("NOT_UPLOADED=secret")
    profile = authorize_profile(
        root=str(root),
        provider="directory",
        project_id=pid,
        project_key="charge-demo",
        source_key="agent-source",
        base_url="http://127.0.0.1",
        consent=True,
    )
    agent = LocalCollectorAgent(tmp_path / ".vks/agent")
    agent.save_profile(profile)
    token = service.create_token(
        actor, pid, dict(name="Collector only", scopes=scopes or ["knowledge:read", "source:read", "source:import"])
    )["token"]
    api = TestClient(app)
    requests = []
    fault = {"drop": False}

    def handle(request):
        requests.append((request.method, request.url.path))
        response = api.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        if request.url.path.endswith("/source-syncs") and fault["drop"]:
            fault["drop"] = False
            raise httpx.ReadError("Synthetic lost response")
        return httpx.Response(response.status_code, json=response.json())

    client = AgentClient(profile.base_url, token, transport=httpx.MockTransport(handle))
    return agent, profile, root, client, requests, fault, token


def test_agent_cursor_waits_for_server_commit_keeps_raw_bytes_and_no_credentials(store, tmp_path):
    agent, profile, root, client, requests, _, token = setup(store, tmp_path)
    service, actor, pid, w, app, _ = store
    try:
        pending = agent.sync_once(profile, client)
        base = agent.paths(profile)
        assert pending["state"] == "queued" and not (base / "cursor.json").exists()
        assert set(pending["files"]) == {"rules.md"}
        agent.sync_once(profile, client)
        assert sum(url.endswith("/source-syncs") for _, url in requests) == 1
        service.process_job(pending["job_id"])
        result = agent.sync_once(profile, client)
        assert result["state"] == "succeeded"
        assert result["files"]["rules.md"] == sha256((root / "rules.md").read_bytes()).hexdigest()
        assert agent.sync_once(profile, client)["state"] == "unchanged"
        assert token not in (base / "profile.json").read_text() and token not in (base / "cursor.json").read_text()
        assert agent.load_profiles() == [profile]
    finally:
        client.close()


def test_lost_enqueue_response_replays_exact_intent_once_and_does_not_advance_cursor(store, tmp_path):
    agent, profile, root, client, requests, fault, token = setup(store, tmp_path)
    service, actor, pid, w, app, _ = store
    try:
        fault["drop"] = True
        with pytest.raises(CollectionError):
            agent.sync_once(profile, client)
        base = agent.paths(profile)
        intent = json.loads((base / "pending.json").read_bytes())
        assert intent["state"] == "import_intent" and not (base / "cursor.json").exists()
        resumed = agent.sync_once(profile, client)
        with service.db() as db:
            jobs = list(db.scalars(select(Job).where(Job.project_id == pid, Job.created_by != actor.id)))
            assert len(jobs) == 1 and jobs[0].id == resumed["job_id"]
        service.process_job(resumed["job_id"])
        first = agent.sync_once(profile, client)
        (root / "rules.md").write_text("# Updated\nNext fixed version\n")
        second = agent.sync_once(profile, client)
        assert json.loads((base / "cursor.json").read_bytes())["snapshot_sha256"] == first["snapshot_sha256"]
        service.process_job(second["job_id"])
        latest = agent.sync_once(profile, client)
        assert latest["state"] == "succeeded" and latest["snapshot_sha256"] != first["snapshot_sha256"]
        assert latest["analysis_set_id"] != first["analysis_set_id"]
        assert latest["delta"]["modified"] == ["rules.md"]
    finally:
        client.close()


def test_agent_rejects_scope_changes_unapproved_paths_insecure_urls_and_corrupted_pending(store, tmp_path):
    agent, profile, root, client, *_ = setup(store, tmp_path)
    try:
        with pytest.raises(CollectionError):
            replace(profile, root=str(tmp_path)).validate()
        with pytest.raises(CollectionError):
            agent.preview(replace(profile, consent_sha256="0" * 64))
        with pytest.raises(CollectionError):
            AgentClient("http://example.org", "private-token")
        with pytest.raises(CollectionError):
            authorize_profile(
                root=str(root),
                provider="directory",
                project_id=profile.project_id,
                project_key="charge-demo",
                source_key="source",
                base_url=profile.base_url,
                consent=False,
            )
        agent.preview(profile)
        (agent.paths(profile) / "snapshot.zip").write_bytes(b"corrupt")
        with pytest.raises(CollectionError) as error:
            agent.sync_once(profile, client)
        assert error.value.code == "VKS_HASH_MISMATCH"
    finally:
        client.close()


def test_server_rechecks_missing_file_storm_and_does_not_advance_client_receipt(store, tmp_path):
    agent, profile, root, client, requests, fault, token = setup(store, tmp_path)
    service = store[0]
    try:
        for i in range(12):
            (root / f"note-{i}.md").write_text(f"# Synthetic {i}\n")
        first = agent.sync_once(profile, client)
        service.process_job(first["job_id"])
        agent.sync_once(profile, client)
        for file in root.glob("note-*.md"):
            file.unlink()
        with pytest.raises(CollectionError) as error:
            agent.preview(profile)
        assert error.value.code == "VKS_SYNC_SCOPE_ANOMALY"
        assert not (agent.paths(profile) / "pending.json").exists()
    finally:
        client.close()


def test_explicit_sync_target_and_change_event_create_one_impact_job_without_publishing(store, tmp_path):
    from packages.collector.agent import digest
    from vks_api.knowledge import KnowledgeService
    from v2_support import published

    service, actor, pid, w, _, _ = store
    release, w = published(service, actor, {"id": pid})
    clone = KnowledgeService(service).clone(actor, pid, w["selected_analysis_set"]["id"], "Sync target", "clone-sync")
    agent, profile, root, client, *_ = setup(
        store, tmp_path, scopes=["knowledge:read", "source:read", "source:import", "analysis:run"]
    )
    profile = replace(profile, analysis_set_id=clone["id"], detect_changes=True)
    profile = replace(profile, consent_sha256=digest(profile.scope()))
    try:
        pending = agent.sync_once(profile, client)
        service.process_job(pending["job_id"])
        result = agent.sync_once(profile, client)
        assert result["state"] == "succeeded" and result["analysis_set_id"] == clone["id"]
        assert result["impact_job_id"]
        service.process_job(result["impact_job_id"])
        report = service.get_job(actor, result["impact_job_id"])
        assert report["state"] == "succeeded", report
        assert report["result"]["from_release_id"] == release["id"]
        assert (
            service.workspace(actor, pid, release_id=release["id"])["selected_release"]["manifest_sha256"]
            == release["manifest_sha256"]
        )
        with service.db() as db:
            rows = list(db.scalars(select(Job).where(Job.project_id == pid, Job.kind == "detect_drift")))
            assert len(rows) == 1
    finally:
        client.close()


def test_inline_secret_is_rejected_locally_before_upload_or_receipt(store, tmp_path):
    agent, profile, root, client, requests, *_ = setup(store, tmp_path)
    original = b'password = "synthetic-inline-secret-001"\r\n'
    (root / "config.properties").write_bytes(original)
    try:
        with pytest.raises(CollectionError) as error:
            agent.sync_once(profile, client)
        assert error.value.code == "VKS_SECRET_DETECTED"
        assert not requests
        assert not (agent.paths(profile) / "pending.json").exists()
        assert not (agent.paths(profile) / "snapshot.zip").exists()
        assert (root / "config.properties").read_bytes() == original
    finally:
        client.close()


def test_server_enforces_cursor_scope_storm_and_impact_permission_even_when_agent_bypassed(store, tmp_path):
    from vks_api.source_sync import SourceSyncService
    from vks_api.service import DomainError
    from packages.collector.snapshots import Snapshot, bundle_bytes

    agent, profile, root, client, _, _, token = setup(store, tmp_path)
    service, _, pid, *_ = store
    machine = service.authenticate(token, "token")
    try:
        for i in range(12):
            (root / f"note-{i}.md").write_text(f"# Synthetic {i}\n")
        first = agent.sync_once(profile, client)
        service.process_job(first["job_id"])
        receipt = agent.sync_once(profile, client)
        raw = bundle_bytes(
            Snapshot("local", "shrunken", {"rules.md": (root / "rules.md").read_bytes()}),
            "charge-demo",
            "shrunken",
            source_key=profile.source_key,
        )
        upload = service.create_upload(
            machine,
            pid,
            dict(
                filename="source.zip", size_bytes=len(raw), sha256=sha256(raw).hexdigest(), media_type="application/zip"
            ),
        )
        service.put_upload(machine, pid, upload["upload_session_id"], raw)
        payload = dict(
            profile_id=profile.id,
            consent_sha256=profile.consent_sha256,
            artifact_id=upload["artifact_id"],
            snapshot_sha256=sha256(raw).hexdigest(),
            previous_snapshot_sha256=receipt["snapshot_sha256"],
        )
        api = SourceSyncService(service)
        for key, changed, expected in [
            ("storm-bypass", {}, "VKS_SYNC_SCOPE_ANOMALY"),
            ("stale-bypass", dict(previous_snapshot_sha256="0" * 64), "VKS_VERSION_CONFLICT"),
            ("scope-bypass", dict(consent_sha256="0" * 64), "VKS_SYNC_SCOPE_CHANGED"),
            ("impact-bypass", dict(detect_changes=True), "VKS_FORBIDDEN"),
        ]:
            with pytest.raises(DomainError) as error:
                api.create(machine, pid, dict(payload, **changed), key)
            assert error.value.code == expected
        assert (
            json.loads((agent.paths(profile) / "cursor.json").read_bytes())["snapshot_sha256"]
            == receipt["snapshot_sha256"]
        )
    finally:
        client.close()
