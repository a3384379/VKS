from hashlib import sha256

import pytest
from sqlalchemy import select
from vks_api.documents import DocumentService, parse_sections
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Relation, Revision, Selection
from vks_api.service import DomainError, Service


@pytest.fixture
def ctx(tmp_path):
    service = Service(tmp_path / "store")
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Documents"}, "project")
    job = service.demo_import(actor, project["id"], "import")
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    workspace = service.workspace(actor, project["id"])
    yield service, actor, project, workspace, DocumentService(service)
    service.stop_worker()
    service.engine.dispose()


def current(ctx):
    service, actor, project, workspace, docs = ctx
    aid = workspace["selected_analysis_set"]["id"]
    document = service.workspace(actor, project["id"], aid)["documents"][0]
    return aid, document, docs.read_revision(actor, document["entity_id"], document["entity_revision_id"])


def machine(ctx):
    service, actor, project, _, _ = ctx
    token = service.create_token(
        actor, project["id"], {"name": "editor", "scopes": ["knowledge:read", "knowledge:edit"], "expires_in_days": 1}
    )
    return service.authenticate(token["token"], "token")


def save(ctx, markdown, locks=()):
    _, actor, _, _, docs = ctx
    aid, document, dto = current(ctx)
    return docs.save_draft(
        actor,
        document["entity_id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": dto["entity_revision_id"],
            "markdown": markdown,
            "locked_section_ids": list(locks),
        },
        dto["row_version"],
    )


def test_commonmark_sections_ignore_fences_and_support_setext():
    raw = "Title\n=====\nIntro\n\n```md\n# Not a heading\n```\n\n## Child\nbody\n"
    rows = parse_sections(raw, "fallback")
    assert [r["title"] for r in rows] == ["Title", "Child"]
    assert rows[0]["startLine"] == 1 and rows[0]["endLine"] == 8
    assert "# Not a heading" in rows[0]["content"]
    assert rows[1]["heading_path"] == ["Title", "Child"]


def test_edits_create_new_artifacts_exact_locators_and_reuse_renamed_chapter(ctx):
    service, actor, project, _, docs = ctx
    _aid, document, old = current(ctx)
    chapter = next(s for s in old["sections"] if s["name"] == "查询缴费记录")
    raw, _ = service.artifact_bytes(actor, project["id"], document["artifact_id"])
    markdown = old["markdown"].replace("## 查询缴费记录", "## 查询与筛选")
    saved = save(ctx, markdown)
    renamed = next(s for s in saved["sections"] if s["name"] == "查询与筛选")
    assert renamed["entity_id"] == chapter["entity_id"]
    assert renamed["revision_id"] != chapter["revision_id"]
    with service.db() as db:
        revision = db.get(Revision, saved["entity_revision_id"])
        artifact = db.get(Artifact, revision.artifact_id)
        assert artifact.id != document["artifact_id"]
        assert artifact.sha256 == sha256(markdown.encode()).hexdigest()
        assert renamed["artifact_id"] == artifact.id
        assert db.get(Revision, old["entity_revision_id"]).content == old["markdown"]
    assert service.artifact_bytes(actor, project["id"], document["artifact_id"])[0] == raw
    assert service.artifact_bytes(actor, project["id"], renamed["artifact_id"])[0] == markdown.encode()
    lines = markdown.splitlines()
    assert "\n".join(lines[renamed["locator"]["startLine"] - 1 : renamed["locator"]["endLine"]]) == renamed["content"]
    assert saved["original_artifact_id"] == document["artifact_id"]
    assert docs.read_revision(actor, document["entity_id"], old["entity_revision_id"])["markdown"] == old["markdown"]


def test_locks_protect_only_selected_chapters_and_machine_cannot_unlock(ctx):
    _, _actor, _, _, docs = ctx
    _, _, old = current(ctx)
    protected = next(s for s in old["sections"] if s["name"] == "查询缴费记录")
    locked = save(ctx, old["markdown"], [protected["entity_id"]])
    unrelated = locked["markdown"].replace("## 申请开票", "## 打开申请表单")
    saved = save(ctx, unrelated, [protected["entity_id"]])
    assert next(s for s in saved["sections"] if s["entity_id"] == protected["entity_id"])["metadata"]["manual_lock"]
    with pytest.raises(DomainError) as exc:
        save(ctx, unrelated.replace("## 查询缴费记录", "## 破坏锁定章节"), [protected["entity_id"]])
    assert exc.value.status == 409
    aid, document, now = current(ctx)
    with pytest.raises(DomainError) as exc:
        docs.save_draft(
            machine(ctx),
            document["entity_id"],
            {
                "analysis_set_id": aid,
                "base_revision_id": now["entity_revision_id"],
                "markdown": now["markdown"],
                "locked_section_ids": [],
            },
            now["row_version"],
        )
    assert exc.value.status == 403
    unlocked = save(ctx, now["markdown"], [])
    assert not any(s["metadata"]["manual_lock"] for s in unlocked["sections"])


