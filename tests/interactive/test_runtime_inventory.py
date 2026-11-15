"""Browser-observed controls stay bound to authorized immutable source pages."""

import copy

import pytest
from sqlalchemy import func, select
from vks_api.bulk_association import BulkAssociationService, InventoryPageInput, PrototypeInventoryPage
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Artifact, uid
from vks_api.service import DomainError

from tests.interactive import test_bulk_association, test_materials

materials = test_materials.materials


def observation(shared, actor, pid, aid, **changes):
    status = shared.inventory_status(actor, pid, aid)
    package = status["packages"][0]
    page = {k: package["pages"][0][k] for k in ("route", "frame_path", "label")}
    targets = [
        {**page, "selector": "body", "state": "", "open_steps": []},
        {**page, "selector": "#mounted", "label": "新增收费项", "state": "", "open_steps": []},
    ]
    value = {
        "analysis_set_id": aid,
        "package_id": package["package_id"],
        "artifact_sha256": package["sha256"],
        "composition": status["composition"],
        "route": page["route"],
        "frame_path": page["frame_path"],
        "targets": targets,
        "observed": len(targets),
        "truncated": 0,
        "unverified_frames": 0,
        "status": "captured",
        **changes,
    }
    return InventoryPageInput.model_validate(value), str(status["selection_version"])


def test_runtime_controls_merge_incrementally_and_preassociate_without_source_execution(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, calls, _ = test_bulk_association.setup(
        materials,
        monkeypatch,
        '<title>收费项</title><div id="app"></div><script>throw new Error("DO_NOT_EXECUTE")</script>',
    )
    shared = BulkAssociationService(service)
    assert shared.status(actor, pid, aid)["coverage"]["controls"] == 0
    form, expected = observation(shared, actor, pid, aid)
    key = uid()
    first = shared.save_inventory(actor, pid, form, expected, key)
    assert shared.save_inventory(actor, pid, form, expected, key) == first
    assert first["selection_version"] == int(expected) and first["pages_captured"] == 1
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(PrototypeInventoryPage)) == 1
    coverage = shared.status(actor, pid, aid)["coverage"]
    assert coverage["controls"] == 1 and coverage["modules"] == 1 and coverage["runtime_pages_captured"] == 1
    assert coverage["dynamic_unverified"] and not coverage["coverage_complete"]
    assert any(item["code"] == "DEFAULT_STATES_ONLY" for item in coverage["limitations"])
    job = test_bulk_association.enqueue(shared, actor, pid, aid)
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["result"]["saved"] == 2
    assert any(row["control"]["selector"] == "#mounted" for row in calls)
    assert len(InteractiveKnowledgeService(service).bindings(actor, pid, aid)) == 2


def test_runtime_observations_reject_wrong_scope_revision_routes_and_replayed_business_steps(materials, monkeypatch):
    service, actor, manager, pid, aid, _, document, _, _ = test_bulk_association.setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    form, expected = observation(shared, actor, pid, aid)
    for change in (
        {"artifact_sha256": "0" * 64},
        {"composition": "0" * 64},
        {"route": "/not-declared"},
        {"observed": 9},
        {"targets": [form.targets[0].model_copy(update={"open_steps": ["#delete"]})]},
    ):
        with pytest.raises(DomainError):
            shared.save_inventory(actor, pid, form.model_copy(update=change), expected, uid())
    with pytest.raises(DomainError, match="变化"):
        shared.save_inventory(actor, pid, form, "0", uid())
    changed = test_materials.stage(
        service, actor, manager, pid, aid, "documents", "# 收费项\n\n更新依据。", package_id=document["package_id"]
    )
    test_materials.activate(service, actor, manager, pid, aid, [changed])
    with pytest.raises(DomainError, match="版本已变化"):
        shared.save_inventory(actor, pid, form, expected, uid())
    assert shared.inventory_status(actor, pid, aid)["pages_captured"] == 0
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(PrototypeInventoryPage)) == 0


