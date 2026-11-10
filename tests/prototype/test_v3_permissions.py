from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from vks_api.journeys import JourneyService
from vks_api.knowledge import KnowledgeService
from vks_api.models import Artifact, Evidence, Relation, Release, uid
from vks_api.prototype import prototype_image
from vks_api.prototype_edit import PrototypeEditor, SettingsEdit
from vks_api.prototype_read import PrototypeReader
from vks_api.service import DomainError, Service

from scripts.backup_restore import backup, restore
from scripts.build_prototype_demo import PAGE_KEY, build_bundle
from tests.prototype import test_v3_workflow
from tests.prototype.test_raster_prototype import import_project, original
from tests.prototype.test_v3_workflow import _binding, _headers

workspace = test_v3_workflow.workspace


def edit_anchors(web, service, owner, pid, aid, grant, key, edit):
    root = f"/api/v3/projects/{pid}/prototype/pages/{PAGE_KEY}"
    selected = service.workspace(owner, pid, aid)["selected_analysis_set"]
    headers = _headers(grant, selected["selection_version"], key)
    current = web.get(root, params={"analysis_set_id": aid}, headers=headers).json()["data"]
    anchors = [
        {field: value for field, value in row.items() if field not in {"action_id", "action_revision_id"}}
        for row in current["anchors"]
    ]
    edit(anchors)
    response = web.put(
        root + "/anchors",
        json={
            "analysis_set_id": aid,
            "page_revision_id": current["page_revision_id"],
            "state_key": current["state_key"],
            "name": current["state_name"],
            "anchors": anchors,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def separate_action_artifact(web, service, owner, pid, aid, grant):
    def rename(anchors):
        next(row for row in anchors if row["actionKey"] == "charge.add")["label"] = "内部新增动作"

    edit_anchors(web, service, owner, pid, aid, grant, "rename", rename)

    def move(anchors):
        next(row for row in anchors if row["actionKey"] == "charge.export")["rect"][0] -= 0.01

    edit_anchors(web, service, owner, pid, aid, grant, "move", move)
    state = service.workspace(owner, pid, aid)
    action = next(row for row in state["actions"] if row["stable_key"] == "charge.add")
    assert action["artifact_id"] != state["pages"][0]["artifact_id"]
    return state, action


def test_filtered_anchor_stays_private_in_raw_artifact_and_is_preserved(workspace):
    app, web, service, owner, pid, aid, _, grant = workspace
    state, action = separate_action_artifact(web, service, owner, pid, aid, grant)
    page_artifact_id = state["pages"][0]["artifact_id"]
    reader = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read"]})
    snapshot = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    draft = KnowledgeService(service).clone(owner, pid, aid, "Editable", "fork")["id"]
    raw_path = f"/api/v1/projects/{pid}/artifacts/{page_artifact_id}/download"
    with TestClient(app) as restricted_web:
        headers = _headers(reader, 0, "read")
        assert b"charge.add" in restricted_web.get(raw_path, headers=headers).content
        with service.db() as db:
            db.get(Artifact, action["artifact_id"]).sensitivity = "confidential"
        visible = restricted_web.get(
            f"/api/v1/projects/{pid}/workspace", params={"release_id": snapshot["id"]}, headers=headers
        ).json()["data"]
        assert visible["pages"] and not any(row["stable_key"] == "charge.add" for row in visible["actions"])
        assert "charge.add" not in str(visible["pages"])
        assert restricted_web.get(raw_path, headers=headers).status_code == 404
        assert b"charge.add" in service.artifact_bytes(owner, pid, page_artifact_id)[0]

    def move_visible(anchors):
        next(row for row in anchors if row["actionKey"] == "charge.export")["rect"][0] -= 0.01

    edit_anchors(web, service, owner, pid, draft, grant, "move-visible", move_visible)
    updated = service.workspace(owner, pid, draft)["pages"][0]["metadata"]["prototype_manifest"]["states"][0]
    old = state["pages"][0]["metadata"]["prototype_manifest"]["states"][0]
    assert next(row for row in updated["anchors"] if row["actionKey"] == "charge.add") == next(
        row for row in old["anchors"] if row["actionKey"] == "charge.add"
    )


def test_hidden_parent_page_filters_actions_from_legacy_workspace_and_search(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    state, action = separate_action_artifact(web, service, owner, pid, aid, grant)
    reader = service.authenticate(grant["token"], "token")
    with service.db() as db:
        db.get(Artifact, state["pages"][0]["artifact_id"]).sensitivity = "confidential"
    visible = service.workspace(reader, pid, aid)
    assert not visible["pages"]
    assert action["id"] not in {row["id"] for row in visible["actions"]}
    assert service.search(reader, pid, {"analysis_set_id": aid, "query": action["name"]}) == []
    explanation = next(row for row in state["relations"] if row["from_revision_id"] == action["revision_id"])
    results = service.search(reader, pid, {"analysis_set_id": aid, "query": "charge.guide.create"})
    assert results and not set(explanation["evidence_ids"]).intersection(results[0]["evidence_ids"])
    with pytest.raises(DomainError) as hidden:
        service.set_relation_state(reader, explanation["id"], {"excluded": True}, explanation["row_version"])
    assert hidden.value.status == 404


def test_journey_action_requires_the_selected_owner_page_even_without_state(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    state, action = separate_action_artifact(web, service, owner, pid, aid, grant)
    journeys = JourneyService(service)
    body = {
        "schema_version": "2.0",
        "key": "demo-flow",
        "title": "合成流程",
        "entry": "action",
        "nodes": [
            {
                "key": "action",
                "kind": "action",
                "title": "查看动作",
                "x": 0,
                "y": 0,
                "binding": {"entity_id": action["id"], "revision_id": action["revision_id"]},
            },
            {"key": "done", "kind": "end", "title": "完成", "x": 10, "y": 0},
        ],
        "edges": [{"key": "finish", "source": "action", "target": "done", "label": ""}],
    }
    reader = service.authenticate(grant["token"], "token")
    journeys.create(reader, pid, {"analysis_set_id": aid, "dsl": body}, "create-visible")
    assert journeys.list(reader, pid, aid)
    with service.db() as db:
        db.get(Artifact, state["pages"][0]["artifact_id"]).sensitivity = "confidential"
    assert not journeys.list(reader, pid, aid)
    with pytest.raises(DomainError) as hidden:
        journeys.create(reader, pid, {"analysis_set_id": aid, "dsl": {**body, "key": "hidden-flow"}}, "create-hidden")
    assert hidden.value.status == 404


def test_anchor_edit_keeps_page_relations_within_saved_snapshot(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    state = service.workspace(owner, pid, aid)
    page = state["pages"][0]
    section = next(row for row in state["entities"] if row["stable_key"] == "charge.guide.create")
    relation = service.propose_links(
        owner,
        pid,
        {
            "analysis_set_id": aid,
            "candidates": [
                {
                    "from_revision_id": page["revision_id"],
                    "to_revision_id": section["revision_id"],
                    "relation_type": "documents",
                    "evidence_ids": state["relations"][0]["evidence_ids"],
                    "causal_basis": "not_established",
                    "reason": "页面说明",
                }
            ],
        },
        "page-documents",
    )["candidates"][0]

    def move(anchors):
        anchors[0]["rect"][0] -= 0.01

    saved = edit_anchors(web, service, owner, pid, aid, grant, "move", move)
    snapshot = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")["manifest"]
    frozen = next(row for row in snapshot["relations"] if row["id"] == relation["id"])
    assert frozen["from_revision_id"] == saved["page_revision_id"]
    assert frozen["evidence_ids"] == relation["evidence_ids"]
    selected = {item["entity_revision_id"] for item in snapshot["selections"]}
    assert all({row["from_revision_id"], row["to_revision_id"]} <= selected for row in snapshot["relations"])


def test_binding_rejects_section_from_another_project_atomically(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    other = service.create_project(owner, {"key": "other-prototype", "name": "Other"}, "other-project")["id"]
    target = import_project(service, owner, other, build_bundle(project="other-prototype"), "other-source")
    foreign = next(row for row in service.workspace(owner, other, target)["entities"] if row["type"] == "section")
    expected, body = _binding(service, owner, pid, aid)
    response = web.post(
        f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points",
        json={**body, "section_revision_ids": [foreign["revision_id"]]},
        headers=_headers(grant, expected, "foreign-section"),
    )
    assert response.status_code == 404
    current = service.workspace(owner, pid, aid)
    assert current["selected_analysis_set"]["selection_version"] == expected
    assert body["point_key"] not in {row["stable_key"] for row in current["entities"]}


def test_binding_conflict_and_replay_do_not_disclose_restricted_relation(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    expected, body = _binding(service, owner, pid, aid)
    endpoint = f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points"
    result = web.post(endpoint, json=body, headers=_headers(grant, expected, "save-point"))
    assert result.status_code == 200, result.text
    saved = result.json()["data"]
    with service.db() as db:
        binding = db.get(Relation, saved["binding_id"])
        source = db.get(Artifact, db.get(Evidence, binding.evidence_ids[0]).artifact_id)
        private_source = Artifact(
            id=uid(),
            project_id=pid,
            snapshot_id=source.snapshot_id,
            relative_path="private-evidence.json",
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            kind="fixture",
            media_type="application/json",
            sensitivity="confidential",
            object_key=source.object_key,
        )
        db.add(private_source)
        db.flush()
        private_evidence = Evidence(
            id=uid(),
            project_id=pid,
            analysis_set_id=aid,
            artifact_id=private_source.id,
            stable_key="private-evidence",
            evidence_kind="manual",
            content_sha256=private_source.sha256,
        )
        db.add(private_evidence)
        binding.evidence_ids = [*binding.evidence_ids, private_evidence.id]
    again = web.post(endpoint, json=body, headers=_headers(grant, expected, "save-point"))
    assert again.status_code == 404 and "current_version" not in again.text
    remove = {key: value for key, value in body.items() if key not in {"point", "section_revision_ids"}}
    conflict = web.post(
        endpoint,
        json={**remove, "point_revision_id": saved["point_revision_id"], "binding_version": 1, "excluded": True},
        headers=_headers(grant, saved["selection_version"], "cannot-remove"),
    )
    assert conflict.status_code == 404 and "current_version" not in conflict.text


def test_restricted_settings_editor_preserves_hidden_entry_and_menu(workspace):
    _, _, service, owner, pid, aid, _, _ = workspace
    editor = PrototypeEditor(service)
    selected = SettingsEdit(
        analysis_set_id=aid, entry_page_key=PAGE_KEY, menu_order=[PAGE_KEY], menu_groups={PAGE_KEY: "内部项目"}
    )
    initial = editor.save_settings(owner, pid, selected, 0)
    page = service.workspace(owner, pid, aid)["pages"][0]
    with service.db() as db:
        db.get(Artifact, page["artifact_id"]).sensitivity = "confidential"
    limited = replace(owner, scopes=frozenset({"project:manage", "knowledge:edit", "knowledge:read"}))
    result = editor.save_settings(
        limited, pid, SettingsEdit(analysis_set_id=aid, menu_groups={}), initial["row_version"]
    )
    assert result["entry_page_key"] is None and result["menu_order"] == [] and result["menu_groups"] == {}
    assert service.workspace(limited, pid, aid)["prototype_settings"]["menu_groups"] == {}
    current = service.workspace(owner, pid, aid)["prototype_settings"]
    assert current["entry_page_id"] == page["id"] and current["menu_order"] == [PAGE_KEY]
    assert current["menu_groups"] == {PAGE_KEY: "内部项目"}


def test_snapshot_replays_reject_changed_manifest_bytes(workspace):
    _, _, service, owner, pid, aid, _, _ = workspace
    payload = {"analysis_set_id": aid}
    saved = service.save_snapshot(owner, pid, payload, "snapshot")
    with service.db() as db:
        release = db.get(Release, saved["id"])
        release.manifest_json = {**release.manifest_json, "selection_version": 999}
    for key in ("snapshot", "another-key"):
        with pytest.raises(DomainError) as error:
            service.save_snapshot(owner, pid, payload, key)
        assert error.value.code == "VKS_INTEGRITY_ERROR"


def test_detached_surface_is_unavailable_and_cannot_be_saved_in_new_snapshot(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    editor = PrototypeEditor(service)
    editor.save_settings(owner, pid, SettingsEdit(analysis_set_id=aid, entry_page_key=PAGE_KEY), 0)
    original = service.workspace(owner, pid, aid)["pages"][0]
    saved = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    fork = KnowledgeService(service).clone(owner, pid, aid, "Repair", "fork")["id"]
    with service.db() as db:
        artifact = db.get(Artifact, original["artifact_id"])
        retained = [
            identifier
            for identifier in artifact.metadata_json["prototype_surface_artifact_ids"]
            if db.get(Artifact, identifier).relative_path == "views/edit-drawer.png"
        ]
        artifact.metadata_json = {**artifact.metadata_json, "prototype_surface_artifact_ids": retained}

    root = f"/api/v3/projects/{pid}/prototype"
    params = {"release_id": saved["id"]}
    headers = _headers(grant, 0, "read")
    menu = web.get(root + "/menu", params=params, headers=headers).json()["data"]
    assert menu["entry_page_key"] is None and not menu["pages"][0]["has_surface"]
    page = web.get(root + f"/pages/{PAGE_KEY}", params=params, headers=headers).json()["data"]
    assert page["anchors"] and page["image_sha256"] is None
    context = web.get(
        root + "/actions/charge.add/context",
        params={**params, "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=headers,
    ).json()["data"]
    assert context["missing_target_surface"] and context["display_state_key"] == "list"
    assert (
        web.get(
            root + f"/pages/{PAGE_KEY}/states/list/image",
            params={**params, "page_revision_id": page["page_revision_id"]},
            headers=headers,
        ).status_code
        == 404
    )
    current = service.workspace(owner, pid, release_id=saved["id"])
    states = current["pages"][0]["metadata"]["prototype_manifest"]["states"]
    assert current["prototype_settings"]["entry_page_id"] is None
    assert {state["stateKey"] for state in states if "surface" in state} == {"edit-drawer"}
    with service.db() as db:
        artifact = db.get(Artifact, original["artifact_id"])
        artifact.metadata_json = {
            key: value for key, value in artifact.metadata_json.items() if key != "prototype_surface_artifact_ids"
        }
    raw_path = f"/api/v1/projects/{pid}/artifacts/{original['artifact_id']}/download"
    assert web.get(raw_path, headers=headers).status_code == 404
    with pytest.raises(DomainError) as error:
        service.save_snapshot(owner, pid, {"analysis_set_id": fork}, "blocked-snapshot")
    assert error.value.code == "VKS_INTEGRITY_ERROR"


def test_binding_replays_after_freeze_and_new_write_is_rejected(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    expected, body = _binding(service, owner, pid, aid)
    endpoint = f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points"
    saved = web.post(endpoint, json=body, headers=_headers(grant, expected, "save-point"))
    assert saved.status_code == 200
    service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    repeated = web.post(endpoint, json=body, headers=_headers(grant, expected, "save-point"))
    assert repeated.status_code == 200 and repeated.json()["data"] == saved.json()["data"]
    rejected = web.post(endpoint, json=body, headers=_headers(grant, expected, "new-point"))
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "VKS_STATE_CONFLICT"


def test_restore_keeps_frozen_prototype_bytes_and_exact_point_source(workspace, tmp_path):
    _, web, service, owner, pid, aid, bundle, grant = workspace
    expected, body = _binding(service, owner, pid, aid)
    saved = web.post(
        f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points",
        json=body,
        headers=_headers(grant, expected, "save-point"),
    )
    assert saved.status_code == 200
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "snapshot")
    archive = backup(service.data_dir, tmp_path / "roundtrip.zip")
    destination = tmp_path / "restored"
    restore(archive["path"], destination, expected_sha256=archive["sha256"])
    rebuilt = Service(destination)
    try:
        _, restored_owner = rebuilt.login(rebuilt.bootstrap_token)
        assert rebuilt.bootstrap_token != service.bootstrap_token
        with pytest.raises(DomainError):
            rebuilt.authenticate(grant["token"], "token")
        context = PrototypeReader(rebuilt).context(
            restored_owner, pid, PAGE_KEY, "charge.add", "list", None, release["id"]
        )
        point = context["points"][0]
        assert point["point_revision_id"] == saved.json()["data"]["point_revision_id"]
        assert point["sources"][0]["section_revision_id"] == body["section_revision_ids"][0]
        assert prototype_image(
            rebuilt, restored_owner, pid, PAGE_KEY, "list", context["page_revision_id"], None, release["id"]
        ) == original(bundle)
    finally:
        rebuilt.engine.dispose()
