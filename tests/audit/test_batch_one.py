"""A01–A04: authorization ceilings, input provenance and governed replacement."""
from datetime import datetime, timezone
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from packages.collector.snapshots import Snapshot, bundle_bytes
from vks_api.ai_gateway import AiService, ModelPolicy, PolicyInput
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService, SourceSnapshot
from vks_api.models import Analysis, Artifact, Evidence, Job, Relation, Revision, Selection, uid
from vks_api.oauth import OAuthGrant, OAuthIssuer, OAuthPrincipal
from vks_api.service import DomainError


def oauth_actor(c, scopes):
    scopes = [*scopes, "analysis:run"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="audit", alg="RS256", use="sig")
    with c.s.db() as db:
        issuer = OAuthIssuer(id=uid(), issuer="https://audit.invalid", audience="audit-resource", jwks={"keys": [public]})
        db.add(issuer)
        db.flush()
        principal = OAuthPrincipal(id=uid(), issuer_id=issuer.id, subject="synthetic", client_id="audit")
        db.add(principal)
        db.flush()
        grant = OAuthGrant(id=uid(), principal_id=principal.id, project_id=c.p, created_by=c.owner.id, scopes=list(scopes))
        db.add(grant)
        db.flush()
        gid = grant.id
    timestamp = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(dict(iss="https://audit.invalid", aud="audit-resource", sub="synthetic", client_id="audit",
                            iat=timestamp, exp=timestamp + 600, scope=" ".join(scopes)), key,
                       algorithm="RS256", headers={"kid": "audit"})
    return c.s.authenticate(token, "token"), gid


def model_stub(c, monkeypatch, restricted_kind="source"):
    sent = []
    with c.s.db() as db:
        for art in db.scalars(select(Artifact)):
            art.metadata_json = dict(art.metadata_json, allow_platform_llm=True)
        evs = list(db.scalars(select(Evidence)))
        public = next(e for e in evs if db.get(Artifact, e.artifact_id).kind == "markdown")
        secret = next(e for e in evs if db.get(Artifact, e.artifact_id).kind == "source")
        art = db.get(Artifact, secret.artifact_id)
        art.kind = restricted_kind
        secret.excerpt = "SYNTHETIC_RESTRICTED_INPUT"
        restricted_id = art.id
        public_id = public.id
        db.add(ModelPolicy(id=uid(), project_id=c.p, settings=PolicyInput(
            enabled=True, endpoint="https://audit.invalid/v1", model_id="synthetic-audit", allow_source=True,
            max_input_tokens=100000).model_dump()))

    def reply(config, body):
        sent.append(body)
        answer = {"title": "Synthetic generated", "paragraphs": [
            {"markdown": "Synthetic output derived from input", "evidence_ids": [public_id]}], "unknowns": []}
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(answer)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50}}
    monkeypatch.setattr("vks_api.ai_gateway.request_model", reply)
    return sent, restricted_id


def generate(c, actor, key="generate"):
    job = c.s.create_analysis_job(actor, c.aid, {"kind": "generate_documents"}, key)
    c.s.process_job(job["id"])
    return c.s.get_job(c.owner, job["id"])


def generated(c):
    return [d for d in c.s.workspace(c.owner, c.p, c.aid)["documents"]
            if d["metadata"].get("source_kind") == "ai_generated"]


@pytest.mark.parametrize("kind", ["source", "trace"])
def test_a01_narrow_jwt_never_sends_restricted_context_even_after_grant_expansion_and_retry(audit_store, monkeypatch, kind):
    c = audit_store
    sent, _ = model_stub(c, monkeypatch, kind)
    actor, gid = oauth_actor(c, ["knowledge:read", "knowledge:edit"])
    job = c.s.create_analysis_job(actor, c.aid, {"kind": "generate_documents"}, "generate")
    with c.s.db() as db:
        grant = db.get(OAuthGrant, gid)
        grant.scopes = [*grant.scopes, "source:read", "trace:read"]
        j = db.get(Job, job["id"])
        j.state = "failed"
    c.s.retry_job(actor, job["id"])
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "succeeded"
    assert sent and "SYNTHETIC_RESTRICTED_INPUT" not in json.dumps(sent)


def test_a01_legacy_oauth_job_without_server_snapshot_fails_closed(audit_store, monkeypatch):
    c = audit_store
    sent, _ = model_stub(c, monkeypatch)
    actor, _ = oauth_actor(c, ["knowledge:read", "knowledge:edit"])
    job = c.s.create_analysis_job(actor, c.aid, {"kind": "generate_documents"}, "generate")
    with c.s.db() as db:
        db.get(Job, job["id"]).input_json = {"analysis_set_id": c.aid, "kind": "generate_documents"}
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "failed"
    assert not sent