def test_duplicate_titles_and_bodies_never_merge_identity(ctx):
    first = save(ctx, "# Doc\nroot\n\n## Same\nrepeated\n\n## Same\nrepeated\n")
    previous = {s["entity_id"] for s in first["sections"] if s["name"] == "Same"}
    assert len(previous) == 2
    second = save(ctx, first["markdown"])
    replacement = {s["entity_id"] for s in second["sections"] if s["name"] == "Same"}
    assert len(replacement) == 2 and not previous & replacement
    assert len(second["identity_ambiguities"]) == 2


def test_conflict_preserves_current_selection_and_immutable_revision(ctx):
    service, actor, _, _, docs = ctx
    aid, document, old = current(ctx)
    saved = save(ctx, old["markdown"] + "\nNew note\n")
    with pytest.raises(DomainError) as exc:
        docs.save_draft(
            actor,
            document["entity_id"],
            {
                "analysis_set_id": aid,
                "base_revision_id": old["entity_revision_id"],
                "markdown": "overwrite",
                "locked_section_ids": [],
            },
            old["row_version"],
        )
    assert exc.value.status == 412
    with service.db() as db:
        assert db.get(Selection, (aid, document["entity_id"])).entity_revision_id == saved["entity_revision_id"]


pass


def test_draft_retargets_only_its_relations_and_removed_sections_remain_broken(ctx):
    service, actor, project, workspace, docs = ctx
    original_id = workspace["selected_analysis_set"]["id"]
    clone = KnowledgeService(service).clone(actor, project["id"], original_id, "Editable", "clone")
    old = workspace["documents"][0]
    before = {r["id"]: (r["from_revision_id"], r["to_revision_id"]) for r in workspace["relations"]}
    docs.read_revision(actor, old["entity_id"], old["entity_revision_id"])
    saved = docs.save_draft(
        actor,
        old["entity_id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": old["entity_revision_id"],
            "markdown": "# Replaced\nAll original sections removed.\n",
            "locked_section_ids": [],
        },
        1,
    )
    assert len(saved["sections"]) == 1
    with service.db() as db:
        assert all(
            (db.get(Relation, rid).from_revision_id, db.get(Relation, rid).to_revision_id) == ends
            for rid, ends in before.items()
        )
        rows = list(db.scalars(select(Relation).where(Relation.analysis_set_id == clone["id"])))
        broken = [r for r in rows if r.validity_status == "broken"]
        assert broken
        selected = {r.id for r in service._revisions(db, clone["id"])}
        assert any(r.from_revision_id not in selected or r.to_revision_id not in selected for r in broken)


def test_derived_document_edits_preserve_source_trace_and_model_permissions(ctx):
    service, _actor, _project, _, docs = ctx
    _, document, old = current(ctx)
    with service.db() as db:
        artifact = db.get(Artifact, document["artifact_id"])
        artifact.metadata_json = dict(
            artifact.metadata_json, requires_source_read=True, requires_trace_read=True, allow_platform_llm=False
        )
    saved = save(ctx, old["markdown"] + "\nA derived edit\n")
    with service.db() as db:
        artifact = db.get(Artifact, db.get(Revision, saved["entity_revision_id"]).artifact_id)
        assert artifact.metadata_json["requires_source_read"] is True
        assert artifact.metadata_json["requires_trace_read"] is True
        assert artifact.metadata_json["allow_platform_llm"] is False
    with pytest.raises(DomainError) as exc:
        docs.read_revision(machine(ctx), document["entity_id"], saved["entity_revision_id"])
    assert exc.value.status == 404


def test_content_rollback_cannot_reuse_older_weaker_artifact_policy(ctx):
    service, _actor, _project, _, _ = ctx
    _, _, old = current(ctx)
    first = save(ctx, old["markdown"])
    with service.db() as db:
        artifact = db.get(Artifact, db.get(Revision, first["entity_revision_id"]).artifact_id)
        artifact.metadata_json = dict(artifact.metadata_json, allow_platform_llm=True)
    second = save(ctx, first["markdown"] + "\nClassified edit\n")
    with service.db() as db:
        artifact = db.get(Artifact, db.get(Revision, second["entity_revision_id"]).artifact_id)
        artifact.sensitivity = "confidential"
        artifact.metadata_json = dict(
            artifact.metadata_json, requires_source_read=True, requires_trace_read=True, allow_platform_llm=False
        )
    reverted = save(ctx, first["markdown"])
    with service.db() as db:
        artifact = db.get(Artifact, db.get(Revision, reverted["entity_revision_id"]).artifact_id)
        older = db.get(Revision, first["entity_revision_id"])
        assert artifact.id != older.artifact_id
        assert artifact.sensitivity == "confidential"
        assert artifact.metadata_json["requires_source_read"] is True
        assert artifact.metadata_json["requires_trace_read"] is True
        assert artifact.metadata_json["allow_platform_llm"] is False
