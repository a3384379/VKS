"""Install, replace, migrate and restore project-owned interactive materials."""

import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.app import create_app
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.materials import (
    ActivateMaterials,
    MaterialService,
    PrepareMaterial,
    RestoreMaterialVersion,
    StudioWorkspace,
)
from vks_api.models import Analysis, Relation, uid

from tests.interactive import test_knowledge
from tests.interactive.test_assistant import response

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
from tests.prototype.test_v3_workflow import _upload


@pytest.fixture
def materials(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    service = app.state.service
    with TestClient(app) as web:
        _, owner = service.login(service.bootstrap_token)
        manager = MaterialService(service)
        projects = []
        for i in range(2):
            pid = service.create_project(owner, {"key": f"material-{i}", "name": f"Project {i}"}, f"project-{i}")["id"]
            aid = manager.open(owner, pid)["analysis_set_id"]
            projects.append((pid, aid))
        yield web, service, owner, manager, projects
    service.engine.dispose()


def selected(service, owner, pid, aid):
    return InteractiveKnowledgeService(service).catalog(owner, pid, aid)


def stage(service, owner, manager, pid, aid, kind, content, package_id=None, name=None, filename=None):
    filename = filename or ("index.html" if kind == "prototype" else "guide.md")
    if isinstance(content, str):
        content = content.encode()
    upload = _upload(service, owner, pid, content, filename)
    return manager.prepare(
        owner,
        pid,
        PrepareMaterial(
            analysis_set_id=aid, kind=kind, name=name or kind, artifact_ids=[upload], package_id=package_id
        ),
        selected(service, owner, pid, aid)["selection_version"],
        uid(),
    )


def activate(service, owner, manager, pid, aid, rows=(), remove=(), entire=False, key=None):
    return manager.activate(
        owner,
        pid,
        ActivateMaterials(
            analysis_set_id=aid,
            revision_ids=[r["revision_id"] for r in rows],
            remove_package_ids=list(remove),
            replace_all=entire,
        ),
        str(selected(service, owner, pid, aid)["selection_version"]),
        key or uid(),
    )


def install(service, owner, manager, pid, aid):
    p = stage(
        service, owner, manager, pid, aid, "prototype", '<title>收费项</title><button id="add">新增收费项</button>'
    )
    d = stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 收费项\n\n新增收费项需要填写名称。\n\n## 删除\n\n删除前检查引用。",
    )
    activate(service, owner, manager, pid, aid, [p, d])
    return p, d


def bind(service, owner, pid, aid, package, *, model=False):
    links = InteractiveKnowledgeService(service)
    catalog = selected(service, owner, pid, aid)
    doc = links.document(owner, pid, catalog["documents"][0]["revision_id"], aid)
    form = InteractiveBinding(
        analysis_set_id=aid,
        package_id=package["artifact_id"],
        target={
            "route": "/",
            "frame_path": [],
            "selector": "#add",
            "label": "新增收费项",
            "state": "",
            "open_steps": [],
        },
        point={"title": "新增收费项说明", "summary": "必须保留的人工说明"},
        section_revision_ids=[doc["sections"][0]["revision_id"]],
    )
    return links.save(
        owner,
        pid,
        form,
        str(catalog["selection_version"]),
        uid(),
        generation={
            "origin": "model",
            "model_id": "synthetic",
            "confidence": 0.95,
            "input_revision_ids": form.section_revision_ids,
        }
        if model
        else None,
    )


