from pathlib import Path
import hashlib
from fastapi.testclient import TestClient
from vks_api.app import create_app
from vks_api.knowledge import KnowledgeService, SourceSnapshot
from vks_api.models import Selection, Relation, Analysis
from sqlalchemy import select


def setup_app(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    secret, owner = s.login(s.bootstrap_token)
    p = s.create_project(owner, {"key": "charge-demo", "name": "Demo"}, "p")
    job = s.demo_import(owner, p["id"], "demo")
    s.process_job(job["id"])
    w = s.workspace(owner, p["id"])
    return app, s, owner, p, w


def test_clone_reuses_immutable_revisions_and_remaps_evidence_in_draft(tmp_path):
    app, s, owner, p, w = setup_app(tmp_path)
    c = KnowledgeService(s).clone(owner, p["id"], w["selected_analysis_set"]["id"], "New draft", "clone1")
    assert c["id"] != w["selected_analysis_set"]["id"]
    assert (
        KnowledgeService(s).clone(owner, p["id"], w["selected_analysis_set"]["id"], "New draft", "clone1")["id"]
        == c["id"]
    )
    new = s.workspace(owner, p["id"], c["id"])
    assert {x["entity_revision_id"] for x in new["entities"]} == {x["entity_revision_id"] for x in w["entities"]}
    assert not {x["id"] for x in new["evidence"]} & {x["id"] for x in w["evidence"]}
    assert len(new["relations"]) == len(w["relations"])
    with s.db() as db:
        for r in db.scalars(select(Relation).where(Relation.analysis_set_id == c["id"])):
            s._verify_relation(db, r)


def test_standalone_markdown_import_and_same_source_revision_dedupe(tmp_path):
    app, s, owner, p, w = setup_app(tmp_path)
    clone = KnowledgeService(s).clone(owner, p["id"], w["selected_analysis_set"]["id"], "Docs", "clone")
    raw = b"\xef\xbb\xbf# New\r\n\r\n## Rules\r\nChinese fixture\r\n"
    u = s.create_upload(
        owner,
        p["id"],
        {
            "filename": "standalone.md",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "text/markdown",
        },
    )
    s.put_upload(owner, p["id"], u["upload_session_id"], raw)
    payload = {"kind": "documents", "artifact_ids": [u["artifact_id"]], "analysis_set_id": clone["id"]}
    j = s.import_bundle(owner, p["id"], payload, "docs")
    s.process_job(j["id"])
    assert s.get_job(owner, j["id"])["state"] == "succeeded", s.get_job(owner, j["id"])
    new = s.workspace(owner, p["id"], clone["id"])
    doc = next(d for d in new["documents"] if d["name"] == "New")
    assert s.artifact_bytes(owner, p["id"], doc["artifact_id"])[0] == raw
    assert len(new["documents"]) == 2
    count = len(KnowledgeService(s).snapshots(owner, p["id"]))
    again = s.import_bundle(owner, p["id"], payload, "docs-again")
    s.process_job(again["id"])
    assert s.get_job(owner, again["id"])["state"] == "succeeded"
    assert len(KnowledgeService(s).snapshots(owner, p["id"])) == count
    assert len(s.workspace(owner, p["id"], clone["id"])["documents"]) == 2
