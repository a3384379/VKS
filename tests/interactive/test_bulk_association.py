"""Upfront control association is durable, scoped and preserves authored links."""

import json

import pytest
from sqlalchemy import func, select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.bulk_association import BatchAssociationInput, BulkAssociationService
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.material_parser import inventory_report
from vks_api.models import Artifact, Job, Relation, Revision, uid
from vks_api.service import DomainError

from tests.interactive import test_materials
from tests.interactive.test_assistant import response

materials = test_materials.materials
SOURCE = """<title>收费项</title><button id="add">新增收费项</button>
<label for="name">名称</label><input id="name" value="DO_NOT_SEND_INPUT_VALUE">
<label for="kind">类别</label><select id="kind"><option>不要用选项文本冒充字段名</option></select>
<label for="memo">备注</label><textarea id="memo">DO_NOT_SEND_TEXTAREA_VALUE</textarea>
<table id="fees"><tr><td><button id="edit1">编辑收费项</button></td></tr>
<tr><td><button id="edit2">编辑收费项</button></td></tr></table>"""


def setup(materials, monkeypatch, source=SOURCE):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(service, owner, manager, pid, aid, "prototype", source)
    document = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 收费项\n\n新增收费项需要填写名称、选择类别、填写备注。编辑收费项检查原值。",
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype, document])
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    calls = []

    def model(_config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    return service, owner, manager, pid, aid, prototype, document, calls, model


def enqueue(manager, owner, pid, aid, *, mode="missing", resume=None, key=None, expected=None, concurrency=1):
    status = manager.status(owner, pid, aid)
    return manager.enqueue(
        owner,
        pid,
        BatchAssociationInput(analysis_set_id=aid, mode=mode, resume_job_id=resume, concurrency=concurrency),
        str(status["selection_version"] if expected is None else expected),
        key or uid(),
    )


def test_unlinked_modules_fields_and_repeated_row_actions_are_preassociated(materials, monkeypatch):
    service, owner, _, pid, aid, _, _, calls, _ = setup(materials, monkeypatch)
    manager = BulkAssociationService(service)
    before = manager.status(owner, pid, aid)
    assert before["coverage"]["discovered"] == 7
    assert before["coverage"]["controls"] == 6 and before["coverage"]["modules"] == 1
    assert before["coverage"]["operation_groups"] == 6 and before["coverage"]["duplicate_controls"] == 1
    assert before["coverage"]["pending"] == 7
    job = enqueue(manager, owner, pid, aid)
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["state"] == "succeeded", done
    assert done["result"]["saved"] == 7 and done["result"]["pending"] == 0
    assert len(calls) == 6, calls
    assert all(c["context"]["page_title"] == "收费项" for c in calls)
    assert "DO_NOT_SEND" not in json.dumps(calls)
    after = manager.status(owner, pid, aid)
    assert after["coverage"]["linked"] == 7 and after["coverage"]["pending"] == 0
    links = InteractiveKnowledgeService(service).bindings(owner, pid, aid)
    aliases = [b for b in links if b["target"]["selector"] in ("#edit1", "#edit2")]
    assert len(aliases) == 2
    with service.db() as s:
        for binding in aliases:
            point = s.get(Revision, binding["point_revision_id"])
            art = s.get(Artifact, point.artifact_id)
            assert art.metadata_json["interactive_generation"]["origin"] == "model"
            assert {p["revision_id"] for p in art.metadata_json["input_revision_provenance"]} == {
                c["revision_id"] for c in calls[-1]["chapters"]
            }
    reused = enqueue(manager, owner, pid, aid)
    service.process_job(reused["id"])
    assert len(calls) == 6
    assert service.get_job(owner, reused["id"])["result"]["reused"] == 7


def test_reusing_all_valid_links_does_not_require_embedding_or_model_access(materials, monkeypatch):
    from vks_api.vector_rag import RagSettings, VectorRag, VectorStore

    service, owner, _, pid, aid, _, _, calls, _ = setup(materials, monkeypatch)
    shared = BulkAssociationService(service)
    first = enqueue(shared, owner, pid, aid)
    service.process_job(first["id"])
    count = len(calls)
    with service.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = {**policy.settings, "enabled": False}
    monkeypatch.setattr(VectorStore, "settings", lambda *args: RagSettings(enabled=True))

    def forbidden(*args, **kwargs):
        raise AssertionError("Reused knowledge must not start a network index build")

    monkeypatch.setattr(VectorRag, "build_documents", forbidden)
    reused = enqueue(shared, owner, pid, aid)
    service.process_job(reused["id"])
    done = service.get_job(owner, reused["id"])
    assert done["state"] == "succeeded" and done["result"]["reused"] == 7
    assert len(calls) == count


def test_manual_and_excluded_targets_are_preserved_even_in_all_mode(materials, monkeypatch):
    service, owner, _, pid, aid, prototype, _, calls, _ = setup(materials, monkeypatch)
    saved = test_materials.bind(service, owner, pid, aid, prototype)
    manager = BulkAssociationService(service)
    first = enqueue(manager, owner, pid, aid, mode="all")
    service.process_job(first["id"])
    assert service.get_job(owner, first["id"])["result"]["manual"] == 1
    binding = next(
        b for b in InteractiveKnowledgeService(service).bindings(owner, pid, aid) if b["id"] == saved["binding_id"]
    )
    assert binding["summary"] == "必须保留的人工说明"
    with service.db() as s:
        s.get(Relation, saved["binding_id"]).excluded = True
    second = enqueue(manager, owner, pid, aid, mode="all")
    service.process_job(second["id"])
    assert service.get_job(owner, second["id"])["result"]["excluded"] == 1
    assert not any(c["control"]["selector"] == "#add" for c in calls)


def test_active_deduplication_idempotency_and_stale_scope(materials, monkeypatch):
    service, owner, material, pid, aid, _, document, calls, _ = setup(materials, monkeypatch)
    manager = BulkAssociationService(service)
    key = uid()
    expected = manager.status(owner, pid, aid)["selection_version"]
    first = enqueue(manager, owner, pid, aid, key=key, expected=expected)
    second = enqueue(manager, owner, pid, aid, expected=0)
    assert first["id"] == second["id"]
    with service.db() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "associate_all")) == 1
    changed = test_materials.stage(
        service, owner, material, pid, aid, "documents", "# 收费项\n\n新规则。", package_id=document["package_id"]
    )
    test_materials.activate(service, owner, material, pid, aid, [changed])
    service.process_job(first["id"])
    failed = service.get_job(owner, first["id"])
    assert failed["state"] == "failed" and failed["error"]["code"] == "VKS_VERSION_CONFLICT", failed
    assert not calls
    assert manager.status(owner, pid, aid)["jobs"][0]["current_scope"] is False
    with pytest.raises(DomainError, match="资料版本已变化"):
        enqueue(manager, owner, pid, aid, resume=first["id"])


