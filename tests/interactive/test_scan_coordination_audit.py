"""Independent scan lease, partial-scope and single-enqueue negative controls."""

import pytest
from sqlalchemy import func, select
from vks_api.bulk_association import BatchAssociationInput, BulkAssociationService, InventoryPageInput
from vks_api.models import Artifact, Credential, Job, uid
from vks_api.prototype_scan import PrototypeScanService, ScanClient, ScanHeartbeat, ScanInput
from vks_api.service import DomainError

from tests.interactive import test_bulk_association, test_materials
from tests.interactive.test_document_navigation import zip_docs

materials = test_materials.materials


def setup(materials, monkeypatch, mode="all", *, pipeline=False):
    service, owner, manager, pid, aid, initial, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    prototype = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        zip_docs(
            [
                (
                    name + ".html",
                    f'<title>收费项 {name}</title><h1>收费项 {name}</h1><button id="{name}">Static {name}</button>',
                )
                for name in ("first", "second", "third")
            ]
        ),
        package_id=initial["package_id"],
        filename="pages.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype])
    bulk, scans = BulkAssociationService(service), PrototypeScanService(service)
    version = str(bulk.inventory_status(owner, pid, aid)["selection_version"])
    run = scans.start(
        owner,
        pid,
        ScanInput(analysis_set_id=aid, mode=mode, client_id="first-client", pipeline=pipeline),
        version,
        uid(),
    )
    return service, owner, manager, pid, aid, bulk, scans, version, run, calls


def capture(bulk, owner, pid, run, version, page, *, client="first-client", failed=False):
    package = run["manifest"]["packages"][0]
    target = {
        "route": page["route"],
        "frame_path": page["frame_path"],
        "selector": "body",
        "label": page["label"],
        "state": "",
        "open_steps": [],
    }
    targets = [] if failed else [target, {**target, "selector": "#captured", "label": "Observed control"}]
    form = InventoryPageInput(
        analysis_set_id=run["analysis_set_id"],
        package_id=package["package_id"],
        artifact_sha256=package["sha256"],
        composition=run["manifest"]["composition"],
        route=page["route"],
        frame_path=page["frame_path"],
        targets=targets,
        observed=len(targets),
        truncated=0,
        unverified_frames=0,
        status="failed" if failed else "captured",
        scan_run_id=run["id"],
        client_id=client,
    )
    return bulk.save_inventory(owner, pid, form, version, uid())


def test_failed_pages_require_explicit_partial_and_only_captured_page_targets_enter_single_job(materials, monkeypatch):
    service, owner, _, pid, aid, bulk, scans, version, run, calls = setup(materials, monkeypatch)
    pages = run["manifest"]["packages"][0]["pages"]
    for index, page in enumerate(pages):
        capture(bulk, owner, pid, run, version, page, failed=index != 0)
    current = scans.status(owner, pid, aid)["run"]
    assert current["state"] == "partial" and current["pages_completed"] == 1
    assert current["pages_attempted"] == 3 and current["pages_failed"] == 2
    with pytest.raises(DomainError):
        bulk.enqueue(
            owner, pid, BatchAssociationInput(analysis_set_id=aid, mode="all", scan_run_id=run["id"]), version, uid()
        )
    assert not bulk.status(owner, pid, aid)["jobs"] and not calls
    form = BatchAssociationInput(analysis_set_id=aid, mode="all", scan_run_id=run["id"], allow_partial=True)
    job = bulk.enqueue(owner, pid, form, version, uid())
    assert bulk.enqueue(owner, pid, form, version, uid())["id"] == job["id"]
    with service.db() as s:
        stored = s.get(Job, job["id"])
        items = stored.input_json["items"]
        assert {item["target"]["route"] for item in items} == {pages[0]["route"]}
        assert {item["target"]["selector"] for item in items} == {"body", "#captured"}
        assert {key for group in stored.input_json["groups"] for key in group} <= {item["id"] for item in items}
        assert stored.input_json["partial_scope"]["pages_failed"] == 2
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "associate_all")) == 1
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "succeeded"
    assert all(call["control"]["route"] == pages[0]["route"] for call in calls)
    assert bulk.enqueue(owner, pid, form, version, uid())["id"] == job["id"]


