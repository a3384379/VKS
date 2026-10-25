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
        service.review_relation(
            actor,
            relation["id"],
            {
                "decision": "reject" if relation["id"] == rejected_id else "confirm",
                "reason": "Fixture inspected",
                "evidence_ids": relation["evidence_ids"],
            },
            relation["row_version"],
        )
    candidate = service.request_release(
        actor, project["id"], {"analysis_set_id": workspace["selected_analysis_set"]["id"]}, "candidate"
    )
    assert not candidate["blockers"], candidate
    ticket = service.confirm_release(actor, candidate["candidate_id"])
    release = service.publish(
        actor,
        candidate["candidate_id"],
        {
            "human_confirmation_ref": ticket["human_confirmation_ref"],
            "expected_manifest_sha256": ticket["expected_manifest_sha256"],
            "change_note": "Version baseline",
        },
        "publish",
    )
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


def test_drift_retains_review_locks_and_history_and_is_idempotent(ctx):
    service, actor, project, workspace, release, clone, versions = ctx
    with service.db() as db:
        historical = copy.deepcopy(db.get(Release, release["id"]).manifest_json)
        decisions = {
            r.id: (r.review_status, r.manual_lock, r.row_version)
            for r in db.scalars(
                select(Relation).where(Relation.analysis_set_id == workspace["selected_analysis_set"]["id"])
            )
        }
    edit(ctx)
    report = detect(ctx)
    assert any(c["change_kind"] == "renamed" for c in report["changes"])
    assert report["impact_paths"]
    proposals = versions.list_proposals(actor, project["id"], clone["id"])
    migrations = [p for p in proposals if p["kind"] == "relation_migration"]
    assert migrations and all(p["relation"]["review_status"] == "confirmed" for p in migrations)
    assert all(p["relation"]["validity_status"] == "stale" for p in migrations)
    with service.db() as db:
        assert all(db.get(Relation, p["target_relation_id"]).manual_lock for p in migrations)
        assert db.get(Release, release["id"]).manifest_json == historical
        assert all(
            (db.get(Relation, rid).review_status, db.get(Relation, rid).manual_lock, db.get(Relation, rid).row_version)
            == state
            for rid, state in decisions.items()
        )
    again = detect(ctx)
    assert set(again["proposal_ids"]) == set(report["proposal_ids"])
    assert len(versions.list_proposals(actor, project["id"], clone["id"])) == len(proposals)


def test_missing_section_stays_historical_broken_accept_does_not_validate(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx, "# Replacement\nNo matching original chapters.\n")
    detect(ctx)
    proposals = versions.list_proposals(actor, project["id"], clone["id"])
    proposal = next(
        p for p in proposals if p["kind"] == "relation_migration" and p["relation"]["validity_status"] == "broken"
    )
    with service.db() as db:
        relation = db.get(Relation, proposal["target_relation_id"])
        selected = {r.id for r in service._revisions(db, clone["id"])}
        assert relation.from_revision_id not in selected or relation.to_revision_id not in selected
    accepted = versions.decide(
        actor, proposal["id"], {"decision": "accept", "reason": "Review migration"}, proposal["row_version"]
    )
    assert accepted["status"] == "accepted" and accepted["relation"]["validity_status"] == "broken"
    with pytest.raises(DomainError) as exc:
        versions.revalidate(
            actor,
            proposal["target_relation_id"],
            {
                "analysis_set_id": clone["id"],
                "evidence_ids": proposal["relation"]["evidence_ids"],
                "reason": "Cannot infer missing endpoint",
            },
            accepted["relation"]["row_version"],
        )
    assert exc.value.status == 422
    other = next(p for p in proposals if p["kind"] == "relation_migration" and p["id"] != proposal["id"])
    rejected = versions.decide(
        actor,
        other["id"],
        {"decision": "reject", "reason": "Historical relation no longer applies"},
        other["row_version"],
    )
    assert rejected["relation"]["review_status"] == "rejected" and rejected["relation"]["validity_status"] == "obsolete"


def test_revalidation_requires_new_source_coverage_and_preserves_review_causality(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx)
    detect(ctx)
    migration = next(
        p for p in versions.list_proposals(actor, project["id"], clone["id"]) if p["kind"] == "relation_migration"
    )
    relation = migration["relation"]
    with service.db() as db:
        endpoints = [db.get(Revision, relation[k]) for k in ("from_revision_id", "to_revision_id")]
        artifacts = {r.artifact_id for r in endpoints}
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
                excerpt="New selected-source review",
                claim="Checked current endpoint version",
            )
            db.add(evidence)
            selected_evidence.append(evidence.id)
    with pytest.raises(DomainError) as exc:
        versions.revalidate(
            actor,
            relation["id"],
            {"analysis_set_id": clone["id"], "evidence_ids": selected_evidence[:1], "reason": "Incomplete coverage"},
            relation["row_version"],
        )
    assert exc.value.status == 422
    revalidated = versions.revalidate(
        actor,
        relation["id"],
        {
            "analysis_set_id": clone["id"],
            "evidence_ids": selected_evidence,
            "reason": "Both endpoint versions inspected",
        },
        relation["row_version"],
    )
    assert revalidated["review_status"] == relation["review_status"]
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


def test_public_reports_hide_restricted_new_revision_even_when_history_is_visible(ctx):
    service, actor, project, _, _, clone, versions = ctx
    edit(ctx)
    report = detect(ctx)
    migrations = [
        p for p in versions.list_proposals(actor, project["id"], clone["id"]) if p["kind"] == "relation_migration"
    ]
    with service.db() as db:
        db.get(Revision, migrations[0]["relation"]["to_revision_id"])
        document_artifact = next(
            r.artifact_id for r in service._revisions(db, clone["id"]) if db.get(Entity, r.entity_id).type == "document"
        )
        db.get(Artifact, document_artifact).sensitivity = "confidential"
    token = service.create_token(
        actor,
        project["id"],
        {"name": "restricted editor", "scopes": ["knowledge:read", "knowledge:edit"], "expires_in_days": 1},
    )
    limited = service.authenticate(token["token"], "token")
    visible = versions.public_report(limited, project["id"], report)
    assert not visible["changes"]
    assert not visible["impact_paths"]
    assert not visible["proposal_ids"]
    assert not versions.list_proposals(limited, project["id"], clone["id"])


@pytest.mark.parametrize("ctx", ["rejected"], indirect=True)
def test_human_rejection_and_lock_false_are_preserved_without_guessing(ctx):
    service, actor, project, workspace, _release, clone, versions = ctx
    with service.db() as db:
        target = next(
            r.id
            for r in db.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == workspace["selected_analysis_set"]["id"],
                    Relation.review_status == "rejected",
                )
            )
        )
    edit(ctx)
    detect(ctx)
    migrated = next(
        p for p in versions.list_proposals(actor, project["id"], clone["id"]) if p["source_relation_id"] == target
    )
    assert migrated["relation"]["review_status"] == "rejected"
    with service.db() as db:
        assert not db.get(Relation, migrated["target_relation_id"]).manual_lock
