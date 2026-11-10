from base64 import b64encode
from gzip import compress
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from packages.analyzers.vks_ingest import canonical_json
from scripts.build_prototype_demo import PAGE_KEY, build_bundle
from tests.prototype.test_v3_workflow import _binding, _headers, workspace  # noqa: F401
from vks_api.models import Artifact, Credential, Job
from vks_api.service import DomainError


def upload(service, actor, pid, filename, raw, media_type):
    pending = service.create_upload(
        actor,
        pid,
        {"filename": filename, "size_bytes": len(raw), "sha256": sha256(raw).hexdigest(), "media_type": media_type},
    )
    service.put_upload(actor, pid, pending["upload_session_id"], raw)
    return pending["artifact_id"]


def png():
    stream = BytesIO()
    Image.new("RGB", (20, 10), "white").save(stream, "PNG")
    return stream.getvalue()


def preview(web, service, grant, pid, aid, ids):
    headers = {"Authorization": "Bearer " + grant["token"]}
    response = web.post(
        f"/api/v3/projects/{pid}/prototype/import-previews",
        json={"analysis_set_id": aid, "artifact_ids": ids},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    job = response.json()["data"]
    service.process_job(job["id"])
    out = web.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()["data"]
    assert out["state"] == "succeeded", out
    return job["id"], out["result"]


def confirm(web, grant, pid, aid, preview_id, report, ids, key):
    return web.post(
        f"/api/v3/projects/{pid}/prototype/imports",
        json={
            "analysis_set_id": aid,
            "preview_id": preview_id,
            "preview_digest": report["preview_digest"],
            "selected_artifact_ids": ids,
        },
        headers=_headers(grant, report["selection_version"], key),
    )


def test_mixed_preflight_can_skip_invalid_archive_and_commit_exact_png_and_markdown(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    raw = png()
    image = upload(service, actor, pid, "screenshot.png", raw, "image/png")
    document = upload(service, actor, pid, "details.md", b"# Additional guide\n\nRead only.\n", "text/markdown")
    broken = upload(service, actor, pid, "broken.zip", b"invalid ZIP", "application/zip")
    preview_id, report = preview(web, service, grant, pid, aid, [image, document, broken])
    rows = {row["artifact_id"]: row for row in report["materials"]}
    assert rows[image]["format"] == "png"
    assert rows[document]["format"] == "markdown"
    assert rows[broken]["status"] == "blocked"
    assert rows[broken]["issues"][0]["code"] == "VKS_INVALID_ARCHIVE"
    assert not service.get_job(owner, preview_id).get("result")
    assert confirm(web, grant, pid, aid, preview_id, report, [document, broken], "invalid").status_code == 422
    assert confirm(web, grant, pid, aid, preview_id, report, [document, document], "duplicate").status_code == 422
    version = report["selection_version"]
    assert service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"] == version

    accepted = confirm(web, grant, pid, aid, preview_id, report, [image, document], "partial")
    assert accepted.status_code == 202, accepted.text
    jid = accepted.json()["data"]["id"]
    service.process_job(jid)
    service.process_job(jid)
    out = service.get_job(actor, jid)
    assert out["state"] == "succeeded", out
    assert out["result"]["skipped_count"] == 1
    assert out["result"]["selection_version"] == version + 1
    current = service.workspace(owner, pid, aid)
    assert current["selected_analysis_set"]["selection_version"] == version + 1
    page = next(row for row in current["pages"] if row["stable_key"] == out["result"]["first_page_key"])
    assert page["metadata"]["prototype_manifest"]["states"][0]["viewport"]["height"] == 10
    path = f"/api/v3/projects/{pid}/prototype/pages/{page['stable_key']}/states/default/image"
    query = {"analysis_set_id": aid, "page_revision_id": page["entity_revision_id"]}
    assert web.get(path, params=query, headers=_headers(grant, version, "image")).content == raw
    assert any(row["name"] == "Additional guide" for row in current["documents"])
    repeated = confirm(web, grant, pid, aid, preview_id, report, [document, image], "partial")
    assert repeated.json()["data"]["id"] == jid
    assert confirm(web, grant, pid, aid, preview_id, report, [image], "stale").status_code == 409

    duplicate_preview, duplicate_report = preview(web, service, grant, pid, aid, [image, document])
    repeated = confirm(web, grant, pid, aid, duplicate_preview, duplicate_report, [image, document], "repeat")
    service.process_job(repeated.json()["data"]["id"])
    duplicate_result = service.get_job(actor, repeated.json()["data"]["id"])["result"]
    assert {row["status"] for row in duplicate_result["materials"]} == {"reused"}
    assert duplicate_result["selection_version"] == version + 1

    service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "freeze")
    replay = confirm(web, grant, pid, aid, preview_id, report, [image, document], "partial")
    assert replay.json()["data"]["id"] == jid


def test_batch_rollback_and_retry_preserve_atomic_selection(workspace, monkeypatch):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    ids = [upload(service, actor, pid, f"doc-{i}.md", f"# New doc {i}\n".encode(), "text/markdown") for i in range(2)]
    preview_id, report = preview(web, service, grant, pid, aid, ids)
    accepted = confirm(web, grant, pid, aid, preview_id, report, ids, "atomic")
    jid = accepted.json()["data"]["id"]
    from vks_api import prototype_import

    real_commit = prototype_import.persist_bundle
    calls = []

    def fail_after_second_write(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "模拟临时故障", 503, retryable=True)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(prototype_import, "persist_bundle", fail_after_second_write)
        service.process_job(jid)
    assert len(calls) == 2
    assert service.get_job(actor, jid)["state"] == "failed"
    current = service.workspace(owner, pid, aid)
    assert current["selected_analysis_set"]["selection_version"] == report["selection_version"]
    assert not any(row["name"].startswith("New doc") for row in current["documents"])
    service.retry_job(actor, jid)
    service.process_job(jid)
    assert service.get_job(actor, jid)["state"] == "succeeded"
    updated = service.workspace(owner, pid, aid)
    assert updated["selected_analysis_set"]["selection_version"] == report["selection_version"] + 1
    assert sum(row["name"].startswith("New doc") for row in updated["documents"]) == 2


def test_anchor_only_reimport_keeps_action_and_explanation_current(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    base = f"/api/v3/projects/{pid}/prototype"
    version, body = _binding(service, owner, pid, aid)
    action = base + "/actions/charge.add"
    saved = web.post(action + "/business-points", json=body, headers=_headers(grant, version, "point"))
    assert saved.is_success, saved.text

    def move(page):
        page["states"][0]["anchors"][0]["rect"][0] -= 0.02

    asset = upload(service, actor, pid, "layout.zip", build_bundle(edit=move), "application/zip")
    preview_id, report = preview(web, service, grant, pid, aid, [asset])
    assert report["materials"][0]["operations"] == {"added": 0, "updated": 1, "reused": 10}
    response = confirm(web, grant, pid, aid, preview_id, report, [asset], "move")
    service.process_job(response.json()["data"]["id"])
    context = web.get(
        action + "/context",
        params={"analysis_set_id": aid, "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=_headers(grant, version, "read"),
    ).json()["data"]
    assert context["action_revision_id"] == body["action_revision_id"]
    assert context["page_revision_id"] != body["page_revision_id"]
    assert context["points"][0]["validity"] == "valid"


def test_partial_offline_directory_refresh_preserves_supplemented_state(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    base = f"/api/v3/projects/{pid}/prototype"

    def offline(label):
        data = {
            "routeMap": {"/demo": "page.html"},
            "pages": {"page.html": {"title": label, "html": "<p>Fixture</p>"}},
            "resources": [],
            "repoAssets": {},
        }
        return (
            b'<script id="prototypePackPayload" type="application/octet-stream">'
            + b64encode(compress(canonical_json(data)))
            + b"</script>"
        )

    def import_offline(label):
        asset = upload(service, actor, pid, "offline.html", offline(label), "text/html")
        receipt, report = preview(web, service, grant, pid, aid, [asset])
        response = confirm(web, grant, pid, aid, receipt, report, [asset], label)
        jid = response.json()["data"]["id"]
        service.process_job(jid)
        result = service.get_job(actor, jid)
        assert result["state"] == "succeeded", result
        return result["result"]

    first = import_offline("Original")
    key = first["first_page_key"]
    route = base + f"/pages/{key}"
    headers = _headers(grant, first["selection_version"], "supplement")
    page = web.get(route, params={"analysis_set_id": aid}, headers=headers).json()["data"]
    raw = png()
    image = upload(service, actor, pid, "offline.png", raw, "image/png")
    saved = web.put(
        route + "/anchors",
        json={
            "analysis_set_id": aid,
            "page_revision_id": page["page_revision_id"],
            "state_key": "captured",
            "name": "补录状态",
            "anchors": [
                {"anchorKey": "test", "actionKey": "offline.action", "label": "合成按钮", "rect": [0.1, 0.1, 0.2, 0.2]}
            ],
            "viewport": {"width": 20, "height": 10, "devicePixelRatio": 1},
            "upload_artifact_id": image,
        },
        headers=headers,
    )
    assert saved.is_success, saved.text
    updated = import_offline("Updated")
    assert updated["first_page_key"] == key
    page = web.get(route, params={"analysis_set_id": aid, "state_key": "captured"}, headers=headers).json()["data"]
    assert page["name"] == "Updated"
    assert page["anchors"][0]["actionKey"] == "offline.action"
    assert (
        web.get(
            route + "/states/captured/image",
            params={"analysis_set_id": aid, "page_revision_id": page["page_revision_id"]},
            headers=headers,
        ).content
        == raw
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_import_receipt_does_not_replace_unreadable_selected_page(workspace, legacy):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    initial = service.workspace(owner, pid, aid)["pages"][0]
    with service.db() as s:
        s.get(Artifact, initial["artifact_id"]).sensitivity = "confidential"
    asset = upload(service, actor, pid, "changed.zip", build_bundle(accent="#371d52"), "application/zip")
    if legacy:
        response = web.post(
            f"/api/v1/projects/{pid}/imports",
            json={"kind": "prototype", "analysis_set_id": aid, "artifact_ids": [asset]},
            headers=_headers(grant, 0, "restricted"),
        )
    else:
        receipt, report = preview(web, service, grant, pid, aid, [asset])
        response = confirm(web, grant, pid, aid, receipt, report, [asset], "restricted")
    jid = response.json()["data"]["id"]
    service.process_job(jid)
    result = service.get_job(actor, jid)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "VKS_NOT_FOUND"
    assert service.workspace(owner, pid, aid)["pages"][0]["entity_revision_id"] == initial["entity_revision_id"]


@pytest.mark.parametrize("revoke", [False, True])
def test_queued_batch_revalidates_exact_draft_and_actor(workspace, revoke):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    upload_id = upload(service, actor, pid, "pending.md", b"# Pending material\n", "text/markdown")
    receipt, report = preview(web, service, grant, pid, aid, [upload_id])
    response = confirm(web, grant, pid, aid, receipt, report, [upload_id], "pending")
    jid = response.json()["data"]["id"]
    if revoke:
        service.revoke_token(owner, pid, grant["id"])
    else:
        version, form = _binding(service, owner, pid, aid)
        changed = web.post(
            f"/api/v3/projects/{pid}/prototype/actions/charge.add/business-points",
            json=form,
            headers=_headers(grant, version, "concurrent"),
        )
        assert changed.is_success, changed.text
    service.process_job(jid)
    with service.db() as s:
        job = s.get(Job, jid)
        assert job.state == "failed"
        assert job.error_json["code"] == ("VKS_UNAUTHENTICATED" if revoke else "VKS_VERSION_CONFLICT")
    assert not any(row["name"] == "Pending material" for row in service.workspace(owner, pid, aid)["documents"])


def test_preview_receipt_rejects_other_actor_wrong_digest_and_changed_read_scope(workspace):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    raw = b"# Receipt test\n"
    upload_id = upload(service, actor, pid, "receipt.md", raw, "text/markdown")
    receipt, report = preview(web, service, grant, pid, aid, [upload_id])
    foreign = service.create_token(owner, pid, {"name": "other-editor", "scopes": grant["scopes"]})
    other = service.authenticate(foreign["token"], "token")
    other_upload = upload(service, other, pid, "receipt.md", raw, "text/markdown")
    assert confirm(web, foreign, pid, aid, receipt, report, [other_upload], "foreign").status_code == 404
    altered = dict(report, preview_digest="0" * 64)
    assert confirm(web, grant, pid, aid, receipt, altered, [upload_id], "changed").status_code == 409
    with service.db() as s:
        credential = s.get(Credential, grant["id"])
        credential.scopes = [*credential.scopes, "source:read"]
    assert confirm(web, grant, pid, aid, receipt, report, [upload_id], "changed-scope").status_code == 409
    assert (
        service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"] == report["selection_version"]
    )


def handbook(files):
    index = {
        path: {"path": path, "base64": b64encode(raw).decode(), "sha256": sha256(raw).hexdigest()}
        for path, raw in files.items()
    }
    payload = b'<script id="source-data" type="application/json">' + canonical_json(index) + b"</script>"
    meta = {"format": "fh-gzip-html-1", "bytes": len(payload), "sha256": sha256(payload).hexdigest()}
    return (
        b'<script id="fh-pack-meta" type="application/json">'
        + canonical_json(meta)
        + b'</script><script id="fh-pack-data" type="application/octet-stream">'
        + b64encode(compress(payload))
        + b"</script>"
    )


@pytest.mark.parametrize("manual", [False, True])
def test_handbook_refresh_preserves_exact_unmodified_input_selection_and_citations(workspace, manual):
    _, web, service, owner, pid, aid, _, grant = workspace
    actor = service.authenticate(grant["token"], "token")
    stable = b"# Preserved chapter\n\nUnchanged original bytes.\n"

    def update(changed, key):
        payload = handbook({"preserved.md": stable, "edited.md": changed})
        uploaded = upload(service, actor, pid, "handbook.html", payload, "text/html")
        receipt, report = preview(web, service, grant, pid, aid, [uploaded])
        if key == "second-handbook":
            assert report["materials"][0]["operations"] == {"added": 0, "reused": 2, "updated": 2}
        response = confirm(web, grant, pid, aid, receipt, report, [uploaded], key)
        jid = response.json()["data"]["id"]
        service.process_job(jid)
        job = service.get_job(actor, jid)
        assert job["state"] == "succeeded", job
        return job["result"]["materials"][0]

    def selected():
        current = service.workspace(owner, pid, aid)
        document = next(row for row in current["documents"] if row["name"] == "Preserved chapter")
        section = next(
            row for row in current["entities"] if row["type"] == "section" and row["name"] == "Preserved chapter"
        )
        return document, section

    update(b"# Other chapter\n\nOriginal\n", "first-handbook")
    document, section = selected()
    if manual:
        service.save_document(
            owner,
            document["id"],
            {
                "analysis_set_id": aid,
                "base_revision_id": document["entity_revision_id"],
                "markdown": "# Preserved chapter\n\nCurated draft text.\n",
                "locked_section_ids": [section["id"]],
            },
            str(document["metadata"].get("row_version", 1)),
        )
        document, section = selected()
        assert section["metadata"]["manual_lock"]

    version, binding = _binding(service, owner, pid, aid)
    action = f"/api/v3/projects/{pid}/prototype/actions/charge.add"
    saved = web.post(
        action + "/business-points",
        json=dict(binding, section_revision_ids=[section["entity_revision_id"]]),
        headers=_headers(grant, version, "handbook-proof"),
    )
    assert saved.is_success, saved.text
    result = update(b"# Other chapter\n\nUpdated\n", "second-handbook")
    assert result["operations"] == {"added": 0, "reused": 2, "updated": 2}
    current_doc, current_section = selected()
    assert current_doc["entity_revision_id"] == document["entity_revision_id"]
    assert current_section["entity_revision_id"] == section["entity_revision_id"]
    context = web.get(
        action + "/context",
        params={"analysis_set_id": aid, "page_key": PAGE_KEY, "trigger_state_key": "list"},
        headers=_headers(grant, version, "read"),
    ).json()["data"]
    assert context["points"][0]["sources"][0]["section_revision_id"] == section["entity_revision_id"]


def test_raw_handbook_inherits_member_permissions_across_refresh(workspace):
    _, web, service, owner, pid, aid, _, limited_grant = workspace
    grant = service.create_token(
        owner, pid, {"name": "source editor", "scopes": [*limited_grant["scopes"], "source:read"]}
    )
    source_editor = service.authenticate(grant["token"], "token")
    controlled = b"# Controlled sample\n\nSynthetic material.\n"

    def update(body, key):
        raw = handbook({"controlled.md": controlled, "edited.md": body})
        uploaded = upload(service, source_editor, pid, "handbook.html", raw, "text/html")
        receipt, report = preview(web, service, grant, pid, aid, [uploaded])
        response = confirm(web, grant, pid, aid, receipt, report, [uploaded], key)
        jid = response.json()["data"]["id"]
        service.process_job(jid)
        assert service.get_job(source_editor, jid)["state"] == "succeeded"
        with service.db() as s:
            return s.scalar(
                select(Artifact.id).where(
                    Artifact.project_id == pid,
                    Artifact.relative_path == "offline/original.html",
                    Artifact.sha256 == sha256(raw).hexdigest(),
                )
            )

    original_id = update(b"# Another sample\n\nOriginal\n", "original")
    first = f"/api/v1/projects/{pid}/artifacts/{original_id}/download"
    headers = _headers(limited_grant, 0, "limited")
    assert web.get(first, headers=headers).status_code == 200
    with service.db() as s:
        material = s.scalar(
            select(Artifact).where(Artifact.project_id == pid, Artifact.relative_path == "handbook/controlled.md")
        )
        material.sensitivity = "confidential"
    assert web.get(first, headers=headers).status_code == 403
    refreshed_id = update(b"# Another sample\n\nUpdated\n", "refreshed")
    second = f"/api/v1/projects/{pid}/artifacts/{refreshed_id}/download"
    assert web.get(second, headers=headers).status_code == 403
    assert web.get(second, headers=_headers(grant, 0, "authorized")).status_code == 200
    with service.db() as s:
        archive = s.get(Artifact, refreshed_id)
        material = s.scalar(
            select(Artifact).where(
                Artifact.snapshot_id == archive.snapshot_id, Artifact.relative_path == "handbook/controlled.md"
            )
        )
        assert material.metadata_json["requires_source_read"]
        material.size_bytes += 1  # A corrupted registry cannot authorize the aggregate bytes.
    with pytest.raises(DomainError, match="未找到文件"):
        service.artifact_bytes(owner, pid, refreshed_id)
