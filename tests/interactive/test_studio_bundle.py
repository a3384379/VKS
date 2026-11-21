"""Round-trip a frozen Studio composition through the ordinary upload/import job."""

from base64 import b64decode
from io import BytesIO
from zipfile import ZipFile

import pytest

from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import Relation, Revision, uid
from vks_api.service import Service

from tests.interactive import test_materials
from tests.prototype.test_v3_workflow import _upload

materials = test_materials.materials


def zip_resources(**entries):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for path, raw in entries.items():
            archive.writestr(path.replace("__", "/"), raw)
    return output.getvalue()


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("guide.md", "独立前言。\n\n# 规则\n\n必须校验。\n\n## 补充\n\n按时办理。"),
        ("guide.txt", "独立前言。\n\n# 规则\n\n必须校验。\n\n## 补充\n\n按时办理。"),
        ("guide.html", "<p>独立前言。</p><h1>规则</h1><p>必须校验。</p><h2>补充</h2><p>按时办理。</p>"),
    ],
    ids=["markdown", "plain-text", "html"],
)
def test_round_trip_restores_runtime_and_exact_ordered_references(materials, tmp_path, filename, source):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    originals = {
        name: zip_resources(
            **{
                "index.html": f"<title>目录</title><a href='detail.html'>明细</a><p>{name}</p>",
                "detail.html": "<title>明细</title><button id='add'>Add</button>",
            }
        )
        for name in ("原型 A", "原型 B")
    }
    packs = [
        test_materials.stage(service, owner, manager, pid, aid, "prototype", raw, name=name, filename="site.zip")
        for name, raw in originals.items()
    ]
    docs = test_materials.stage(service, owner, manager, pid, aid, "documents", source, filename=filename)
    test_materials.activate(service, owner, manager, pid, aid, [*packs, docs])
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    doc = catalog["documents"][0]
    body = links.document(owner, pid, doc["revision_id"], aid)
    with service.db() as s:
        locked = s.get(Revision, body["sections"][-1]["revision_id"]).entity_id
    markdown = service.get_document(owner, doc["id"], doc["revision_id"])["markdown"]
    markdown = markdown.replace("必须校验。", "人工修订，必须校验金额。")
    service.save_document(
        owner,
        doc["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": doc["revision_id"],
            "markdown": markdown,
            "locked_section_ids": [locked],
        },
        "1",
    )
    doc = links.catalog(owner, pid, aid)["documents"][0]
    body = links.document(owner, pid, doc["revision_id"], aid)
    target = {
        "route": "/detail.html",
        "frame_path": ["actualPage"],
        "selector": "#add",
        "label": "Add",
        "state": "popup",
        "open_steps": ["#open"],
    }
    for i, pack in enumerate(packs):
        catalog = links.catalog(owner, pid, aid)
        result = links.save(
            owner,
            pid,
            InteractiveBinding(
                analysis_set_id=aid,
                package_id=pack["artifact_id"],
                target=target,
                point={"title": f"人工规则 {i}", "summary": f"原型 {i} 的独立维护说明[1][2]"},
                section_revision_ids=[body["sections"][-1]["revision_id"], body["sections"][1]["revision_id"]],
            ),
            catalog["selection_version"],
            uid(),
        )
        if i == 0:
            with service.db() as s:
                s.get(Relation, result["binding_id"]).excluded = True
    snapshot = manager.checkpoint(owner, pid, aid, "固定版本", uid())
    original_bindings = links.bindings(owner, pid, release_id=snapshot["id"])
    grant = service.create_token(
        owner,
        pid,
        {
            "name": "portable transport",
            "scopes": ["knowledge:read", "knowledge:export", "source:import"],
        },
    )["token"]
    headers = {"Authorization": f"Bearer {grant}"}
    export = web.post(
        f"/api/v3/projects/{pid}/interactive/tools/export",
        json={"release_id": snapshot["id"], "format": "vksbundle"},
        headers={**headers, "Idempotency-Key": "studio-export"},
    )
    assert export.status_code == 202, export.text
    job = export.json()["data"]["id"]
    service.process_job(job)
    result = service.get_job(owner, job)
    assert result["state"] == "succeeded", result.get("error") or result
    response = web.get(result["result"]["download_url"], headers=headers)
    assert response.status_code == 200, response.text
    raw = response.content
    with ZipFile(BytesIO(raw)) as archive:
        import json

        manifest = json.loads(archive.read("manifest.json"))
        portable = json.loads(archive.read(manifest["studioManifest"]["path"]))
        assert len(portable["bindings"]) == 2
        assert all(
            b64decode(archive.read(pack["inputs"][0]["path"])) == originals[pack["name"]]
            for pack in portable["packages"]
            if pack["kind"] == "prototype"
        )
        documents_package = next(pack for pack in portable["packages"] if pack["kind"] == "documents")
        assert documents_package["documents"][0]["path"] == filename
        original = documents_package["inputs"][0]
        assert original["name"] == filename
        assert archive.read(original["path"]) == source.encode()

    def import_twice(destination, principal, project):
        upload = _upload(destination, principal, project, raw, "studio.vksbundle.zip")
        inputs = {"kind": "bundle", "artifact_ids": [upload]}
        assert destination.preflight(principal, project, inputs)["valid"]
        restored_aid = None
        for attempt in range(2):
            imported = destination.import_bundle(principal, project, inputs, f"bundle-import-{attempt}")
            destination.process_job(imported["id"])
            result = destination.get_job(principal, imported["id"])
            assert result["state"] == "succeeded", result.get("error") or result
            candidate = result["result"]["analysis_set_id"]
            if restored_aid is not None:
                assert candidate == restored_aid and result["result"]["reused"]
            restored_aid = candidate
        restored = InteractiveKnowledgeService(destination)
        catalog = restored.catalog(principal, project, restored_aid)
        by_id = {r["id"]: r for r in catalog["packages"]}
        bindings = sorted(restored.bindings(principal, project, restored_aid), key=lambda r: r["title"])
        assert len(bindings) == 2
        for i, binding in enumerate(bindings):
            pack = by_id[binding["package_id"]]
            assert pack["name"] == ("原型 A", "原型 B")[i]
            assert binding["target"] == target and binding["excluded"] == (i == 0)
            assert binding["summary"] == f"原型 {i} 的独立维护说明[1][2]"
            assert [r["name"] for r in binding["sections"]] == ["补充", "规则"]
            runtime = restored.content(principal, project, pack["id"], restored_aid)
            assert runtime == links.content(owner, pid, packs[i]["artifact_id"], release_id=snapshot["id"])
        document = catalog["documents"][0]
        assert document["path"] == filename
        restored_doc = destination.get_document(principal, document["id"], document["revision_id"])
        assert restored_doc["markdown"] == markdown
        with destination.db() as session:
            assert session.get(Revision, document["revision_id"]).metadata_json["source_kind"] == "platform_draft"
        assert [r["name"] for r in restored_doc["sections"] if r["metadata"]["manual_lock"]] == ["补充"]
        assert bindings[1]["sections"][0]["document_revision_id"] == document["revision_id"]

    import_twice(service, owner, pid)
    fresh = Service(tmp_path / "fresh")
    try:
        _, new_owner = fresh.login(fresh.bootstrap_token)
        project = fresh.create_project(new_owner, {"key": "material-0", "name": "导入项目"}, "same-logical-project")
        import_twice(fresh, new_owner, project["id"])
    finally:
        fresh.engine.dispose()
    assert links.bindings(owner, pid, release_id=snapshot["id"]) == original_bindings