def test_observation_access_filters_cached_pages_and_http_is_versioned(materials, monkeypatch):
    web = materials[0]
    service, actor, manager, pid, aid, _, _, _, _ = test_bulk_association.setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    form, expected = observation(shared, actor, pid, aid)
    auth = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).json()["data"]
    endpoint = f"/api/v3/projects/{pid}/associations/inventory"
    assert web.get(endpoint + f"?analysis_set_id={aid}").json()["data"]["pages_total"] == 1
    response = web.put(
        endpoint,
        json=form.model_dump(),
        headers={"If-Match": expected, "Idempotency-Key": uid(), "X-CSRF-Token": auth["csrf_token"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["target_count"] == 2
    with service.db() as s:
        s.get(Artifact, form.package_id).sensitivity = "confidential"
    credential = service.create_token(actor, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    limited = service.authenticate(credential["token"], "token")
    frozen = manager.checkpoint(actor, pid, aid, "Read-only inventory", uid())
    frozen_id = frozen["manifest"]["analysis_set_id"]
    visible = shared.inventory_status(limited, pid, frozen_id)
    assert visible["packages"] == [] and visible["pages_captured"] == 0
    with pytest.raises(DomainError):
        shared.save_inventory(limited, pid, form, expected, uid())
    with pytest.raises(DomainError):
        shared.save_inventory(actor, materials[4][1][0], form, expected, uid())


def test_failed_page_and_truncation_are_honest_and_retry_replaces_only_that_page(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, _, _ = test_bulk_association.setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    form, expected = observation(shared, actor, pid, aid, observed=4, truncated=2)
    shared.save_inventory(actor, pid, form, expected, uid())
    assert shared.status(actor, pid, aid)["coverage"]["uncovered"] == 2
    failed = form.model_copy(update={"targets": [], "observed": 0, "truncated": 0, "status": "failed"})
    shared.save_inventory(actor, pid, failed, expected, uid())
    assert shared.inventory_status(actor, pid, aid)["pages_failed"] == 1
    assert shared.status(actor, pid, aid)["coverage"]["runtime_pages_captured"] == 0
    shared.save_inventory(actor, pid, form, expected, uid())
    assert shared.inventory_status(actor, pid, aid)["pages_captured"] == 1


def test_runtime_repeated_table_rows_group_only_same_operation_and_table(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, _, _ = test_bulk_association.setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    form, expected = observation(shared, actor, pid, aid)
    targets = [form.targets[0].model_dump()]
    for table in ("first", "second"):
        for row in (1, 2):
            target = copy.deepcopy(form.targets[1].model_dump())
            target["selector"] = (
                f"#{table} > tbody:nth-of-type(1) > tr:nth-of-type({row}) > td:nth-of-type(2) > button:nth-of-type(1)"
            )
            target["label"] = "编辑收费项"
            targets.append(target)
    shared.save_inventory(
        actor,
        pid,
        InventoryPageInput.model_validate({**form.model_dump(), "targets": targets, "observed": len(targets)}),
        expected,
        uid(),
    )
    coverage = shared.status(actor, pid, aid)["coverage"]
    assert coverage["controls"] == 4 and coverage["operation_groups"] == 3 and coverage["duplicate_controls"] == 2


def test_captured_pages_reuse_same_source_after_document_updates_but_not_replaced_prototypes(materials, monkeypatch):
    service, actor, manager, pid, aid, prototype, document, _, _ = test_bulk_association.setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    form, expected = observation(shared, actor, pid, aid)
    shared.save_inventory(actor, pid, form, expected, uid())
    newer = test_materials.stage(
        service, actor, manager, pid, aid, "documents", "# 收费项\n\n新的业务说明。", package_id=document["package_id"]
    )
    test_materials.activate(service, actor, manager, pid, aid, [newer])
    current = shared.inventory_status(actor, pid, aid)
    assert current["composition"] != form.composition
    assert current["pages_captured"] == 1
    assert current["packages"][0]["pages"][0]["status"] == "captured"
    assert current["packages"][0]["pages"][0]["current_count"] == 2
    with pytest.raises(DomainError):
        shared.save_inventory(actor, pid, form, expected, uid())
    replacement = test_materials.stage(
        service, actor, manager, pid, aid, "prototype", "<title>新版收费项</title>", package_id=prototype["package_id"]
    )
    test_materials.activate(service, actor, manager, pid, aid, [replacement])
    replaced = shared.inventory_status(actor, pid, aid)
    assert replaced["pages_captured"] == 0
    assert replaced["packages"][0]["pages"][0]["status"] == "pending"
