"""Current input classification follows derived wording across its lifecycle."""

from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import pytest
from sqlalchemy import select
from test_platform_design_alignment import model_input, plain_actor
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Artifact, Entity, Relation, Revision
from vks_api.reading import artifact_policy

pass
from vks_api.relationships import RelationshipService
from vks_api.service import DomainError, Service


def relation_job(c, monkeypatch):
    marker, aid, sent = model_input(c, monkeypatch)
    tasks = KnowledgeTaskService(c.s)
    job = tasks.create(
        c.owner,
        c.p,
        TaskInput(
            analysis_set_id=c.aid, task_kind="knowledge_plan", instruction="Propose relation", max_model_calls=1
        ).model_dump(),
        "remediation",
    )
    c.s.process_job(job["id"])
    waiting = tasks.get(c.owner, job["id"])
    c.s.process_job(job["id"])
    result = tasks.get(c.owner, job["id"])
    assert result["state"] == "succeeded", result
    assert len(sent) == 1
    relation = next((row for row in c.s.workspace(c.owner, c.p, c.aid)["relations"] if marker in row["reason"]))
    return (marker, aid, relation)


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_relation_current_permissions_follow_clone_correction_graph_review_and_candidates(
    audit_store, monkeypatch, restriction
):
    c = audit_store
    marker, aid, relation = relation_job(c, monkeypatch)
    reader = plain_actor(c)
    assert any((row["id"] == relation["id"] for row in c.s.workspace(reader, c.p, c.aid)["relations"]))
    full = replace(c.owner, scopes=c.owner.scopes - {"source:read", "trace:read"})
    clone = KnowledgeService(c.s).clone(c.owner, c.p, c.aid, "Provenance clone", "clone")
    with c.s.db() as s:
        artifact = s.get(Artifact, aid)
        if restriction == "trace":
            artifact.kind = "trace"
        else:
            artifact.sensitivity = "confidential"
    assert all((marker not in row["reason"] for row in c.s.workspace(reader, c.p, c.aid)["relations"]))
    assert all((marker not in row["reason"] for row in c.s.workspace(reader, c.p, clone["id"])["relations"]))
    assert all((marker not in row["reason"] for row in c.s.candidates(reader, c.aid)))
    pass
    graph = c.s.graph(
        reader, c.p, relation["from_revision_id"], analysis_set_id=c.aid, depth=3, max_nodes=200, extended=True
    )
    assert all((row["id"] != relation["id"] for row in graph["edges"]))
    with pytest.raises(DomainError):
        c.s.set_relation_state(
            full,
            relation["id"],
            {
                "excluded": dict(decision="confirm", reason="Cannot declassify", evidence_ids=relation["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            relation["row_version"],
        )
    with c.s.db() as s:
        other = next(
            (
                row
                for row in c.s._revisions(s, c.aid)
                if row.id not in (relation["from_revision_id"], relation["to_revision_id"])
                and s.get(Entity, row.entity_id).type == "action"
            )
        )
    corrected = RelationshipService(c.s).correct(
        c.owner,
        relation["id"],
        dict(
            analysis_set_id=c.aid,
            operation="retarget",
            reason="Human wording edited",
            targets=[dict(to_revision_id=other.id, evidence_ids=relation["evidence_ids"])],
            merge_relation_ids=[],
            expected_versions={},
        ),
        relation["row_version"],
    )
    changed = corrected["new_relations"][0]
    assert changed["reason"] == "Human wording edited"
    assert all((row["id"] != changed["id"] for row in c.s.workspace(reader, c.p, c.aid)["relations"]))
    assert "metadata_json" not in changed and "input_provenance" not in changed


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_generated_document_edit_and_artifact_download_keep_current_input_permissions(
    audit_store, monkeypatch, restriction
):
    c = audit_store
    marker, aid, _ = model_input(c, monkeypatch, document=True)
    job = c.s.create_analysis_job(c.owner, c.aid, dict(kind="generate_documents"), "derived-doc")
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "succeeded"
    doc = next((row for row in c.s.workspace(c.owner, c.p, c.aid)["documents"] if row["name"] == marker))
    docs = DocumentService(c.s)
    original = docs.read_revision(c.owner, doc["entity_id"], doc["entity_revision_id"])
    edited = docs.save_draft(
        c.owner,
        doc["entity_id"],
        dict(
            analysis_set_id=c.aid,
            base_revision_id=doc["entity_revision_id"],
            markdown=original["markdown"] + "\n## Human edit\nStill derived from all inputs.\n",
            locked_section_ids=[],
        ),
        1,
    )
    reader = plain_actor(c)
    assert marker in str(c.s.workspace(reader, c.p, c.aid)["documents"])
    with c.s.db() as s:
        artifact = s.get(Artifact, aid)
        if restriction == "trace":
            artifact.kind = "trace"
        else:
            artifact.sensitivity = "confidential"
        old_art = s.get(Revision, doc["entity_revision_id"]).artifact_id
        new_art = s.get(Revision, edited["entity_revision_id"]).artifact_id
    assert marker not in str(c.s.workspace(reader, c.p, c.aid)["documents"])
    for rid, artifact_id in ((doc["entity_revision_id"], old_art), (edited["entity_revision_id"], new_art)):
        with pytest.raises(DomainError):
            docs.read_revision(reader, doc["entity_id"], rid)
        with pytest.raises(DomainError):
            c.s.artifact_bytes(reader, c.p, artifact_id)


@pytest.mark.parametrize("damage", ["missing", "wrong_hash", "wrong_evidence", "cycle"])
def test_provenance_damaged_identity_or_cycle_fails_closed(audit_store, monkeypatch, damage):
    c = audit_store
    _, aid, relation = relation_job(c, monkeypatch)
    with c.s.db() as s:
        row = s.get(Relation, relation["id"])
        artifact = s.get(Artifact, row.metadata_json["model_provenance_artifact_ids"][0])
        metadata = deepcopy(artifact.metadata_json)
        if damage == "cycle":
            target = s.get(Artifact, aid)
            target.metadata_json = dict(target.metadata_json, original_artifact_id=artifact.id)
        else:
            entry = metadata["input_provenance"][0]
            entry[{"missing": "artifact_id", "wrong_hash": "sha256", "wrong_evidence": "evidence_id"}[damage]] = (
                "missing-or-forged"
            )
            artifact.metadata_json = metadata
        policy = artifact_policy(s, artifact)
        assert policy["requires_source_read"] and policy["requires_trace_read"]
    assert all((row["id"] != relation["id"] for row in c.s.workspace(plain_actor(c), c.p, c.aid)["relations"]))


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_export_rechecks_current_derived_relation_inputs_at_build_and_download(audit_store, monkeypatch, restriction):
    from io import BytesIO
    import zipfile

    c = audit_store
    marker, aid, relation = relation_job(c, monkeypatch)
    for row in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.set_relation_state(
            c.owner,
            row["id"],
            {
                "excluded": dict(
                    decision="confirm", reason="Checked synthetic fixture", evidence_ids=row["evidence_ids"]
                )["decision"]
                == "reject"
            },
            row["row_version"],
        )
    candidate = c.s.save_snapshot(c.owner, c.p, dict(analysis_set_id=c.aid), "release-export")
    pass
    ticket = None
    release = candidate
    token = c.s.create_token(
        c.owner, c.p, dict(name="Export limited", scopes=["knowledge:read", "knowledge:edit", "knowledge:export"])
    )
    actor = c.s.authenticate(token["token"], "token")
    payload = dict(release_id=release["id"], format="markdown_bundle", include_source=False)
    job = c.s.export(actor, c.p, payload, "public-export")
    c.s.process_job(job["id"])
    assert c.s.get_job(actor, job["id"])["state"] == "succeeded"
    raw, _ = c.s.download_export(actor, c.p, job["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        assert any((marker in archive.read(name).decode("utf-8", errors="ignore") for name in archive.namelist()))
    with c.s.db() as s:
        artifact = s.get(Artifact, aid)
        if restriction == "trace":
            artifact.kind = "trace"
        else:
            artifact.sensitivity = "confidential"
    with pytest.raises(DomainError, match="当前来源权限"):
        c.s.download_export(actor, c.p, job["id"])
    refreshed = c.s.export(actor, c.p, payload, "restricted-export")
    c.s.process_job(refreshed["id"])
    assert c.s.get_job(actor, refreshed["id"])["state"] == "succeeded"
    raw, _ = c.s.download_export(actor, c.p, refreshed["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        assert not any((marker in archive.read(name).decode("utf-8", errors="ignore") for name in archive.namelist()))


@pytest.mark.parametrize("upgrade", ["automatic_sqlite", "alembic"])
def test_legacy_relation_schema_upgrade_preserves_data_and_quarantines_unknown_model_sources(tmp_path, upgrade):
    directory = tmp_path / "legacy"
    service = Service(directory)
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, dict(key="charge-demo", name="Legacy"), "p")
    job = service.demo_import(owner, project["id"], "demo")
    service.process_job(job["id"])
    w = service.workspace(owner, project["id"])
    relation = w["relations"][0]
    with service.db() as s:
        s.get(Relation, relation["id"]).reason = "AI 语义候选：Legacy unknown model inputs"
        count = len(list(s.scalars(select(Relation))))
    with service.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE relation DROP COLUMN metadata_json")
    url = service.engine.url.render_as_string(hide_password=False)
    service.engine.dispose()
    if upgrade == "alembic":
        from alembic import command
        from alembic.config import Config

        config = Config(str(Path("alembic.ini").resolve()))
        config.set_main_option("sqlalchemy.url", url)
        command.stamp(config, "h71baa007807")
        command.upgrade(config, "head")
    rebuilt = Service(directory, database_url=url)
    try:
        _, actor = rebuilt.login(rebuilt.bootstrap_token)
        with rebuilt.db() as s:
            assert len(list(s.scalars(select(Relation)))) == count
            assert s.get(Relation, relation["id"]).metadata_json == {}
        public = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
        assert all(
            (
                row["id"] != relation["id"]
                for row in rebuilt.workspace(public, project["id"], w["selected_analysis_set"]["id"])["relations"]
            )
        )
        assert any(
            (
                row["id"] == relation["id"]
                for row in rebuilt.workspace(actor, project["id"], w["selected_analysis_set"]["id"])["relations"]
            )
        )
    finally:
        rebuilt.engine.dispose()


def test_reimport_bundle_omits_model_derived_reason_when_all_input_permissions_cannot_transfer(
    audit_store, monkeypatch, tmp_path
):
    import json
    from io import BytesIO
    import zipfile

    c = audit_store
    marker, _, _ = relation_job(c, monkeypatch)
    for row in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.set_relation_state(
            c.owner,
            row["id"],
            {
                "excluded": dict(decision="confirm", reason="Checked fixture", evidence_ids=row["evidence_ids"])[
                    "decision"
                ]
                == "reject"
            },
            row["row_version"],
        )
    candidate = c.s.save_snapshot(c.owner, c.p, dict(analysis_set_id=c.aid), "roundtrip-precheck")
    pass
    ticket = None
    release = candidate
    job = c.s.export(
        c.owner, c.p, dict(release_id=release["id"], format="vksbundle", include_source=True), "roundtrip-export"
    )
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "succeeded"
    raw, _ = c.s.download_export(c.owner, c.p, job["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        provenance = json.loads(archive.read("export/provenance.json"))
        assert provenance["exclusions"][0]["kind"] == "model_derived_relationships"
        if "export/bindings.json" in archive.namelist():
            bindings = json.loads(archive.read("export/bindings.json"))
            assert all((marker not in row["reason"] for row in bindings["bindings"]))
    target = Service(tmp_path / "reimport")
    try:
        _, actor = target.login(target.bootstrap_token)
        project = target.create_project(actor, dict(key="charge-demo", name="Reimport"), "p")
        parsed = target._parse(raw, "charge-demo")
        with target.db() as s:
            target._commit_bundle(s, project["id"], actor.id, parsed)
        assert all((marker not in row["reason"] for row in target.workspace(actor, project["id"])["relations"]))
    finally:
        target.engine.dispose()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_journey_idempotent_replay_keeps_exact_result_and_rechecks_current_sources(audit_store, operation):
    from vks_api.journeys import JourneyService

    c = audit_store
    actor = plain_actor(c)
    action, doc = (c.w["actions"][0], c.w["documents"][0])
    dsl = dict(
        schema_version="2.0",
        key="cached-journey",
        title="Source-backed cached title",
        entry="action",
        nodes=[
            dict(
                key="action",
                kind="action",
                title=action["name"],
                x=0,
                y=0,
                binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
            ),
            dict(key="end", kind="end", title="End", x=100, y=0),
        ],
        edges=[dict(key="next", source="action", target="end", label="")],
        semantics=dict(
            kind="evidence_navigation",
            basis="declared",
            source_refs=[dict(entity_id=doc["entity_id"], revision_id=doc["entity_revision_id"])],
        ),
    )
    payload = dict(analysis_set_id=c.aid, dsl=dsl)
    journeys = JourneyService(c.s)
    if operation == "create":
        invoke = lambda: journeys.create(actor, c.p, payload, "replay-cache")
    else:
        first = journeys.create(actor, c.p, payload, "initial")
        payload = deepcopy(payload)
        payload["dsl"]["title"] = "Edited cached title"
        invoke = lambda: journeys.update(actor, first["journey_id"], payload, first["revision_id"], "replay-cache")
    saved = invoke()
    version = c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"]
    assert invoke() == saved
    assert c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"] == version
    with c.s.db() as s:
        s.get(Artifact, s.get(Revision, doc["entity_revision_id"]).artifact_id).sensitivity = "confidential"
    with pytest.raises(DomainError):
        invoke()
    assert c.s.workspace(c.owner, c.p, c.aid)["selected_analysis_set"]["selection_version"] == version
