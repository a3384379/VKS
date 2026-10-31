import copy

import pytest
from sqlalchemy import select
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Entity, Evidence, Relation, Release, Revision, uid
from vks_api.service import DomainError, Service
from vks_api.versions import VersionService


@pytest.fixture
def ctx(tmp_path, request):
    service = Service(tmp_path / "store")
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Versions"}, "project")
    job = service.demo_import(actor, project["id"], "import")
    service.process_job(job["id"])
    workspace = service.workspace(actor, project["id"])
    rejected_id = None
    if getattr(request, "param", "") == "rejected":
        with service.db() as db:
            rejected_id = next(
                row["id"]
                for row in workspace["relations"]
                if db.get(Entity, db.get(Revision, row["to_revision_id"]).entity_id).type == "section"
            )
    for relation in workspace["relations"]:
        service.set_relation_state(
            actor, relation["id"], {"excluded": relation["id"] == rejected_id}, relation["row_version"]
        )
    candidate = service.save_snapshot(
        actor, project["id"], {"analysis_set_id": workspace["selected_analysis_set"]["id"]}, "candidate"
    )
    pass
    ticket = None
    release = candidate
    clone = KnowledgeService(service).clone(
        actor, project["id"], workspace["selected_analysis_set"]["id"], "V2", "clone"
    )
    yield service, actor, project, workspace, release, clone, VersionService(service)
    service.stop_worker()
    service.engine.dispose()


def edit(ctx, markdown=None):
    service, actor, _, workspace, _, clone, _ = ctx
    document = workspace["documents"][0]
    docs = DocumentService(service)
    old = docs.read_revision(actor, document["entity_id"], document["entity_revision_id"])
    result = docs.save_draft(
        actor,
        document["entity_id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": old["entity_revision_id"],
            "markdown": markdown
            if markdown is not None
            else old["markdown"].replace("## 查询缴费记录", "## 查询与筛选"),
            "locked_section_ids": [],
        },
        1,
    )
    return result


def detect(ctx):
    service, actor, project, _, release, clone, versions = ctx
    with service.db() as db:
        return versions.detect_drift(db, project["id"], release["id"], clone["id"], actor.id)


def migrated(ctx):
    service, _, _, _, _, clone, _ = ctx
    with service.db() as db:
        return [
            service.relation_dto(row)
            for row in db.scalars(select(Relation).where(Relation.analysis_set_id == clone["id"]))
            if (row.metadata_json or {}).get("drift_inputs")
        ]


def test_drift_preserves_user_choices_history_and_is_idempotent(ctx):
    service, actor, project, workspace, release, clone, versions = ctx
    with service.db() as db:
        historical = copy.deepcopy(db.get(Release, release["id"]).manifest_json)
        decisions = {
            r.id: (r.excluded, r.manual_lock, r.row_version)
            for r in db.scalars(
                select(Relation).where(Relation.analysis_set_id == workspace["selected_analysis_set"]["id"])
            )
        }
    edit(ctx)
    report = detect(ctx)
    assert any(c["change_kind"] == "renamed" for c in report["changes"])
    assert report["impact_paths"]
    rows = migrated(ctx)
    assert rows and all(row["validity_status"] == "stale" for row in rows)
    assert not any(row["excluded"] for row in rows)
    with service.db() as db:
        assert db.get(Release, release["id"]).manifest_json == historical
        assert all(
            (db.get(Relation, rid).excluded, db.get(Relation, rid).manual_lock, db.get(Relation, rid).row_version)
            == state
            for rid, state in decisions.items()
        )
    again = detect(ctx)
    assert again["to_selection_version"] == report["to_selection_version"]
    assert migrated(ctx) == rows


def test_missing_section_keeps_broken_history_and_exclusion_is_separate(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx, "# Replacement\nNo matching original chapters.\n")
    detect(ctx)
    row = next(row for row in migrated(ctx) if row["validity_status"] == "broken")
    with pytest.raises(DomainError) as exc:
        versions.revalidate(
            actor,
            row["id"],
            {"analysis_set_id": clone["id"], "evidence_ids": row["evidence_ids"], "reason": "missing endpoint"},
            row["row_version"],
        )
    assert exc.value.status == 422
    result = service.set_relation_state(actor, row["id"], {"excluded": True}, row["row_version"])["relation"]
    assert result["excluded"] and result["validity_status"] == "broken"


