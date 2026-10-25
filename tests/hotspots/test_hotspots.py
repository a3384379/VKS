import copy

import pytest
from sqlalchemy import select
from vks_api.documents import DocumentService
from vks_api.hotspots import HotspotService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Entity, Evidence, Relation, Revision, Selection
from vks_api.service import DomainError, Service


@pytest.fixture
def ctx(tmp_path):
    service = Service(tmp_path / "store")
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, {"key": "charge-demo", "name": "Hotspots"}, "project")
    job = service.demo_import(actor, project["id"], "import")
    service.process_job(job["id"])
    workspace = service.workspace(actor, project["id"])
    yield service, actor, project, workspace, HotspotService(service)
    service.stop_worker()
    service.engine.dispose()


def page(ctx):
    service, actor, project, workspace, _ = ctx
    return service.workspace(actor, project["id"], workspace["selected_analysis_set"]["id"])["pages"][0]


def payload(ctx, **changes):
    _, _, _, workspace, _ = ctx
    current = page(ctx)
    return dict(
        {
            "analysis_set_id": workspace["selected_analysis_set"]["id"],
            "base_page_revision_id": current["entity_revision_id"],
            "action_key": "manual.extra",
            "label": "Manual annotation",
            "rect": {"x": 0.05, "y": 0.05, "width": 0.12, "height": 0.12},
        },
        **changes,
    )


def save(ctx, **changes):
    _, actor, project, _, hotspots = ctx
    current = page(ctx)
    return hotspots.save(actor, project["id"], current["entity_id"], payload(ctx, **changes), current["row_version"])


def replace_page_html(ctx, html, media="text/html"):
    service, _, _, workspace, _ = ctx
    current = page(ctx)
    with service.db() as db:
        revision = db.get(Revision, current["entity_revision_id"])
        artifact = db.get(Artifact, revision.artifact_id)
        artifact.media_type = media
        artifact.relative_path = "snapshot.png" if media.startswith("image") else "snapshot.html"
        new = DocumentService(service)._revision(
            db,
            db.get(Entity, revision.entity_id),
            artifact,
            revision.name,
            html,
            revision.locator,
            revision.metadata_json,
        )
        db.get(Selection, (workspace["selected_analysis_set"]["id"], revision.entity_id)).entity_revision_id = new.id


def test_new_hotspot_is_immutable_manual_evidence_and_candidate_structure(ctx):
    service, actor, project, workspace, _ = ctx
    original = page(ctx)
    original_bytes = service.artifact_bytes(actor, project["id"], original["artifact_id"])[0]
    result = save(ctx, target_state_key="invoice.application")
    assert result["page"]["entity_revision_id"] != original["entity_revision_id"]
    assert result["page"]["row_version"] == 2
    assert result["action"]["metadata"]["rect"] == payload(ctx)["rect"]
    assert result["action"]["metadata"]["page_revision_id"] == result["page"]["entity_revision_id"]
    assert result["action"]["metadata"]["evidence_origin"] == "manual"
    assert result["action"]["metadata"]["runtime_source_alignment"] == "unknown"
    assert result["hotspots"][0]["status"] == "active"
    with service.db() as db:
        assert db.get(Revision, original["entity_revision_id"]).metadata_json.get("hotspots") is None
        relations = list(
            db.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == workspace["selected_analysis_set"]["id"],
                    Relation.to_revision_id == result["action"]["entity_revision_id"],
                )
            )
        )
        assert {r.relation_type for r in relations} == {"contains", "renders"}
        assert all(r.review_status == "candidate" and r.causal_basis == "not_established" for r in relations)
        assert all(db.get(Evidence, eid).evidence_kind == "manual" for r in relations for eid in r.evidence_ids)
    assert service.artifact_bytes(actor, project["id"], original["artifact_id"])[0] == original_bytes