@pytest.mark.parametrize("kind", ["source", "trace"])
def test_a02_human_output_inherits_all_sent_input_policies_not_only_citations(audit_store, monkeypatch, kind):
    c = audit_store
    sent, restricted = model_stub(c, monkeypatch, kind)
    result = generate(c, c.owner)
    assert result["state"] == "succeeded", result
    assert "SYNTHETIC_RESTRICTED_INPUT" in json.dumps(sent)
    doc = generated(c)[0]
    token = c.s.create_token(c.owner, c.p, {"name": "plain", "scopes": ["knowledge:read", "knowledge:edit"]})
    plain = c.s.authenticate(token["token"], "token")
    assert doc["entity_id"] not in {d["entity_id"] for d in c.s.workspace(plain, c.p, c.aid)["documents"]}
    with pytest.raises(DomainError):
        c.s.artifact_bytes(plain, c.p, doc["artifact_id"])
    docs = DocumentService(c.s)
    revision = docs.read_revision(c.owner, doc["entity_id"], doc["entity_revision_id"])
    payload = dict(analysis_set_id=c.aid, revision_id=revision["entity_revision_id"], reason="Synthetic review")
    submitted = docs.submit(c.owner, doc["entity_id"], payload, revision["row_version"])
    approved = docs.review(c.owner, doc["entity_id"], dict(payload, decision="approve"), submitted["row_version"])
    assert approved["state"] == "approved"
    assert doc["entity_id"] not in {d["entity_id"] for d in c.s.workspace(plain, c.p, c.aid)["documents"]}
    with pytest.raises(DomainError):
        c.s.artifact_bytes(plain, c.p, doc["artifact_id"])
    with c.s.db() as db:
        provenance = db.get(Artifact, doc["artifact_id"]).metadata_json["input_provenance"]
        assert restricted in {row["artifact_id"] for row in provenance}


def test_a02_unsent_source_does_not_taint_output(audit_store, monkeypatch):
    c = audit_store
    sent, _ = model_stub(c, monkeypatch)
    with c.s.db() as db:
        for art in db.scalars(select(Artifact).where(Artifact.kind == "source")):
            art.metadata_json = dict(art.metadata_json, allow_platform_llm=False)
    assert generate(c, c.owner)["state"] == "succeeded"
    assert "SYNTHETIC_RESTRICTED_INPUT" not in json.dumps(sent)
    with c.s.db() as db:
        assert not db.get(Artifact, generated(c)[0]["artifact_id"]).metadata_json.get("requires_source_read")


def test_a02_legacy_generated_artifact_without_input_provenance_is_fail_closed(audit_store, monkeypatch):
    c = audit_store
    model_stub(c, monkeypatch)
    assert generate(c, c.owner)["state"] == "succeeded"
    doc = generated(c)[0]
    with c.s.db() as db:
        artifact = db.get(Artifact, doc["artifact_id"])
        artifact.metadata_json = {k: v for k, v in artifact.metadata_json.items()
                                  if k not in ("input_provenance", "requires_source_read", "requires_trace_read")}
    token = c.s.create_token(c.owner, c.p, {"name": "plain legacy", "scopes": ["knowledge:read", "knowledge:edit"]})
    actor = c.s.authenticate(token["token"], "token")
    assert doc["entity_id"] not in {d["entity_id"] for d in c.s.workspace(actor, c.p, c.aid)["documents"]}
    with pytest.raises(DomainError):
        c.s.artifact_bytes(actor, c.p, doc["artifact_id"])


def test_a02_input_classification_changed_before_commit_discards_result(audit_store, monkeypatch):
    c = audit_store
    sent, _ = model_stub(c, monkeypatch)
    original = AiService.prepare_document
    def prepare(ai, jid):
        result = original(ai, jid)
        evidence = json.loads(sent[-1]["input"][1]["content"])["evidence"]
        eid = evidence[0]["evidence_id"]
        with c.s.db() as db:
            artifact = db.get(Artifact, db.get(Evidence, eid).artifact_id)
            artifact.metadata_json = dict(artifact.metadata_json, requires_trace_read=True)
        return result
    monkeypatch.setattr(AiService, "prepare_document", prepare)
    assert generate(c, c.owner)["state"] == "failed"
    assert not generated(c)


