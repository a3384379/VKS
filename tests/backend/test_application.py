from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Event
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from vks_api.app import create_app
from vks_api.service import Service, DomainError
from vks_api.models import Analysis, Revision, Selection, Relation, Review, Job, Release
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path):
    s = Service(tmp_path / "store")
    _, owner = s.login(s.bootstrap_token)
    p = s.create_project(owner, {"key": "charge-demo", "name": "测试知识工程"}, "project-create")
    yield s, owner, p
    s.stop_worker()
    s.engine.dispose()


def imported(s, owner, p, key="demo"):
    j = s.demo_import(owner, p["id"], key)
    s.process_job(j["id"])
    job = s.get_job(owner, j["id"])
    assert job["state"] == "succeeded", job
    return s.workspace(owner, p["id"], job["result"]["analysis_set_id"])


def confirm_all(s, owner, w):
    for r in w["relations"]:
        s.review_relation(
            owner,
            r["id"],
            {"decision": "confirm", "reason": "人工检查合成样例和绑定证据", "evidence_ids": r["evidence_ids"]},
            r["row_version"],
        )


def published(s, owner, p, w):
    confirm_all(s, owner, w)
    c = s.request_release(owner, p["id"], {"analysis_set_id": w["selected_analysis_set"]["id"]}, "candidate")
    t = s.confirm_release(owner, c["candidate_id"])
    payload = {k: t[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")}
    payload["change_note"] = "人工发布合成样例"
    return s.publish(owner, c["candidate_id"], payload, "publish"), c, payload


def token(s, owner, p, scopes):
    t = s.create_token(owner, p["id"], {"name": "test-client", "scopes": scopes, "expires_in_days": 1})
    return t, s.authenticate(t["token"], "token")


def test_import_has_fixed_versions_original_bytes_and_candidate_evidence(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    assert len(w["actions"]) == 4
    assert {"handler_of", "maps_to_sql", "writes_table", "documents"} <= {r["relation_type"] for r in w["relations"]}
    assert all(r["review_status"] == "candidate" for r in w["relations"])
    assert all(r["causal_basis"] != "correlated_runtime" for r in w["relations"])
    assert all(source["content_sha256"] and source["provider_revision"] for source in w["sources"])
    doc = w["documents"][0]
    raw, _ = s.artifact_bytes(owner, p["id"], doc["artifact_id"])
    assert raw == (ROOT / "fixtures/charge-demo/docs/invoice-rules.md").read_bytes()
    assert w["selected_analysis_set"]["runtime_alignment"] == "unknown"


def test_project_and_import_idempotency_reject_payload_conflict(setup):
    s, owner, p = setup
    assert s.create_project(owner, {"key": "charge-demo", "name": "测试知识工程"}, "project-create")["id"] == p["id"]
    with pytest.raises(DomainError) as exc:
        s.create_project(owner, {"key": "other", "name": "不同内容"}, "project-create")
    assert exc.value.status == 409
    j1 = s.demo_import(owner, p["id"], "demo")
    j2 = s.demo_import(owner, p["id"], "demo")
    assert j1["id"] == j2["id"]
    s.process_job(j1["id"])
    w = s.workspace(owner, p["id"])
    imported(s, owner, p, "second")
    assert len(s.workspace(owner, p["id"])["analysis_sets"]) == 1


def test_review_optimistic_lock_and_rejected_decisions_survive_reimport(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    r = w["relations"][0]
    result = s.review_relation(
        owner,
        r["id"],
        {"decision": "reject", "reason": "关联意义不成立", "evidence_ids": r["evidence_ids"]},
        r["row_version"],
    )
    assert result["relation"]["review_status"] == "rejected"
    with pytest.raises(DomainError) as exc:
        s.review_relation(
            owner,
            r["id"],
            {"decision": "confirm", "reason": "旧窗口尝试确认", "evidence_ids": r["evidence_ids"]},
            r["row_version"],
        )
    assert exc.value.status == 412
    imported(s, owner, p, "again")
    assert (
        next(x for x in s.workspace(owner, p["id"])["relations"] if x["id"] == r["id"])["review_status"] == "rejected"
    )


def test_publish_requires_review_bound_human_ticket_and_freezes_history(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    c = s.request_release(owner, p["id"], {"analysis_set_id": w["selected_analysis_set"]["id"]}, "before-review")
    assert c["blockers"]
    with pytest.raises(DomainError):
        s.confirm_release(owner, c["candidate_id"])
    release, c, payload = published(s, owner, p, w)
    assert s.publish(owner, c["candidate_id"], payload, "publish")["id"] == release["id"]
    before = s.workspace(owner, p["id"], release_id=release["id"])
    with pytest.raises(DomainError) as exc:
        s.review_relation(
            owner,
            w["relations"][0]["id"],
            {"decision": "reject", "reason": "发布后不能覆盖", "evidence_ids": w["relations"][0]["evidence_ids"]},
            2,
        )
    assert exc.value.status == 409
    assert s.workspace(owner, p["id"], release_id=release["id"])["relations"] == before["relations"]


def test_preflight_hash_invalidated_by_new_review(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    confirm_all(s, owner, w)
    c = s.request_release(owner, p["id"], {"analysis_set_id": w["selected_analysis_set"]["id"]}, "release-candidate")
    r = s.candidates(owner, w["selected_analysis_set"]["id"])[0]
    s.review_relation(
        owner,
        r["id"],
        {"decision": "reopen", "reason": "证据需再次复核", "evidence_ids": r["evidence_ids"]},
        r["row_version"],
    )
    with pytest.raises(DomainError) as exc:
        s.confirm_release(owner, c["candidate_id"])
    assert exc.value.status == 412


def test_scoped_machine_token_cannot_read_other_project_import_or_publish(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    release, c, payload = published(s, owner, p, w)
    t, reader = token(s, owner, p, ["knowledge:read"])
    assert len(s.list_projects(reader)[0]) == 1
    other = s.create_project(owner, {"key": "private-project", "name": "隐藏项目"}, "other")
    with pytest.raises(DomainError) as exc:
        s.workspace(reader, other["id"])
    assert exc.value.status == 404 and "隐藏项目" not in str(exc.value)
    with pytest.raises(DomainError) as exc:
        s.demo_import(reader, p["id"], "forbidden")
    assert exc.value.status == 403
    with pytest.raises(DomainError):
        s.publish(reader, c["candidate_id"], payload, "machine-publish")
    s.revoke_token(owner, p["id"], t["id"])
    with pytest.raises(DomainError) as exc:
        s.authenticate(t["token"], "token")
    assert exc.value.status == 401


def test_document_saves_new_revision_and_keeps_original_import(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    d = w["documents"][0]
    raw, _ = s.artifact_bytes(owner, p["id"], d["artifact_id"])
    saved = s.save_document(
        owner,
        d["entity_id"],
        {
            "analysis_set_id": w["selected_analysis_set"]["id"],
            "base_revision_id": d["entity_revision_id"],
            "markdown": d["content"] + "\n人工补充\n",
            "locked_section_ids": [],
        },
        d["row_version"],
    )
    assert saved["entity_revision_id"] != d["entity_revision_id"]
    assert s.get_document(owner, d["entity_id"], d["entity_revision_id"])["markdown"] == d["content"]
    assert s.artifact_bytes(owner, p["id"], d["artifact_id"])[0] == raw
    assert any(r["validity_status"] == "stale" for r in s.workspace(owner, p["id"])["relations"])
    with pytest.raises(DomainError) as exc:
        s.save_document(
            owner,
            d["entity_id"],
            {
                "analysis_set_id": w["selected_analysis_set"]["id"],
                "base_revision_id": d["entity_revision_id"],
                "markdown": "旧覆盖",
                "locked_section_ids": [],
            },
            1,
        )
    assert exc.value.status == 412


def test_cancel_drops_late_parser_results_then_retry_reuses_job(setup):
    s, owner, p = setup
    j = s.demo_import(owner, p["id"], "cancel")
    started = Event()
    resume = Event()
    original = s._parse

    def delayed(*args):
        result = original(*args)
        started.set()
        assert resume.wait(5)
        return result

    s._parse = delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(s.process_job, j["id"])
        assert started.wait(5)
        assert s.cancel_job(owner, j["id"])["state"] == "cancelling"
        resume.set()
        future.result(timeout=5)
    assert s.get_job(owner, j["id"])["state"] == "cancelled"
    assert not s.workspace(owner, p["id"])["analysis_sets"]
    s._parse = original
    s.retry_job(owner, j["id"])
    s.process_job(j["id"])
    assert s.get_job(owner, j["id"])["attempt"] == 2
    assert len(s.workspace(owner, p["id"])["analysis_sets"]) == 1


def test_export_original_markdown_is_exact_and_derived_explicit(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    release, _, _ = published(s, owner, p, w)
    j = s.export(
        owner, p["id"], {"release_id": release["id"], "format": "markdown_bundle", "include_source": False}, "export"
    )
    s.process_job(j["id"])
    raw, _ = s.download_export(owner, p["id"], j["id"])
    with zipfile.ZipFile(BytesIO(raw)) as z:
        original = [n for n in z.namelist() if n.startswith("originals/")][0]
        assert z.read(original) == (ROOT / "fixtures/charge-demo/docs/invoice-rules.md").read_bytes()
        manifest = json.loads(z.read("knowledge-manifest.json"))
        assert {f["kind"] for f in manifest["files"]} == {"original", "derived"}


def test_http_cookie_csrf_strict_fields_and_bearer_scope(tmp_path):
    app = create_app(tmp_path / "http", enable_worker=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/projects").status_code == 401
        assert c.get("/api/v1/auth/session").json()["data"]["authenticated"] is False
        response = c.post("/api/v1/auth/session", json={"bootstrap_token": app.state.service.bootstrap_token})
        csrf = response.json()["data"]["csrf_token"]
        assert (
            c.post(
                "/api/v1/projects", json={"key": "charge-demo", "name": "演示"}, headers={"Idempotency-Key": "create"}
            ).status_code
            == 403
        )
        c.headers["X-CSRF-Token"] = csrf
        assert (
            c.post(
                "/api/v1/projects",
                json={"key": "charge-demo", "name": "演示", "actorId": "forged"},
                headers={"Idempotency-Key": "forged"},
            ).status_code
            == 422
        )
        r = c.post(
            "/api/v1/projects", json={"key": "charge-demo", "name": "演示"}, headers={"Idempotency-Key": "create"}
        )
        assert r.status_code == 201
        pid = r.json()["data"]["id"]
        assert (
            c.post(
                "/api/v1/projects",
                json={"key": "evil", "name": "x"},
                headers={"Origin": "https://evil.example", "Idempotency-Key": "evil"},
            ).status_code
            == 403
        )
        r = c.post(f"/api/v1/projects/{pid}/tokens", json={"name": "reader"})
        bearer = r.json()["data"]["token"]
        with TestClient(app) as machine:
            machine.headers["Authorization"] = "Bearer " + bearer
            assert machine.get("/api/v1/auth/session").json()["data"]["human"] is False
            assert machine.get("/api/v1/projects").status_code == 200
            assert (
                machine.post(f"/api/v1/projects/{pid}/demo-import", headers={"Idempotency-Key": "attack"}).status_code
                == 403
            )
            assert machine.get("/api/v1/projects", headers={"Host": "attacker.example"}).status_code == 403


def test_framework_analyzer_replays_actual_fixed_sources_and_preserves_human_decisions(setup):
    s, owner, p = setup
    w = imported(s, owner, p)
    original = next(r for r in w["relations"] if r["relation_type"] == "handler_of")
    reviewed = s.review_relation(
        owner,
        original["id"],
        {"decision": "reject", "reason": "人工排除", "evidence_ids": original["evidence_ids"]},
        original["row_version"],
    )["relation"]
    job = s.create_analysis_job(
        owner,
        w["selected_analysis_set"]["id"],
        {"kind": "index", "analyzer_profile": "java-spring-mybatis-v1"},
        "reindex",
    )
    s.process_job(job["id"])
    result = s.get_job(owner, job["id"])
    assert result["state"] == "succeeded", result
    assert result["result"]["analysis_metrics"]["parser_version"]
    current = next(r for r in s.workspace(owner, p["id"])["relations"] if r["id"] == original["id"])
    assert current["review_status"] == "rejected" and current["row_version"] == reviewed["row_version"]


@pytest.mark.parametrize("format", ["pdf", "vksbundle"])
def test_release_export_formats_are_real_outputs(setup, format):
    s, owner, p = setup
    w = imported(s, owner, p)
    release, _, _ = published(s, owner, p, w)
    job = s.export(
        owner, p["id"], {"release_id": release["id"], "format": format, "include_source": False}, "export-format"
    )
    s.process_job(job["id"])
    state = s.get_job(owner, job["id"])
    assert state["state"] == "succeeded", state
    raw, filename = s.download_export(owner, p["id"], job["id"])
    assert raw.startswith(b"%PDF-") if format == "pdf" else raw.startswith(b"PK")
    assert filename.endswith(".pdf" if format == "pdf" else ".vksbundle.zip")
