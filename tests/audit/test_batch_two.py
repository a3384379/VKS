from io import BytesIO
import json
import zipfile

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from packages.collector.snapshots import Snapshot, bundle_bytes
from vks_api.models import Artifact, Evidence, Relation
from vks_api.service import DomainError


@pytest.mark.parametrize(
    "review,validity,expected",
    [
        ("confirmed", "valid", True),
        ("candidate", "valid", True),
        ("rejected", "valid", False),
        ("confirmed", "stale", False),
        ("confirmed", "broken", False),
    ],
)
def test_a05_action_context_uses_only_trusted_current_relations(audit_store, review, validity, expected):
    c = audit_store
    apis = {e["entity_revision_id"] for e in c.w["entities"] if e["type"] == "api"}
    actions = {e["entity_revision_id"] for e in c.w["actions"]}
    edge = next(r for r in c.w["relations"] if r["from_revision_id"] in actions and r["to_revision_id"] in apis)
    action = next(a for a in c.w["actions"] if a["entity_revision_id"] == edge["from_revision_id"])
    with c.s.db() as db:
        for r in db.scalars(select(Relation)):
            r.excluded = True
        row = db.get(Relation, edge["id"])
        row.excluded, row.validity_status = review == "rejected", validity
    context = c.s.action_context(c.owner, c.p, action["entity_id"], c.aid)
    assert bool(context["api_endpoints"]) is expected


def test_a06_cross_source_same_path_is_explicit_conflict_but_prefixes_are_independent(audit_store):
    c = audit_store

    def parsed(source, path, content):
        return c.s._parse(
            bundle_bytes(
                Snapshot("local", source, {path: content}),
                "charge-demo",
                source,
                source_key=source,
                role="documentation",
            ),
            "charge-demo",
        )

    first = parsed("backend", "README.md", b"# Backend\nSynthetic backend docs\n")
    with c.s.db() as db:
        c.s._commit_bundle(db, c.p, c.owner.id, first, c.aid)
    second = parsed("frontend", "README.md", b"# Frontend\nSynthetic frontend docs\n")
    with pytest.raises(DomainError) as failure:
        with c.s.db() as db:
            c.s._commit_bundle(db, c.p, c.owner.id, second, c.aid)
    assert failure.value.status == 409 and "前缀" in failure.value.message
    for source in ("backend", "frontend"):
        bundle = parsed(source, source + "/README.md", ("# " + source + "\nSeparate docs\n").encode())
        with c.s.db() as db:
            c.s._commit_bundle(db, c.p, c.owner.id, bundle, c.aid)
    assert {"Backend", "backend", "frontend"} <= {d["name"] for d in c.s.workspace(c.owner, c.p, c.aid)["documents"]}


def test_a06_explicit_section_key_cannot_replace_another_documents_chapter(audit_store):
    c = audit_store
    bundle = c.s._parse(
        bundle_bytes(
            Snapshot("local", "new", {"frontend/README.md": b"# New chapter\nNew content\n"}),
            "charge-demo",
            "new",
            source_key="frontend",
            role="documentation",
        ),
        "charge-demo",
    )
    old = next(e for e in c.w["entities"] if e["type"] == "section")
    chapter = next(e for e in bundle.entities if e["type"] == "section")
    chapter["stable_key"] = old["stable_key"]
    with pytest.raises(DomainError) as conflict:
        with c.s.db() as db:
            c.s._commit_bundle(db, c.p, c.owner.id, bundle, c.aid)
    assert conflict.value.status == 409


def test_a07_relation_with_only_hidden_evidence_is_absent_in_every_export(audit_store, monkeypatch):
    c = audit_store
    edge = next(r for r in c.w["relations"] if r["relation_type"] == "documents")
    with c.s.db() as db:
        secret = next(e for e in db.scalars(select(Evidence)) if db.get(Artifact, e.artifact_id).kind == "source")
        row = db.get(Relation, edge["id"])
        row.evidence_ids = [secret.id]
    for r in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.set_relation_state(
            c.owner,
            r["id"],
            {
                "excluded": dict(decision="confirm", reason="Synthetic audit", evidence_ids=r["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            r["row_version"],
        )
    candidate = c.s.save_snapshot(c.owner, c.p, {"analysis_set_id": c.aid}, "release")
    ticket = None
    release = candidate
    token = c.s.create_token(c.owner, c.p, {"name": "reader", "scopes": ["knowledge:read", "knowledge:export"]})
    reader = c.s.authenticate(token["token"], "token")
    assert edge["id"] not in {r["id"] for r in c.s.workspace(reader, c.p, release_id=release["id"])["relations"]}
    import vks_api.exports as exports

    real_pdf = exports.pdf_report
    pdf_relations = []

    def pdf(*args):
        pdf_relations.extend(r["id"] for r in args[-1])
        return real_pdf(*args)

    monkeypatch.setattr(exports, "pdf_report", pdf)
    for format in ("markdown_bundle", "pdf", "vksbundle"):
        job = c.s.export(reader, c.p, {"release_id": release["id"], "format": format, "include_source": False}, format)
        c.s.process_job(job["id"])
        raw, _ = c.s.download_export(reader, c.p, job["id"])
        if format == "pdf":
            text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(raw)).pages)
        else:
            with zipfile.ZipFile(BytesIO(raw)) as z:
                text = "\n".join(z.read(name).decode() for name in z.namelist())
        assert edge["id"] not in text
        if format == "markdown_bundle":
            with zipfile.ZipFile(BytesIO(raw)) as z:
                manifest = json.loads(z.read("knowledge-manifest.json"))
            assert edge["id"] not in {r["id"] for r in manifest["relations"]}
    assert edge["id"] not in pdf_relations