def test_a01_reserved_snapshot_cannot_be_replaced_by_client_payload(audit_store):
    c = audit_store
    actor, _ = oauth_actor(c, ["knowledge:read", "knowledge:edit"])
    with c.s.db() as db:
        job = c.s._new_job(db, actor, c.p, "generate_documents", {"analysis_set_id": c.aid,
            "_authorization": {"scopes": ["source:read", "trace:read"]}})
        snapshot = job.input_json["_authorization"]
        assert not {"source:read", "trace:read"} & set(snapshot["scopes"])
        assert snapshot["actor_id"] == actor.id and snapshot["project_id"] == c.p
        assert "token" not in snapshot


@pytest.mark.parametrize("stage", ["queued", "before_commit"])
def test_a01_revoke_discards_job_and_late_result(audit_store, monkeypatch, stage):
    c = audit_store
    sent, _ = model_stub(c, monkeypatch)
    actor, gid = oauth_actor(c, ["knowledge:read", "knowledge:edit", "source:read", "trace:read"])
    job = c.s.create_analysis_job(actor, c.aid, {"kind": "generate_documents"}, "generate")
    def revoke():
        with c.s.db() as db:
            db.get(OAuthGrant, gid).active = False
    if stage == "queued":
        revoke()
    else:
        original = AiService.prepare_document
        def prepare(ai, jid):
            result = original(ai, jid)
            revoke()
            return result
        monkeypatch.setattr(AiService, "prepare_document", prepare)
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "failed"
    assert not generated(c)
    assert bool(sent) == (stage == "before_commit")


def replacement(c, document, markdown):
    with c.s.db() as db:
        art = db.get(Artifact, document["artifact_id"])
        source = db.get(SourceSnapshot, art.snapshot_id)
        raw = bundle_bytes(Snapshot("local", uid(), {art.relative_path: markdown.encode()}),
                           "charge-demo", "replacement", source_key=source.source_key, role="documentation")
    return c.s._parse(raw, "charge-demo")


def test_a03_reimport_same_document_cannot_bypass_human_section_lock(audit_store):
    c = audit_store
    doc = c.w["documents"][0]
    docs = DocumentService(c.s)
    old = docs.read_revision(c.owner, doc["entity_id"], doc["entity_revision_id"])
    lock = old["sections"][1]["entity_id"]
    saved = docs.save_draft(c.owner, doc["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=old["entity_revision_id"], markdown=old["markdown"], locked_section_ids=[lock]), old["row_version"])
    machine_token = c.s.create_token(c.owner, c.p, {"name": "machine", "scopes": ["knowledge:read", "knowledge:edit", "source:import"]})
    machine = c.s.authenticate(machine_token["token"], "token")
    changed = saved["markdown"].replace(old["sections"][1]["name"], "Changed protected heading")
    with pytest.raises(DomainError) as direct:
        docs.save_draft(machine, doc["entity_id"], dict(analysis_set_id=c.aid,
            base_revision_id=saved["entity_revision_id"], markdown=changed, locked_section_ids=[lock]), saved["row_version"])
    assert direct.value.status == 409
    parsed = replacement(c, doc, changed)
    with pytest.raises(DomainError) as imported:
        with c.s.db() as db:
            c.s._commit_bundle(db, c.p, machine.id, parsed, c.aid)
    assert imported.value.status == 409
    assert c.s.workspace(c.owner, c.p, c.aid)["documents"][0]["entity_revision_id"] == saved["entity_revision_id"]


def test_a03_reimport_governed_document_requires_new_approval(audit_store):
    c = audit_store
    doc = c.w["documents"][0]
    docs = DocumentService(c.s)
    saved = docs.save_draft(c.owner, doc["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=doc["entity_revision_id"], markdown=doc["content"] + "\nHuman edit\n", locked_section_ids=[]), 1)
    parsed = replacement(c, doc, saved["markdown"] + "\nReimported addition\n")
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, parsed, c.aid)
        assert docs.assert_publishable(db, db.get(Analysis, c.aid))


def test_a04_reimport_removes_only_missing_chapters_in_clone_and_preserves_history(audit_store):
    c = audit_store
    other_bundle = c.s._parse(bundle_bytes(Snapshot("local", "other", {"docs/other.md": b"# Other\nUnrelated chapter\n"}),
                                          "charge-demo", "other", source_key="other-doc", role="documentation"), "charge-demo")
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, other_bundle, c.aid)
    c.w = c.s.workspace(c.owner, c.p, c.aid)
    other = next(d for d in c.w["documents"] if d["name"] == "Other")
    for rel in c.w["relations"]:
        c.s.review_relation(c.owner, rel["id"], dict(decision="confirm", reason="synthetic audit", evidence_ids=rel["evidence_ids"]), rel["row_version"])
    candidate = c.s.request_release(c.owner, c.p, {"analysis_set_id": c.aid}, "release")
    ticket = c.s.confirm_release(c.owner, candidate["candidate_id"])
    release = c.s.publish(c.owner, candidate["candidate_id"], dict(ticket, change_note="synthetic"), "publish")
    clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "Replacement", "clone")
    doc = next(d for d in c.w["documents"] if d["name"] != "Other")
    old_sections = [e for e in c.w["entities"] if e["type"] == "section"]
    parsed = replacement(c, doc, "# Reduced\nOnly this chapter remains.\n")
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, parsed, clone["id"])
    now = c.s.workspace(c.owner, c.p, clone["id"])
    assert len([e for e in now["entities"] if e["type"] == "section"]) == 2
    assert next(d for d in now["documents"] if d["entity_id"] == other["entity_id"])["entity_revision_id"] == other["entity_revision_id"]
    diff = c.s.diff(c.owner, c.p, release["id"], clone["id"])
    assert any(row["change_kind"] == "removed" for row in diff["changes"])
    assert not c.s.search(c.owner, c.p, {"query": "查询缴费记录", "analysis_set_id": clone["id"], "kinds": ["section"]})
    assert len([e for e in c.s.workspace(c.owner, c.p, release_id=release["id"])["entities"] if e["type"] == "section"]) == len(old_sections)
    with c.s.db() as db:
        relations = list(db.scalars(select(Relation).where(Relation.analysis_set_id == clone["id"])))
        assert any(r.validity_status == "broken" for r in relations)


