import pytest
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Revision, Selection
from vks_api.modules import BusinessModule, ModuleEntity, ModuleService, ModuleSource
from vks_api.service import DomainError, Service


@pytest.fixture
def ctx(tmp_path):
    service = Service(tmp_path / "store")
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Modules"}, "project")
    job = service.demo_import(actor, project["id"], "import")
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    workspace = service.workspace(actor, project["id"])
    yield service, actor, project, workspace, ModuleService(service)
    service.stop_worker()
    service.engine.dispose()


def create(ctx, key, parent=None):
    _, actor, project, _, modules = ctx
    return modules.create(actor, project["id"], {"key": key, "name": key, "parent_id": parent})


def bind(ctx, module, entities=(), sources=(), connectors=()):
    _, actor, project, workspace, modules = ctx
    return modules.set_members(
        actor,
        project["id"],
        module["id"],
        {
            "analysis_set_id": workspace["selected_analysis_set"]["id"],
            "entity_ids": list(entities),
            "source_snapshot_ids": list(sources),
            "connector_ids": list(connectors),
        },
        module["row_version"],
    )


def test_module_move_preserves_entities_revisions_and_prevents_cycles(ctx):
    service, actor, project, workspace, modules = ctx
    root = create(ctx, "root")
    child = create(ctx, "child", root["id"])
    other = create(ctx, "other")
    action = workspace["actions"][0]
    members = bind(ctx, child, [action["entity_id"]])
    moved = modules.update(actor, project["id"], child["id"], {"parent_id": other["id"]}, members["row_version"])
    assert moved["id"] == child["id"] and moved["parent_id"] == other["id"]
    assert modules.members(actor, project["id"], child["id"])["entity_ids"] == [action["entity_id"]]
    with service.db() as db:
        assert (
            db.get(Selection, (workspace["selected_analysis_set"]["id"], action["entity_id"])).entity_revision_id
            == action["entity_revision_id"]
        )
    with pytest.raises(DomainError) as exc:
        modules.update(actor, project["id"], other["id"], {"parent_id": child["id"]}, other["row_version"])
    assert exc.value.status == 422
    with pytest.raises(DomainError) as exc:
        modules.update(actor, project["id"], child["id"], {"name": "stale"}, members["row_version"])
    assert exc.value.status == 412
    with pytest.raises(DomainError) as exc:
        modules.delete(actor, project["id"], other["id"], other["row_version"])
    assert exc.value.status == 409


def test_many_to_many_memberships_search_descendants_type_service_and_path(ctx):
    service, actor, project, workspace, modules = ctx
    root = create(ctx, "root")
    child = create(ctx, "child", root["id"])
    other = create(ctx, "other")
    document = workspace["documents"][0]
    symbol = next(e for e in workspace["entities"] if e["type"] == "symbol" and "InvoiceController" in e["name"])
    bind(ctx, child, [document["entity_id"], symbol["entity_id"]])
    bind(ctx, other, [document["entity_id"]])
    query = {
        "query": "invoice",
        "module_id": root["id"],
        "analysis_set_id": workspace["selected_analysis_set"]["id"],
        "limit": 50,
    }
    result = service.search(actor, project["id"], query)
    assert {r["entity"]["entity_id"] for r in result} == {document["entity_id"], symbol["entity_id"]}
    assert not service.search(actor, project["id"], dict(query, include_descendants=False))
    typed = service.search(actor, project["id"], dict(query, kinds=["symbol"], service_key="billing"))
    assert [r["entity"]["entity_id"] for r in typed] == [symbol["entity_id"]]
    assert not service.search(actor, project["id"], dict(query, kinds=["symbol"], service_key="other-service"))
    path = service.search(actor, project["id"], dict(query, query="docs/invoice-rules.md"))
    assert [r["entity"]["entity_id"] for r in path] == [document["entity_id"]]
    assert modules.members(actor, project["id"], other["id"])["entity_ids"] == [document["entity_id"]]