def test_edit_reuses_action_identity_preserves_decisions_and_remove_keeps_broken_history(ctx):
    service, actor, project, workspace, hotspots = ctx
    initial = save(ctx)
    w = service.workspace(actor, project["id"], workspace["selected_analysis_set"]["id"])
    relation = next(r for r in w["relations"] if r["to_revision_id"] == initial["action"]["entity_revision_id"])
    service.review_relation(
        actor,
        relation["id"],
        {"decision": "confirm", "reason": "Manual review", "evidence_ids": relation["evidence_ids"]},
        relation["row_version"],
    )
    old_action = initial["action"]["entity_revision_id"]
    edited = save(
        ctx,
        action_id=initial["action"]["entity_id"],
        label="Edited label",
        rect={"x": 0.2, "y": 0.3, "width": 0.1, "height": 0.1},
    )
    assert edited["action"]["entity_id"] == initial["action"]["entity_id"]
    assert edited["action"]["entity_revision_id"] != old_action
    with service.db() as db:
        retained = db.get(Relation, relation["id"])
        assert retained.review_status == "confirmed" and retained.manual_lock and retained.validity_status == "stale"
        assert db.get(Revision, old_action).name == "Manual annotation"
    removed = hotspots.save(
        actor,
        project["id"],
        edited["page"]["entity_id"],
        {
            "analysis_set_id": workspace["selected_analysis_set"]["id"],
            "base_page_revision_id": edited["page"]["entity_revision_id"],
            "action_id": edited["action"]["entity_id"],
        },
        edited["page"]["row_version"],
        remove=True,
    )
    assert removed["action"]["metadata"]["status"] == "removed"
    with service.db() as db:
        assert (
            db.get(
                Selection, (workspace["selected_analysis_set"]["id"], initial["action"]["entity_id"])
            ).entity_revision_id
            == removed["action"]["entity_revision_id"]
        )
        assert db.get(Relation, relation["id"]).validity_status == "broken"
        assert db.get(Revision, old_action)


def test_dom_selector_is_unique_visible_passive_and_never_executes_script(ctx):
    html = '<main><button id="unique" class="primary" data-testid="manual">Safe label</button><script>throw new Error("never execute")</script><img src="https://invalid.example/private"></main>'
    replace_page_html(ctx, html)
    result = save(ctx, selector='button#unique.primary[data-testid="manual"]')
    assert result["action"]["metadata"]["selector"] == 'button#unique.primary[data-testid="manual"]'
    assert result["warnings"]
    with pytest.raises(DomainError) as exc:
        save(
            ctx,
            action_key="manual.duplicate",
            rect={"x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1},
            selector="#unique",
        )
    assert exc.value.status == 409


@pytest.mark.parametrize(
    "html,selector",
    [
        ('<main><button class="same">A</button><button class="same">B</button></main>', ".same"),
        ('<main hidden><button id="hidden">A</button></main>', "#hidden"),
        ('<main style="display: none"><button id="hidden">A</button></main>', "#hidden"),
        ('<main aria-hidden="true"><button id="hidden">A</button></main>', "#hidden"),
        ('<main><input id="input"></main>', "#input"),
        ('<main><button id="one">A</button></main>', "main > button"),
    ],
)
def test_selector_conflicts_hidden_and_unsupported_targets_are_reported(ctx, html, selector):
    replace_page_html(ctx, html)
    with pytest.raises(DomainError) as exc:
        save(ctx, selector=selector)
    assert exc.value.status == 409


def test_rect_bounds_overlap_and_stale_page_conflict_do_not_overwrite(ctx):
    _service, actor, project, _, hotspots = ctx
    old = page(ctx)
    base = payload(ctx)
    with pytest.raises(DomainError) as exc:
        save(ctx, rect={"x": 0.9, "y": 0, "width": 0.2, "height": 0.1})
    assert exc.value.status == 409
    result = save(ctx)
    with pytest.raises(DomainError) as exc:
        save(ctx, action_key="manual.overlap")
    assert exc.value.status == 409
    with pytest.raises(DomainError) as exc:
        hotspots.save(actor, project["id"], old["entity_id"], dict(base, action_key="stale.window"), old["row_version"])
    assert exc.value.status == 412
    assert page(ctx)["entity_revision_id"] == result["page"]["entity_revision_id"]


def test_screenshot_rects_work_but_dom_claim_is_rejected(ctx):
    replace_page_html(ctx, "", media="image/png")
    result = save(ctx)
    assert result["action"]["metadata"]["rect"]["width"] > 0
    with pytest.raises(DomainError) as exc:
        save(ctx, action_key="manual.dom", selector="#claim", rect={"x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1})
    assert exc.value.status == 409


def test_clone_edit_never_changes_old_baseline_and_target_state_must_exist(ctx):
    service, actor, project, workspace, hotspots = ctx
    original = page(ctx)
    original_relations = copy.deepcopy(workspace["relations"])
    clone = KnowledgeService(service).clone(
        actor, project["id"], workspace["selected_analysis_set"]["id"], "Clone", "clone"
    )
    with pytest.raises(DomainError) as exc:
        save(ctx, target_state_key="unknown.state")
    assert exc.value.status == 404
    result = hotspots.save(
        actor,
        project["id"],
        original["entity_id"],
        dict(payload(ctx), analysis_set_id=clone["id"]),
        original["row_version"],
    )
    assert result["page"]["entity_revision_id"] != original["entity_revision_id"]
    before = service.workspace(actor, project["id"], workspace["selected_analysis_set"]["id"])
    assert before["pages"][0]["entity_revision_id"] == original["entity_revision_id"]
    assert before["relations"] == original_relations