def test_cancel_resume_preserves_completed_targets_without_automatic_resend(materials, monkeypatch):
    source = '<title>收费项</title><button id="add">新增收费项</button><button>编辑收费项</button>'
    service, owner, _, pid, aid, _, _, calls, model = setup(materials, monkeypatch, source)
    manager = BulkAssociationService(service)
    job = enqueue(manager, owner, pid, aid)

    def cancel_second(config, body):
        answer = model(config, body)
        if len(calls) == 2:
            service.cancel_job(owner, job["id"])
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", cancel_second)
    service.process_job(job["id"])
    stopped = service.get_job(owner, job["id"])
    assert stopped["state"] == "cancelled", stopped
    assert stopped["result"]["processed"] == 1
    monkeypatch.setattr(ai_gateway, "request_model", model)
    resumed = enqueue(manager, owner, pid, aid, resume=job["id"])
    assert resumed["id"] == job["id"]
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["state"] == "succeeded" and done["result"]["saved"] == 3, done
    assert sum(c["control"]["selector"] == "body" for c in calls) == 1


def test_insufficient_evidence_is_partial_and_explicitly_retryable(materials, monkeypatch):
    service, owner, _, pid, aid, _, _, calls, model = setup(materials, monkeypatch)
    manager = BulkAssociationService(service)

    def uncertain(config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return response(data["chapters"][0]["revision_id"], confidence=0.2)

    monkeypatch.setattr(ai_gateway, "request_model", uncertain)
    job = enqueue(manager, owner, pid, aid)
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded" and result["completeness"] == "partial"
    assert result["result"]["processed"] == 7 and result["result"]["pending"] == 7
    assert len(calls) == 6  # insufficient repeated rows do not make repeated inference calls
    monkeypatch.setattr(ai_gateway, "request_model", model)
    enqueue(manager, owner, pid, aid, resume=job["id"])
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["result"]["pending"] == 0


def test_source_revocation_before_commit_and_diagnostics_do_not_leak(materials, monkeypatch):
    service, owner, _, pid, aid, prototype, _, _, _ = setup(materials, monkeypatch)
    token = service.create_token(owner, pid, {"name": "Batch editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    actor = service.authenticate(token["token"], "token")
    manager = BulkAssociationService(service)
    job = enqueue(manager, actor, pid, aid)

    def revoke(_config, body):
        with service.db() as s:
            s.get(Artifact, prototype["artifact_id"]).sensitivity = "confidential"
        data = json.loads(body["input"][1]["content"])
        return response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", revoke)
    service.process_job(job["id"])
    read = service.get_job(actor, job["id"])
    assert "result" not in read and read["diagnostics_restricted"] is True
    assert manager.status(actor, pid, aid)["coverage"]["discovered"] == 0
    assert not InteractiveKnowledgeService(service).bindings(owner, pid, aid)


def test_scan_reports_dynamic_unknown_and_truncated_unlabeled_controls():
    report = inventory_report(
        '<title>Test</title><button></button><input type="hidden"><script>throw new Error("never execute")</script>'
        + "<button>Open</button>" * 1510
    )
    assert report["dynamic"] is True
    assert report["discovered"] == 1512 and len(report["targets"]) == 1501
    assert report["truncated"] == 11
    unusual = inventory_report('<button id="1a:b">Open</button><div id="box"><button>Next</button></div>')
    assert unusual["targets"][1]["selector"] == "#\\31 a\\:b"
    assert unusual["targets"][2]["selector"] == "#box > button:nth-of-type(1)"


def test_bounded_parallel_generation_commits_distinct_targets(materials, monkeypatch):
    from threading import Barrier

    service, owner, _, pid, aid, _, _, calls, model = setup(materials, monkeypatch)
    barrier = Barrier(3)

    def concurrent(config, body):
        answer = model(config, body)
        barrier.wait(timeout=5)
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", concurrent)
    manager = BulkAssociationService(service)
    job = enqueue(manager, owner, pid, aid, concurrency=3)
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["state"] == "succeeded" and done["result"]["saved"] == 7, done
    assert len(calls) == 6
    first = manager.details(owner, pid, job["id"], limit=3)
    second = manager.details(owner, pid, job["id"], offset=3, limit=4)
    assert len(first["items"]) == 3 and first["has_more"]
    assert len(second["items"]) == 4 and not second["has_more"]
    assert len({row["id"] for row in first["items"] + second["items"]}) == 7


def test_changed_mode_does_not_claim_unlinked_controls_as_reused(materials, monkeypatch):
    service, owner, _, pid, aid, _, _, calls, _ = setup(materials, monkeypatch)
    manager = BulkAssociationService(service)
    job = enqueue(manager, owner, pid, aid, mode="changed")
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["result"]["skipped"] == 7 and done["result"]["reused"] == 0
    assert not calls and manager.status(owner, pid, aid)["coverage"]["pending"] == 7


def test_batch_http_scopes_and_details_recheck_permissions(materials, monkeypatch):
    service, _owner, _, pid, aid, _, _, _, _ = setup(materials, monkeypatch)
    web = materials[0]
    auth = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).json()["data"]
    path = f"/api/v3/projects/{pid}/associations/batch"
    status = web.get(path, params={"analysis_set_id": aid})
    assert status.status_code == 200 and status.json()["data"]["coverage"]["discovered"] == 7, status.text
    headers = {
        "X-CSRF-Token": auth["csrf_token"],
        "If-Match": str(status.json()["data"]["selection_version"]),
        "Idempotency-Key": "batch-http",
    }
    queued = web.post(path, json={"analysis_set_id": aid}, headers=headers)
    assert queued.status_code == 200, queued.text
    service.process_job(queued.json()["data"]["id"])
    replay = web.post(path, json={"analysis_set_id": aid}, headers=headers)
    assert replay.status_code == 200 and replay.json()["data"]["id"] == queued.json()["data"]["id"]
    other_pid, other_aid = materials[-1][1]
    assert web.get(path, params={"analysis_set_id": other_aid}).status_code == 404
    assert (
        web.get(
            f"/api/v3/projects/{other_pid}/associations/batch/items", params={"job_id": queued.json()["data"]["id"]}
        ).status_code
        == 404
    )


def test_large_batch_checkpoints_bound_job_json_and_keep_earlier_failures_partial(materials, monkeypatch):
    source = "<title>收费项</title>" + "".join(f'<button id="b{i}">收费项操作{i}</button>' for i in range(210))
    service, owner, _, pid, aid, prototype, _, calls, _ = setup(materials, monkeypatch, source)
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    doc = links.document(owner, pid, catalog["documents"][0]["revision_id"], aid)
    manual = links.save(
        owner,
        pid,
        InteractiveBinding(
            analysis_set_id=aid,
            package_id=prototype["artifact_id"],
            target={
                "route": "/",
                "frame_path": [],
                "selector": "body",
                "label": "收费项",
                "state": "",
                "open_steps": [],
            },
            point={"title": "人工模块说明", "summary": "保留人工说明"},
            section_revision_ids=[doc["sections"][0]["revision_id"]],
        ),
        str(catalog["selection_version"]),
        uid(),
    )
    with service.db() as s:
        s.get(Relation, manual["binding_id"]).validity_status = "stale"

    def uncertain(_config, body):
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return response(data["chapters"][0]["revision_id"], confidence=0.2)

    monkeypatch.setattr(ai_gateway, "request_model", uncertain)
    manager = BulkAssociationService(service)
    job = enqueue(manager, owner, pid, aid, mode="changed")
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["state"] == "succeeded" and done["completeness"] == "partial", done
    result = done["result"]
    assert result["processed"] == 211 and result["skipped"] == 210 and result["pending"] == 1
    assert result["details_truncated"] and len(result["completed"]) == 200
    assert result["issues"] and len(json.dumps(result)) < 100000
    all_rows = []
    for offset in (0, 100, 200):
        all_rows.extend(manager.details(owner, pid, job["id"], offset=offset)["items"])
    assert len(all_rows) == 211 and sum(r["status"] == "insufficient" for r in all_rows) == 1