def test_permissions_filter_names_counts_and_preserve_hidden_members_on_replace(ctx):
    service, actor, project, workspace, modules = ctx
    module = create(ctx, "mixed")
    action = workspace["actions"][0]
    private = next(e for e in workspace["entities"] if e["type"] == "symbol")
    with service.db() as db:
        source_id = db.get(Artifact, db.get(Revision, private["entity_revision_id"]).artifact_id).snapshot_id
    state = bind(ctx, module, [action["entity_id"], private["entity_id"]], [source_id])
    token = service.create_token(
        actor,
        project["id"],
        {"name": "limited editor", "scopes": ["knowledge:read", "knowledge:edit"], "expires_in_days": 1},
    )
    limited = service.authenticate(token["token"], "token")
    tree = modules.tree(limited, project["id"], workspace["selected_analysis_set"]["id"])
    assert len(tree) == 1 and tree[0]["entity_count"] == 1
    visible = modules.members(limited, project["id"], module["id"], workspace["selected_analysis_set"]["id"])
    assert visible["entity_ids"] == [action["entity_id"]]
    assert visible["source_snapshot_ids"] == []
    assert private["entity_id"] not in str(visible)
    with pytest.raises(DomainError) as exc:
        modules.set_members(
            limited, project["id"], module["id"], {"entity_ids": [private["entity_id"]]}, state["row_version"]
        )
    assert exc.value.status == 404
    removed = modules.set_members(limited, project["id"], module["id"], {"entity_ids": []}, state["row_version"])
    assert not removed["entity_ids"]
    with service.db() as db:
        assert db.get(ModuleEntity, (module["id"], private["entity_id"]))
        assert db.get(ModuleSource, (module["id"], source_id))
        assert not db.get(ModuleEntity, (module["id"], action["entity_id"]))
    assert modules.tree(limited, project["id"], workspace["selected_analysis_set"]["id"]) == []
    assert not service.search(
        limited,
        project["id"],
        {"query": "Invoice", "module_id": module["id"], "analysis_set_id": workspace["selected_analysis_set"]["id"]},
    )


def test_snapshot_membership_fixed_while_entity_membership_follows_revision(ctx):
    service, actor, project, workspace, _modules = ctx
    document = workspace["documents"][0]
    with service.db() as db:
        snapshot = db.get(Artifact, document["artifact_id"]).snapshot_id
    fixed = create(ctx, "fixed")
    logical = create(ctx, "logical")
    bind(ctx, fixed, sources=[snapshot])
    bind(ctx, logical, entities=[document["entity_id"]])
    clone = KnowledgeService(service).clone(
        actor, project["id"], workspace["selected_analysis_set"]["id"], "V2", "clone"
    )
    DocumentService(service).save_draft(
        actor,
        document["entity_id"],
        {
            "analysis_set_id": clone["id"],
            "base_revision_id": document["entity_revision_id"],
            "markdown": "# Replacement\nNew unique content.\n",
            "locked_section_ids": [],
        },
        1,
    )
    assert service.search(
        actor, project["id"], {"query": "Replacement", "module_id": logical["id"], "analysis_set_id": clone["id"]}
    )
    assert not service.search(
        actor, project["id"], {"query": "Replacement", "module_id": fixed["id"], "analysis_set_id": clone["id"]}
    )
    assert service.search(
        actor,
        project["id"],
        {"query": "invoice", "module_id": fixed["id"], "analysis_set_id": workspace["selected_analysis_set"]["id"]},
    )


def test_cross_project_parent_binding_and_queries_do_not_resolve(ctx):
    service, actor, project, workspace, modules = ctx
    module = create(ctx, "own")
    other = service.create_project(actor, {"key": "other", "name": "Other"}, "other")
    foreign = modules.create(actor, other["id"], {"key": "foreign", "name": "Foreign"})
    with pytest.raises(DomainError) as exc:
        modules.update(actor, project["id"], module["id"], {"parent_id": foreign["id"]}, module["row_version"])
    assert exc.value.status == 404
    with pytest.raises(DomainError) as exc:
        service.search(actor, project["id"], {"query": "Invoice", "module_id": foreign["id"]})
    assert exc.value.status == 404
    module = modules.update(actor, project["id"], module["id"], {"description": "Description"}, module["row_version"])
    assert modules.delete(actor, project["id"], module["id"], module["row_version"]) == {"deleted": True}
    with service.db() as db:
        assert db.get(BusinessModule, module["id"]) is None
        assert db.get(Revision, workspace["documents"][0]["entity_revision_id"])
