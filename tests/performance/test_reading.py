from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, select
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Analysis, Artifact, Entity, Job, Revision, Selection
from vks_api.reading import ReadingService
from vks_api.service import DomainError, Service


@pytest.fixture
def ctx(tmp_path):
    service = Service(tmp_path / "store")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "Read behavior"}, "project")
    job = service.demo_import(owner, project["id"], "import")
    service.process_job(job["id"])
    workspace = service.workspace(owner, project["id"])
    yield service, owner, project, workspace, job
    service.stop_worker()
    service.engine.dispose()


def publish(ctx):
    service, owner, project, workspace, _ = ctx
    for relation in workspace["relations"]:
        service.review_relation(
            owner,
            relation["id"],
            {"decision": "confirm", "reason": "Checked fixture", "evidence_ids": relation["evidence_ids"]},
            relation["row_version"],
        )
    candidate = service.request_release(
        owner, project["id"], {"analysis_set_id": workspace["selected_analysis_set"]["id"]}, "candidate"
    )
    ticket = service.confirm_release(owner, candidate["candidate_id"])
    return service.publish(
        owner,
        candidate["candidate_id"],
        {k: ticket[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")}
        | {"change_note": "Read fixture"},
        "publish",
    )


def test_context_graph_and_search_do_not_load_whole_workspace(ctx, monkeypatch):
    service, owner, project, workspace, _ = ctx
    monkeypatch.setattr(
        service,
        "workspace",
        lambda *args, **kwargs: pytest.fail("Local reads must not materialize the whole workspace"),
    )
    queries = []
    event.listen(service.engine, "before_cursor_execute", lambda *args: queries.append(args[2]))
    aid = workspace["selected_analysis_set"]["id"]
    action = workspace["actions"][0]["entity_id"]
    context = service.action_context(owner, project["id"], action, analysis_set_id=aid, max_nodes=5)
    assert len(context["entities"]) <= 5
    assert len(queries) < 40
    graph = service.graph(owner, project["id"], action, analysis_set_id=aid, max_nodes=1)
    assert len(graph["nodes"]) == 1 and graph["truncated"]
    assert service.search(owner, project["id"], {"query": "开票", "analysis_set_id": aid})


def test_v2_paths_are_closed_fixed_and_recheck_current_evidence_policies(ctx, monkeypatch):
    from vks_api.workbench_v2 import explanation

    service, owner, project, workspace, _ = ctx
    release = publish(ctx)
    action = workspace["actions"][0]["entity_id"]
    monkeypatch.setattr(service, "workspace", lambda *a, **k: pytest.fail("V2 explanations must stay bounded"))
    before = explanation(service, owner, project["id"], action, release_id=release["id"])
    assert before["release_id"] == release["id"] and before["paths"]
    nodes = {row["entity_revision_id"] for row in before["entities"]}
    relations = {row["id"]: row for row in before["relations"]}
    evidence = {row["id"] for row in before["evidence"]}
    assert nodes == {before["action_revision_id"]} | {rid for path in before["paths"] for rid in path["revision_ids"]}
    assert set(relations) == {rid for path in before["paths"] for rid in path["relation_ids"]}
    for path in before["paths"]:
        assert 1 <= len(path["relation_ids"]) <= 3
        assert set(path["evidence_ids"]) <= evidence
        for i, rid in enumerate(path["relation_ids"]):
            edge = relations[rid]
            assert edge["review_status"] == "confirmed" and edge["validity_status"] == "valid"
            assert set(path["revision_ids"][i : i + 2]) == {edge["from_revision_id"], edge["to_revision_id"]}
    token = service.create_token(
        owner, project["id"], {"name": "V2 reader", "scopes": ["knowledge:read"], "expires_in_days": 1}
    )
    limited = service.authenticate(token["token"], "token")
    public_before = explanation(service, limited, project["id"], action, release_id=release["id"])
    doc = workspace["documents"][0]
    with service.db() as db:
        db.get(Artifact, doc["artifact_id"]).sensitivity = "confidential"
    after = explanation(service, limited, project["id"], action, release_id=release["id"])
    hidden = {row["entity_revision_id"] for row in public_before["entities"] if row["type"] in ("document", "section")}
    assert hidden and not hidden & {row["entity_revision_id"] for row in after["entities"]}
    assert explanation(service, owner, project["id"], action, release_id=release["id"]) == before
    service.revoke_token(owner, project["id"], token["id"])
    with pytest.raises(DomainError) as exc:
        explanation(service, limited, project["id"], action, release_id=release["id"])
    assert exc.value.status == 401


def test_fixed_release_cache_never_caches_grants_or_new_artifact_policies(ctx):
    service, owner, project, workspace, _ = ctx
    release = publish(ctx)
    token = service.create_token(
        owner, project["id"], {"name": "limited", "scopes": ["knowledge:read"], "expires_in_days": 1}
    )
    limited = service.authenticate(token["token"], "token")
    action = workspace["actions"][0]["entity_id"]
    first = service.action_context(limited, project["id"], action, release_id=release["id"])
    assert service._read_cache_metrics["misses"] == 1
    doc = workspace["documents"][0]
    with service.db() as db:
        db.get(Artifact, doc["artifact_id"]).sensitivity = "confidential"
    second = service.action_context(limited, project["id"], action, release_id=release["id"])
    hidden_ids = {e["entity_revision_id"] for e in first["entities"] if e["type"] in ("document", "section")}
    assert hidden_ids
    assert not hidden_ids & {e["entity_revision_id"] for e in second["entities"]}
    assert service._read_cache_metrics["hits"] >= 1
    service.revoke_token(owner, project["id"], token["id"])
    with pytest.raises(DomainError) as exc:
        service.action_context(limited, project["id"], action, release_id=release["id"])
    assert exc.value.status == 401


def test_old_release_remains_fixed_after_clone_and_chapter_edit(ctx):
    service, owner, project, workspace, _ = ctx
    release = publish(ctx)
    action = workspace["actions"][0]["entity_id"]
    before = service.action_context(owner, project["id"], action, release_id=release["id"])
    clone = KnowledgeService(service).clone(
        owner, project["id"], workspace["selected_analysis_set"]["id"], "New draft", "clone"
    )
    doc = workspace["documents"][0]
    DocumentService(service).save_draft(
        owner,
        doc["entity_id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": doc["entity_revision_id"],
            "markdown": "# Replaced\nNew draft only\n",
            "locked_section_ids": [],
        },
        1,
    )
    after = service.action_context(owner, project["id"], action, release_id=release["id"])
    assert before == after


def test_read_transaction_has_consistent_snapshot_and_releases_writer(ctx):
    service, owner, project, workspace, _ = ctx
    aid = workspace["selected_analysis_set"]["id"]
    started = Event()

    def update():
        with service.db() as db:
            analysis = db.get(Analysis, aid)
            analysis.selection_version += 1
            db.flush()
            started.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with ReadingService(service).db() as db:
            service.require(db, owner, project["id"])
            version = db.scalar(select(Analysis.selection_version).where(Analysis.id == aid))
            future = pool.submit(update)
            assert started.wait(2)
            assert db.scalar(select(Analysis.selection_version).where(Analysis.id == aid)) == version
        future.result(timeout=5)
    with ReadingService(service).db() as db:
        assert db.scalar(select(Analysis.selection_version).where(Analysis.id == aid)) == version + 1


def test_private_job_diagnostics_and_counts_are_filtered_on_both_read_paths(ctx):
    service, owner, project, workspace, job = ctx
    with service.db() as db:
        row = db.get(Job, job["id"])
        row.result_json = dict(
            row.result_json,
            issues=[{"artifact_path": "backend/private/Secret.java", "message": "Private source gap"}],
            analysis_metrics={"private_files": 17},
            accepted_count=17,
            snapshot_ids=["private-snapshot"],
        )
        row.issue_count = 1
        row.error_json = {
            "code": "VKS_INVALID_INPUT",
            "message": "backend/private/Secret.java failed",
            "details": {"path": "backend/private/Secret.java"},
        }
    token = service.create_token(
        owner, project["id"], {"name": "editor", "scopes": ["knowledge:read", "knowledge:edit"], "expires_in_days": 1}
    )
    limited = service.authenticate(token["token"], "token")
    result = service.get_job(limited, job["id"])
    assert result["diagnostics_restricted"]
    assert not {"issues", "analysis_metrics", "accepted_count", "snapshot_ids"} & result.get("result", {}).keys()
    assert "issue_count" not in result and "Secret.java" not in str(result)
    public = service.workspace(limited, project["id"], workspace["selected_analysis_set"]["id"])
    assert "Secret.java" not in str(public["jobs"])


def test_search_preserves_unicode_casefold_metadata_and_literal_wildcards(ctx):
    service, owner, project, workspace, _ = ctx
    document = workspace["documents"][0]
    with service.db() as db:
        old = db.get(Revision, document["entity_revision_id"])
        entity = db.get(Entity, old.entity_id)
        artifact = db.get(Artifact, old.artifact_id)
        new = DocumentService(service)._revision(
            db,
            entity,
            artifact,
            "Straße special%word",
            "Unicode search",
            old.locator,
            dict(old.metadata_json, only_metadata="专属术语"),
        )
        db.get(Selection, (workspace["selected_analysis_set"]["id"], entity.id)).entity_revision_id = new.id
    for term in ("STRASSE", "专属术语", "special%word"):
        results = service.search(
            owner, project["id"], {"analysis_set_id": workspace["selected_analysis_set"]["id"], "query": term}
        )
        assert any(row["entity"]["entity_id"] == document["entity_id"] for row in results), term
    assert not service.search(
        owner, project["id"], {"query": "special_word", "analysis_set_id": workspace["selected_analysis_set"]["id"]}
    )


def test_limited_workspace_cannot_mutate_shared_release_fact_evidence(ctx):
    from vks_api.models import Evidence, Relation, uid

    service, owner, project, workspace, _ = ctx
    public_relation = next(
        row
        for row in workspace["relations"]
        if next(e["type"] for e in workspace["entities"] if e["entity_revision_id"] == row["from_revision_id"])
        == "action"
    )
    seed_entity = next(
        e["entity_id"] for e in workspace["entities"] if e["entity_revision_id"] == public_relation["from_revision_id"]
    )
    private_artifact = next(e["artifact_id"] for e in workspace["entities"] if e["type"] == "symbol")
    private_id = uid()
    with service.db() as db:
        artifact = db.get(Artifact, private_artifact)
        db.add(
            Evidence(
                id=private_id,
                project_id=project["id"],
                analysis_set_id=workspace["selected_analysis_set"]["id"],
                artifact_id=private_artifact,
                stable_key="private-provenance",
                evidence_kind="manual",
                content_sha256=artifact.sha256,
                locator={"relative_path": artifact.relative_path},
                excerpt="Private synthetic provenance",
                claim="Synthetic cache isolation test",
            )
        )
        relation = db.get(Relation, public_relation["id"])
        relation.evidence_ids = [*relation.evidence_ids, private_id]
    refreshed = service.workspace(owner, project["id"], workspace["selected_analysis_set"]["id"])
    release = publish((service, owner, project, refreshed, ctx[4]))
    before = service.graph(owner, project["id"], seed_entity, release_id=release["id"])
    assert private_id in next(edge["evidence_ids"] for edge in before["edges"] if edge["id"] == public_relation["id"])
    token = service.create_token(
        owner, project["id"], {"name": "limited workspace", "scopes": ["knowledge:read"], "expires_in_days": 1}
    )
    limited = service.authenticate(token["token"], "token")
    restricted = service.workspace(limited, project["id"], release_id=release["id"])
    assert all(private_id not in edge["evidence_ids"] for edge in restricted["relations"])
    after = service.graph(owner, project["id"], seed_entity, release_id=release["id"])
    assert before == after


def test_permission_cache_does_not_survive_reused_session_transaction(ctx):
    service, owner, project, _, _ = ctx
    token = service.create_token(
        owner,
        project["id"],
        {"name": "reused transaction", "scopes": ["knowledge:read", "source:read"], "expires_in_days": 1},
    )
    reader = service.authenticate(token["token"], "token")
    with service.Session() as session:
        session.info["vks_read_only"] = True
        with session.begin():
            assert service._can(session, reader, project["id"], "source:read")
        service.revoke_token(owner, project["id"], token["id"])
        with session.begin():
            assert not service._can(session, reader, project["id"], "source:read")