def test_project_packages_stage_replace_disable_and_exact_snapshot(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    original = selected(service, owner, pid, aid)
    assert len(original["packages"]) == 2 and len(original["documents"]) == 1
    with service.db() as s:
        from sqlalchemy import select
        from vks_api.knowledge import SourceSnapshot

        assert all(
            row.source_key.startswith("material.")
            for row in s.scalars(select(SourceSnapshot).where(SourceSnapshot.project_id == pid))
        )
    bind(service, owner, pid, aid, p)
    snapshot = manager.checkpoint(owner, pid, aid, "原始完整版本", uid())
    with service.db() as s:
        assert s.get(Analysis, aid).state != "frozen"
    next_doc = stage(service, owner, manager, pid, aid, "documents", "# 新手册\n\n全新业务文档。", name="New docs")
    # Preparing a replacement never changes the currently readable corpus.
    assert selected(service, owner, pid, aid)["documents"] == original["documents"]
    activate(service, owner, manager, pid, aid, [next_doc], entire=True)
    assert [r["name"] for r in selected(service, owner, pid, aid)["documents"]] == ["新手册"]
    assert all(r["kind"] != "prototype" for r in selected(service, owner, pid, aid)["packages"])
    frozen = InteractiveKnowledgeService(service).catalog(owner, pid, release_id=snapshot["id"])
    assert frozen["documents"] == original["documents"]
    result = manager.restore(
        owner,
        pid,
        RestoreMaterialVersion(analysis_set_id=aid, release_id=snapshot["id"]),
        str(selected(service, owner, pid, aid)["selection_version"]),
        uid(),
    )
    restored = selected(service, owner, pid, result["analysis_set_id"])
    assert restored["documents"] == original["documents"]
    links = InteractiveKnowledgeService(service).bindings(owner, pid, result["analysis_set_id"])
    assert links[0]["summary"] == "必须保留的人工说明"
    activate(service, owner, manager, pid, result["analysis_set_id"], remove=[d["package_id"]])
    assert selected(service, owner, pid, result["analysis_set_id"])["documents"] == []
    activate(service, owner, manager, pid, result["analysis_set_id"], [d])
    assert len(selected(service, owner, pid, result["analysis_set_id"])["documents"]) == 1
    other, other_aid = projects[1]
    assert selected(service, owner, other, other_aid)["packages"] == []
    from vks_api.service import DomainError

    with pytest.raises(DomainError):
        activate(service, owner, manager, other, other_aid, [p])


def test_migration_remaps_controls_and_leaves_changed_rules_for_manual_generation(materials, monkeypatch):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    bind(service, owner, pid, aid, p)
    p2 = stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>收费项</title><div><button id="add">创建收费项</button></div>',
        p["package_id"],
    )
    d2 = stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 收费项\n\n创建收费项需要选择类别和填写名称。",
        d["package_id"],
    )
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    calls = []

    def model(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    switched = activate(service, owner, manager, pid, aid, [p2, d2])
    assert switched["migration"]["relocated"] == 1
    assert switched["migration"]["reassociate"] == 1 and switched["job_id"]
    service.process_job(switched["job_id"])
    job = service.get_job(owner, switched["job_id"])
    assert job["state"] == "succeeded", job
    assert job["result"]["saved"] == 0 and job["result"]["pending"] == 1, job
    assert not calls
    links = InteractiveKnowledgeService(service).bindings(owner, pid, aid)
    assert len(links) == 1 and links[0]["target"]["label"] == "创建收费项"
    assert links[0]["target"]["selector"] == "#add"
    assert links[0]["summary"] == "必须保留的人工说明" and links[0]["origin"] == "manual"
    assert links[0]["validity_status"] == "stale"


@pytest.mark.parametrize("legacy_job", [False, True])
def test_identical_section_is_reattached_without_model_and_snapshot_keeps_old_reference(
    materials, monkeypatch, legacy_job
):
    from vks_api.materials import CitationReattachment

    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    bind(service, owner, pid, aid, p, model=True)
    links = InteractiveKnowledgeService(service)
    before = links.bindings(owner, pid, aid)[0]
    snapshot = manager.checkpoint(owner, pid, aid, "更新前", uid())
    updated = stage(
        service, owner, manager, pid, aid, "documents",
        "# 收费项\n\n新增收费项需要填写名称。\n\n## 删除\n\n先确认没有在途订单。", d["package_id"],
    )

    def fail_model(*args):
        pytest.fail("Equivalent citations must never call the model")

    monkeypatch.setattr(ai_gateway, "request_model", fail_model)
    with monkeypatch.context() as legacy:
        if legacy_job:
            legacy.setattr(CitationReattachment, "apply", lambda *a, **k: False)
        switched = activate(service, owner, manager, pid, aid, [updated])
    if legacy_job:
        assert switched["migration"]["reassociate"] == 1
        from copy import deepcopy

        with service.db() as s:
            from vks_api.models import Job
            from vks_api.service import canonical, sha

            job = s.get(Job, switched["job_id"])
            prior_target = deepcopy(job.input_json["targets"][0])
            prior_target["target"]["selector"] = "#completed"
            receipt_key = sha(canonical(prior_target))
            job.input_json = {
                k: v for k, v in job.input_json.items() if k not in ("local_plan_complete", "reattached")
            }
            job.input_json = {**job.input_json, "targets": [prior_target, *job.input_json["targets"]]}
            job.state, job.progress = "cancelled", 50
            job.result_json = {
                "total": 2, "saved": 1,
                "completed": {receipt_key: {"label": "已保存", "saved": True, "message": "完成"}},
            }
        key = uid()
        resumed = manager.retry_migration(
            owner, pid, aid, str(switched["selection_version"]), key, resume_job_id=switched["job_id"],
        )
        assert resumed["progress"] == 50 and resumed["result"]["processed"] == 1
        assert resumed["id"] != switched["job_id"]
        service.process_job(resumed["id"])
        result = service.get_job(owner, resumed["id"])["result"]
        assert result["reattached"] == result["preserved"] == 1
        assert result["processed"] == result["total"] == 2
        assert result["completed"][receipt_key]["message"] == "完成"
        assert service.get_job(owner, switched["job_id"])["state"] == "cancelled"
        assert manager.retry_migration(
            owner, pid, aid, str(switched["selection_version"]), key, resume_job_id=switched["job_id"],
        )["id"] == resumed["id"]
    else:
        assert switched["migration"]["reattached"] == 1
        assert switched["migration"]["reassociate"] == 0
    after = links.bindings(owner, pid, aid)[0]
    assert after["id"] == before["id"] and after["origin"] == before["origin"] == "model"
    assert after["summary"] == before["summary"]
    assert after["validity_status"] == "valid"
    assert after["sections"][0]["revision_id"] != before["sections"][0]["revision_id"]
    frozen = links.bindings(owner, pid, release_id=snapshot["id"])[0]
    assert frozen["sections"] == before["sections"]


def test_excluded_links_stay_excluded_and_deleted_sections_leave_corpus(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    saved = bind(service, owner, pid, aid, p)
    with service.db() as s:
        s.get(Relation, saved["binding_id"]).excluded = True
    d2 = stage(service, owner, manager, pid, aid, "documents", "# 收费项\n\n已修改。", d["package_id"])
    switched = activate(service, owner, manager, pid, aid, [d2])
    assert switched["job_id"] is None
    links = InteractiveKnowledgeService(service).bindings(owner, pid, aid)
    assert links[0]["excluded"]
    doc = InteractiveKnowledgeService(service).document(
        owner, pid, selected(service, owner, pid, aid)["documents"][0]["revision_id"], aid
    )
    assert len(doc["sections"]) == 1


def test_atomic_stale_import_invalid_input_and_idempotent_activation(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    v = selected(service, owner, pid, aid)["selection_version"]
    form = ActivateMaterials(analysis_set_id=aid, remove_package_ids=[d["package_id"]])
    key = uid()
    first = manager.activate(owner, pid, form, str(v), key)
    assert manager.activate(owner, pid, form, str(v), key) == first
    from vks_api.service import DomainError

    with pytest.raises(DomainError):
        manager.activate(
            owner,
            pid,
            ActivateMaterials(analysis_set_id=aid, replace_all=True, revision_ids=[p["revision_id"], "missing"]),
            str(first["selection_version"]),
            uid(),
        )
    assert len(selected(service, owner, pid, aid)["packages"]) == 1


def test_migration_stops_when_workspace_is_rolled_back(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    bind(service, owner, pid, aid, p)
    snap = manager.checkpoint(owner, pid, aid, "Before", uid())
    d2 = stage(service, owner, manager, pid, aid, "documents", "# 收费项\n\n新版规则。", d["package_id"])
    changed = activate(service, owner, manager, pid, aid, [d2])
    restored = manager.restore(
        owner,
        pid,
        RestoreMaterialVersion(analysis_set_id=aid, release_id=snap["id"]),
        str(changed["selection_version"]),
        uid(),
    )
    service.process_job(changed["job_id"])
    assert service.get_job(owner, changed["job_id"])["state"] == "failed"
    with service.db() as s:
        assert s.get(StudioWorkspace, pid).analysis_set_id == restored["analysis_set_id"]


def test_static_site_resources_and_no_server_execution(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    buffer = BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr(
            "index.html",
            '<title>Demo</title><link rel="stylesheet" href="assets/style.css"><button id="hello">Hello</button><a href="next.html">Next</a>',
        )
        z.writestr("next.html", "<h1>Next</h1>")
        z.writestr("assets/style.css", "body{color:green}")
    staged = stage(service, owner, manager, pid, aid, "prototype", buffer.getvalue(), filename="site.zip")
    assert staged["format"] == "static-site"
    activate(service, owner, manager, pid, aid, [staged])
    raw = InteractiveKnowledgeService(service).content(owner, pid, staged["artifact_id"], aid)
    assert b"vks-site" in raw and b"__prototypePack" in raw


def test_disable_all_does_not_resurrect_packages_on_reopen(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    install(service, owner, manager, pid, aid)
    activate(service, owner, manager, pid, aid, entire=True)
    manager.open(owner, pid, aid)
    assert selected(service, owner, pid, aid)["packages"] == []
    assert selected(service, owner, pid, aid)["documents"] == []


def test_restore_retry_is_idempotent_and_old_snapshot_is_unchanged(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    install(service, owner, manager, pid, aid)
    snap = manager.checkpoint(owner, pid, aid, "Baseline", uid())
    previous = snap["manifest_sha256"]
    form = RestoreMaterialVersion(analysis_set_id=aid, release_id=snap["id"])
    v, key = str(selected(service, owner, pid, aid)["selection_version"]), uid()
    restored = manager.restore(owner, pid, form, v, key)
    assert manager.restore(owner, pid, form, v, key) == restored
    assert service.releases(owner, pid)[-1]["manifest_sha256"] == previous


def test_material_http_project_and_source_permissions(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    other, other_aid = projects[1]
    grant = service.create_token(owner, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    headers = {"Authorization": "Bearer " + grant["token"]}
    endpoint = f"/api/v3/projects/{pid}/materials"
    assert web.get(endpoint, params={"analysis_set_id": aid}, headers=headers).status_code == 200
    assert web.get(
        f"/api/v3/projects/{other}/materials", params={"analysis_set_id": other_aid}, headers=headers
    ).status_code in {403, 404}
    assert (
        web.post(
            endpoint + "/activate",
            json={"analysis_set_id": aid, "revision_ids": [p["revision_id"]]},
            headers={**headers, "If-Match": "1", "Idempotency-Key": "denied"},
        ).status_code
        == 403
    )
    from vks_api.models import Artifact, Revision

    with service.db() as s:
        root = s.get(Revision, d["revision_id"])
        child = s.get(Revision, root.metadata_json["material_package"]["members"][0])
        s.get(Artifact, child.artifact_id).sensitivity = "confidential"
    response = web.get(endpoint, params={"analysis_set_id": aid}, headers=headers)
    assert response.status_code == 200
    assert [g["id"] for g in response.json()["data"]["packages"]] == [p["package_id"]]
    raw = web.get(
        f"/api/v3/projects/{pid}/interactive/packages/{d['artifact_id']}/content",
        params={"analysis_set_id": aid},
        headers=headers,
    )
    assert raw.status_code in {403, 404}


def test_unresolved_targets_are_reported_without_silent_rebinding(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, _ = install(service, owner, manager, pid, aid)
    bind(service, owner, pid, aid, p)
    replacement = stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        "<title>收费项</title><button>另一个操作</button>",
        p["package_id"],
    )
    changed = activate(service, owner, manager, pid, aid, [replacement])
    assert changed["migration"]["unresolved"] == 1
    service.process_job(changed["job_id"])
    job = service.get_job(owner, changed["job_id"])
    assert job["state"] == "succeeded" and job["completeness"] == "partial"
    assert job["result"]["pending"] == 1 and job["result"]["issues"][0]["label"] == "新增收费项"


def test_manual_document_edits_survive_other_package_changes_and_disable(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    _p, d = install(service, owner, manager, pid, aid)
    doc = selected(service, owner, pid, aid)["documents"][0]
    service.save_document(
        owner,
        doc["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": doc["revision_id"],
            "markdown": "# 收费项\n\n人工维护的正文。\n\n## 新章节\n\n人工新增的章节。",
            "locked_section_ids": [],
        },
        "1",
    )
    extra = stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 其他资料\n\n独立内容。",
        name="Other docs",
        filename="other.md",
    )
    activate(service, owner, manager, pid, aid, [extra])
    current_doc = next(r for r in selected(service, owner, pid, aid)["documents"] if r["id"] == doc["id"])
    assert current_doc["revision_id"] != doc["revision_id"]
    activate(service, owner, manager, pid, aid, remove=[d["package_id"]])
    current = selected(service, owner, pid, aid)
    assert all(r["id"] != doc["id"] for r in current["documents"])
    library = manager.library(owner, pid, aid)
    captured = next(g for g in library["packages"] if g["id"] == d["package_id"])["versions"][0]
    activate(service, owner, manager, pid, aid, [captured])
    assert any(r["revision_id"] == current_doc["revision_id"] for r in selected(service, owner, pid, aid)["documents"])


def test_legacy_prototype_upgrade_retains_its_authored_point(interactive):
    from base64 import b64encode
    from gzip import compress

    from packages.analyzers.vks_ingest import canonical_json
    from tests.interactive.test_knowledge import form

    _web, service, owner, pid, aid, _grant, package_id, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    links.save(
        owner,
        pid,
        InteractiveBinding(**form(aid, package_id, section)),
        str(selected(service, owner, pid, aid)["selection_version"]),
        uid(),
    )
    manager = MaterialService(service)
    manager.open(owner, pid, aid)
    library = manager.library(owner, pid, aid)
    p = next(g for g in library["packages"] if g["kind"] == "prototype")
    payload = canonical_json(
        {
            "routeMap": {"/demo": "page.html"},
            "pages": {"page.html": {"title": "Synthetic page", "html": '<button id="add">Add</button>'}},
            "resources": [],
            "repoAssets": {},
        }
    )
    newraw = (
        b'<script id="prototypePackPayload" type="application/octet-stream">'
        + b64encode(compress(payload))
        + b'</script><title>Updated prototype</title><button id="add">Add</button>'
    )
    replacement = stage(service, owner, manager, pid, aid, "prototype", newraw, p["id"])
    activate(service, owner, manager, pid, aid, [replacement])
    current = links.bindings(owner, pid, aid)
    assert len(current) == 1 and current[0]["summary"] == "Synthetic persistent knowledge"
    assert current[0]["package_id"] == replacement["artifact_id"]


def test_migration_diagnostics_restrict_names_and_counts_after_permission_change(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    p, d = install(service, owner, manager, pid, aid)
    bind(service, owner, pid, aid, p)
    d2 = stage(service, owner, manager, pid, aid, "documents", "# 收费项\n\n新版。", d["package_id"])
    result = activate(service, owner, manager, pid, aid, [d2])
    service.process_job(result["job_id"])
    token = service.create_token(owner, pid, {"name": "Limited editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    headers = {"Authorization": "Bearer " + token["token"]}
    response = web.get("/api/v1/jobs/" + result["job_id"], headers=headers)
    assert response.status_code == 200, response.text
    info = response.json()["data"]
    assert info["diagnostics_restricted"] and "result" not in info and "issue_count" not in info


@pytest.mark.parametrize("change", ["import_scope", "source_policy"])
def test_prepare_rechecks_access_after_parsing_before_creating_revisions(materials, monkeypatch, change):
    from sqlalchemy import func, select
    from vks_api import material_parser
    from vks_api.models import Artifact, Credential, Revision
    from vks_api.service import DomainError

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    current = stage(service, owner, manager, pid, aid, "documents", "# Original\n\nOriginal body.")
    activate(service, owner, manager, pid, aid, [current])
    token = service.create_token(
        owner, pid, {"name": "Importer", "scopes": ["knowledge:read", "knowledge:edit", "source:import"]}
    )
    importer = service.authenticate(token["token"], "token")
    upload = _upload(service, importer, pid, b"# Replacement\n\nNew body.", "replacement.md")
    initial = selected(service, owner, pid, aid)["selection_version"]
    with service.db() as db:
        before = db.scalar(select(func.count()).select_from(Revision))
    parse = material_parser.parse

    def change_after_parse(*args, **kwargs):
        result = parse(*args, **kwargs)
        with service.db() as db:
            if change == "import_scope":
                credential = db.get(Credential, importer.credential_id)
                credential.scopes = [scope for scope in credential.scopes if scope != "source:import"]
            else:
                package = db.get(Revision, current["revision_id"])
                artifact = db.get(Artifact, package.artifact_id)
                artifact.sensitivity = "confidential"
        return result

    monkeypatch.setattr(material_parser, "parse", change_after_parse)
    with pytest.raises(DomainError) as error:
        manager.prepare(
            importer,
            pid,
            PrepareMaterial(
                analysis_set_id=aid,
                kind="documents",
                name="Replacement",
                artifact_ids=[upload],
                package_id=current["package_id"],
            ),
            str(initial),
            uid(),
        )
    assert error.value.code in {"VKS_FORBIDDEN", "VKS_NOT_FOUND"}
    with service.db() as db:
        assert db.scalar(select(func.count()).select_from(Revision)) == before
        assert db.get(Analysis, aid).selection_version == initial