def test_mode_claim_cancel_and_permission_changes_fence_late_scan_requests(materials, monkeypatch):
    service, owner, _, pid, aid, bulk, scans, version, run, calls = setup(materials, monkeypatch, mode="missing")
    page = run["manifest"]["packages"][0]["pages"][0]
    with pytest.raises(DomainError):
        scans.start(owner, pid, ScanInput(analysis_set_id=aid, mode="all", client_id="other-client"), version, uid())
    resumed = scans.resume(owner, pid, run["id"], ScanClient(client_id="second-client"), version, uid())
    assert resumed["id"] == run["id"]
    for operation in (
        lambda: capture(bulk, owner, pid, run, version, page),
        lambda: scans.heartbeat(owner, pid, run["id"], ScanHeartbeat(client_id="first-client")),
        lambda: scans.cancel(owner, pid, run["id"], "first-client"),
    ):
        with pytest.raises(DomainError):
            operation()
    capture(bulk, owner, pid, run, version, page, client="second-client")
    scans.cancel(owner, pid, run["id"], "second-client")
    with pytest.raises(DomainError):
        capture(bulk, owner, pid, run, version, page, client="second-client")
    with pytest.raises(DomainError):
        bulk.enqueue(
            owner,
            pid,
            BatchAssociationInput(analysis_set_id=aid, mode="missing", scan_run_id=run["id"], allow_partial=True),
            version,
            uid(),
        )
    assert not calls
    with service.db() as s:
        s.get(Artifact, run["manifest"]["packages"][0]["package_id"]).sensitivity = "confidential"
    token = service.create_token(owner, pid, {"name": "scan-limited", "scopes": ["knowledge:read", "knowledge:edit"]})
    limited = service.authenticate(token["token"], "token")
    assert scans.status(limited, pid, aid)["run"] is None


def test_saved_scan_revocation_hides_previous_labels_and_rejects_resume_and_partial_model(materials, monkeypatch):
    service, owner, _, pid, aid, bulk, scans, version, initial, calls = setup(materials, monkeypatch)
    package_id = initial["manifest"]["packages"][0]["package_id"]
    with service.db() as s:
        s.get(Artifact, package_id).sensitivity = "confidential"
    token = service.create_token(
        owner,
        pid,
        {
            "name": "scanning-editor",
            "scopes": ["knowledge:read", "knowledge:edit", "source:read", "trace:read"],
        },
    )
    actor = service.authenticate(token["token"], "token")
    run = scans.start(actor, pid, ScanInput(analysis_set_id=aid, mode="all", client_id="token-client"), version, uid())
    page = run["manifest"]["packages"][0]["pages"][0]
    capture(bulk, actor, pid, run, version, page, client="token-client")
    with service.db() as s:
        credential = s.get(Credential, actor.credential_id)
        credential.scopes = ["knowledge:read", "knowledge:edit"]
    restricted = service.authenticate(token["token"], "token")
    hidden = scans.status(restricted, pid, aid)["run"]
    assert hidden["id"] == run["id"] and not hidden["current_scope"]
    assert hidden["pages_completed"] == hidden["controls"] == 0
    assert hidden["manifest"]["packages"] == [] and hidden["current"] is None
    with pytest.raises(DomainError):
        scans.resume(restricted, pid, run["id"], ScanClient(client_id="new-token-client"), version, uid())
    with pytest.raises(DomainError):
        bulk.enqueue(
            restricted,
            pid,
            BatchAssociationInput(analysis_set_id=aid, mode="all", scan_run_id=run["id"], allow_partial=True),
            version,
            uid(),
        )
    assert not calls