def test_revalidation_requires_both_current_sources_and_preserves_causality(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx)
    detect(ctx)
    relation = migrated(ctx)[0]
    with service.db() as db:
        artifacts = {db.get(Revision, relation[k]).artifact_id for k in ("from_revision_id", "to_revision_id")}
        selected_evidence = []
        for artifact_id in artifacts:
            artifact = db.get(Artifact, artifact_id)
            evidence = Evidence(
                id=uid(),
                project_id=project["id"],
                analysis_set_id=clone["id"],
                artifact_id=artifact.id,
                stable_key="revalidate." + uid(),
                evidence_kind="manual",
                content_sha256=artifact.sha256,
                locator={"relative_path": artifact.relative_path, "line_start": 1, "line_end": 1},
                excerpt="Current source",
                claim="Current endpoint",
            )
            db.add(evidence)
            selected_evidence.append(evidence.id)
    with pytest.raises(DomainError) as exc:
        versions.revalidate(
            actor,
            relation["id"],
            {"analysis_set_id": clone["id"], "evidence_ids": [], "reason": "No coverage"},
            relation["row_version"],
        )
    assert exc.value.status == 422
    revalidated = versions.revalidate(
        actor,
        relation["id"],
        {"analysis_set_id": clone["id"], "evidence_ids": selected_evidence, "reason": "Both current endpoints"},
        relation["row_version"],
    )
    assert revalidated["excluded"] == relation["excluded"]
    assert revalidated["validity_status"] == "valid"
    assert revalidated["causal_basis"] == relation["causal_basis"] != "correlated_runtime"
    with pytest.raises(DomainError) as exc:
        versions.revalidate(
            actor,
            relation["id"],
            {"analysis_set_id": clone["id"], "evidence_ids": selected_evidence, "reason": "Stale browser"},
            relation["row_version"],
        )
    assert exc.value.status == 412
    detect(ctx)
    assert next(row for row in migrated(ctx) if row["id"] == relation["id"])["validity_status"] == "valid"


def test_public_reports_hide_restricted_revisions_and_paths(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx)
    report = detect(ctx)
    with service.db() as db:
        artifact = next(
            r.artifact_id for r in service._revisions(db, clone["id"]) if db.get(Entity, r.entity_id).type == "document"
        )
        db.get(Artifact, artifact).sensitivity = "confidential"
    token = service.create_token(
        actor, project["id"], {"name": "limited", "scopes": ["knowledge:read", "knowledge:edit"]}
    )
    limited = service.authenticate(token["token"], "token")
    visible = versions.public_report(limited, project["id"], report)
    assert not visible["changes"] and not visible["impact_paths"]
    assert "proposal_ids" not in visible


@pytest.mark.parametrize("ctx", ["rejected"], indirect=True)
def test_excluded_links_are_not_restored_by_drift(ctx):
    service, actor, project, workspace, release, clone, versions = ctx
    edit(ctx)
    detect(ctx)
    excluded = [row for row in migrated(ctx) if row["excluded"]]
    assert excluded
    before = copy.deepcopy(excluded)
    detect(ctx)
    assert [row for row in migrated(ctx) if row["excluded"]] == before


def test_two_source_updates_preserve_a_user_exclusion_on_the_same_logical_link(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx)
    detect(ctx)
    candidates = migrated(ctx)
    with service.db() as db:
        row = next(
            row
            for row in candidates
            if db.get(Entity, db.get(Revision, row["to_revision_id"]).entity_id).type == "section"
        )
        source_entity_id = db.get(Revision, row["from_revision_id"]).entity_id
        target_entity_id = db.get(Revision, row["to_revision_id"]).entity_id
    service.set_relation_state(actor, row["id"], {"excluded": True}, row["row_version"])
    document = service.workspace(actor, project["id"], clone["id"])["documents"][0]
    docs = DocumentService(service)
    current = docs.read_revision(actor, document["entity_id"], document["entity_revision_id"])
    section = next(item for item in current["sections"] if item["entity_id"] == target_entity_id)
    assert section["content"] in current["markdown"]
    docs.save_draft(
        actor,
        document["entity_id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": current["entity_revision_id"],
            "markdown": current["markdown"].replace(
                section["content"], section["content"] + "\nSecond source update.\n", 1
            ),
            "locked_section_ids": [],
        },
        current["row_version"],
    )
    detect(ctx)
    with service.db() as db:
        rows = [
            relation
            for relation in db.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == clone["id"], Relation.relation_type == row["relation_type"]
                )
            )
            if db.get(Revision, relation.from_revision_id).entity_id == source_entity_id
            and db.get(Revision, relation.to_revision_id).entity_id == target_entity_id
        ]
        assert rows and all(relation.excluded for relation in rows), (
            "A later source revision restored a user-excluded logical link"
        )
        selected = {revision.entity_id: revision.id for revision in service._revisions(db, clone["id"])}
        assert db.get(Relation, row["id"]).to_revision_id == selected[target_entity_id]
