"""Independent 2.0 platform alignment checks on isolated synthetic audit data."""

import json
import pytest
from sqlalchemy import select
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Artifact, Entity, Evidence, Relation, uid
from vks_api.service import sha


def model_input(c, monkeypatch, *, restricted_kind="knowledge", document=False):
    marker = "SYNTHETIC_PLATFORM_DERIVED_CONTENT"
    raw = marker.encode()
    with c.s.db() as db:
        public = next((e for e in db.scalars(select(Evidence)) if db.get(Artifact, e.artifact_id).kind == "markdown"))
        action = next((row for row in c.s._revisions(db, c.aid) if db.get(Entity, row.entity_id).type == "action"))
        for art in db.scalars(select(Artifact)):
            art.metadata_json = dict(
                art.metadata_json, allow_platform_llm=art.id in {public.artifact_id, action.artifact_id}
            )
        public_id = public.id
        art = Artifact(
            id=uid(),
            project_id=c.p,
            snapshot_id=uid(),
            relative_path="proof/platform.txt",
            sha256=sha(raw),
            size_bytes=len(raw),
            kind=restricted_kind,
            media_type="text/plain",
            object_key=c.s._put_object(raw),
            metadata_json={"allow_platform_llm": True},
        )
        db.add(art)
        db.flush()
        evidence = Evidence(
            id=uid(),
            project_id=c.p,
            analysis_set_id=c.aid,
            artifact_id=art.id,
            stable_key="platform.input",
            evidence_kind="explicit",
            content_sha256=sha(raw),
            locator={"path": "proof/platform.txt"},
            excerpt=marker,
        )
        db.add(evidence)
        db.flush()
        relation = db.scalar(select(Relation).where(Relation.analysis_set_id == c.aid))
        relation.evidence_ids = [*relation.evidence_ids, evidence.id]
        art_id, evidence_id = (art.id, evidence.id)
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=c.p,
                settings=PolicyInput(
                    enabled=True,
                    endpoint="https://platform.invalid/v1",
                    model_id="synthetic-platform-audit",
                    allow_source=True,
                    max_input_tokens=100000,
                ).model_dump(),
            )
        )
    sent = []

    def reply(config, body):
        context = json.loads(body["input"][1]["content"])
        sent.append(context)
        assert any((row["evidence_id"] == evidence_id for row in context["evidence"]))
        if document:
            answer = dict(title=marker, paragraphs=[dict(markdown=marker, evidence_ids=[public_id])], unknowns=[])
        else:
            ids = [row["revision_id"] for row in context["entities"] if row["type"] == "action"][:2]
            assert len(ids) == 2
            answer = dict(
                title="Independent platform plan",
                unknowns=[],
                steps=[
                    dict(
                        id="derived_relation",
                        tool="propose_relation",
                        target_revision_ids=ids,
                        evidence_ids=[public_id],
                        relation_type="depends_on",
                        rationale=marker,
                        markdown=None,
                    )
                ],
            )
        return dict(
            status="completed",
            output=[dict(type="message", content=[dict(type="output_text", text=json.dumps(answer))])],
            usage=dict(input_tokens=100, output_tokens=50),
        )

    monkeypatch.setattr("vks_api.ai_gateway.request_model", reply)
    return (marker, art_id, sent)


def plain_actor(c):
    token = c.s.create_token(
        c.owner, c.p, dict(name="Platform plain reader", scopes=["knowledge:read", "knowledge:edit"])
    )
    return c.s.authenticate(token["token"], "token")


@pytest.mark.parametrize("kind", ["source", "trace"])
def test_model_relation_reason_inherits_uncited_input_permissions(audit_store, monkeypatch, kind):
    c = audit_store
    marker, _, sent = model_input(c, monkeypatch, restricted_kind=kind)
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(
        c.owner,
        c.p,
        TaskInput(
            analysis_set_id=c.aid, task_kind="knowledge_plan", instruction="Propose a relation", max_model_calls=1
        ).model_dump(),
        "platform-relation",
    )
    c.s.process_job(job["id"])
    waiting = tasks.get(c.owner, job["id"])
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "succeeded", result
    assert len(sent) == 1
    reader = plain_actor(c)
    assert tasks.get(reader, job["id"])["report"] is None
    assert marker not in json.dumps(c.s.workspace(reader, c.p, c.aid)["relations"])


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_generated_document_rechecks_current_uncited_input_classification(audit_store, monkeypatch, restriction):
    c = audit_store
    marker, art_id, sent = model_input(c, monkeypatch, document=True)
    job = c.s.create_analysis_job(c.owner, c.aid, dict(kind="generate_documents"), "platform-document")
    c.s.process_job(job["id"])
    saved = c.s.get_job(c.owner, job["id"])
    assert saved["state"] == "succeeded", (saved.get("error"), sent)
    assert len(sent) == 1
    reader = plain_actor(c)
    assert marker in json.dumps(c.s.workspace(reader, c.p, c.aid)["documents"])
    with c.s.db() as db:
        art = db.get(Artifact, art_id)
        if restriction == "trace":
            art.kind = "trace"
        else:
            art.sensitivity = "confidential"
    assert marker not in json.dumps(c.s.workspace(reader, c.p, c.aid)["documents"])