def test_a03_commonmark_reimport_retains_unchanged_locked_setext_chapter(audit_store):
    c = audit_store
    doc = c.w["documents"][0]
    docs = DocumentService(c.s)
    markdown = "Root\n====\nRoot body\n\nChild\n-----\nProtected body\n"
    first = docs.save_draft(c.owner, doc["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=doc["entity_revision_id"], markdown=markdown, locked_section_ids=[]), 1)
    lock = next(row["entity_id"] for row in first["sections"] if row["name"] == "Child")
    second = docs.save_draft(c.owner, doc["entity_id"], dict(analysis_set_id=c.aid,
        base_revision_id=first["entity_revision_id"], markdown=markdown, locked_section_ids=[lock]), first["row_version"])
    parsed = replacement(c, doc, markdown)
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, parsed, c.aid)
    current = next(d for d in c.s.workspace(c.owner, c.p, c.aid)["documents"] if d["entity_id"] == doc["entity_id"])
    read = docs.read_revision(c.owner, doc["entity_id"], current["entity_revision_id"])
    assert lock in {row["entity_id"] for row in read["sections"]}


def test_a03_renamed_body_and_reused_heading_keep_distinct_immutable_identities(audit_store):
    c = audit_store
    parsed = c.s._parse(bundle_bytes(Snapshot("local", "initial-heading", {"docs/reused.md": b"# A\nOriginal body\n"}),
        "charge-demo", "initial-heading", source_key="heading-reuse", role="documentation"), "charge-demo")
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, parsed, c.aid)
    doc = next(d for d in c.s.workspace(c.owner, c.p, c.aid)["documents"] if d["name"] == "A")
    docs = DocumentService(c.s)
    old = docs.read_revision(c.owner, doc["entity_id"], doc["entity_revision_id"])["sections"][0]
    replacement_bundle = replacement(c, doc, "# C\nOriginal body\n# A\nNew body\n")
    identities = None
    for _ in range(2):
        with c.s.db() as db:
            c.s._commit_bundle(db, c.p, c.owner.id, replacement_bundle, c.aid)
        workspace = c.s.workspace(c.owner, c.p, c.aid)
        current = next(d for d in workspace["documents"] if d["entity_id"] == doc["entity_id"])
        chapters = docs.read_revision(c.owner, doc["entity_id"], current["entity_revision_id"])["sections"]
        assert len(chapters) == 2
        selected = {row["name"]: row["entity_id"] for row in chapters}
        assert selected["C"] == old["entity_id"] and selected["A"] != old["entity_id"]
        current_ids = {e["entity_id"] for e in workspace["entities"] if e["type"] == "section"
                       and e["metadata"].get("document_key") == doc["stable_key"]}
        assert current_ids == set(selected.values()) and len(current_ids) == 2
        assert identities is None or identities == selected
        identities = selected
    with c.s.db() as db:
        assert db.get(Revision, old["entity_revision_id"]).content == "# A\nOriginal body"
