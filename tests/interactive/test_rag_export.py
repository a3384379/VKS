"""Portable vectors contain only exact authorized document chunks, never DB state."""

import base64
import json
import math
import struct

import pytest
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.models import Analysis, Artifact, Entity, Revision, Selection, uid
from vks_api.service import DomainError
from vks_api.vector_rag import RagSettings, split

from tests.interactive import test_knowledge, test_materials
from tests.prototype.test_v3_workflow import _headers

workspace = test_knowledge.workspace
materials = test_materials.materials
interactive = test_knowledge.interactive


@pytest.fixture
def vectors(workspace, monkeypatch):
    _app, _web, service, owner, pid, aid, _bundle, _grant = workspace
    with service.db() as db:
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True,
                    allow_external=True,
                    model_id="answer-model",
                    secret_ref="model-default",
                    endpoint="https://api.openai.com/v1",
                ).model_dump(),
            )
        )
    calls = []

    def provider(config, payload):
        assert config["protocol"] == "embeddings"
        calls.append(payload)
        return {
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * (payload["dimensions"] - 1)}
                for i in range(len(payload["input"]))
            ],
            "usage": {"total_tokens": len(payload["input"]) * 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    rag = InteractiveAssistant(service).rag
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    return service, owner, pid, aid, rag, calls


def test_plain_documents_build_export_without_any_html_prototype(vectors):
    service, owner, pid, aid, rag, calls = vectors
    # The standard fixture is Markdown plus raster-prototype data; it contains
    # no interactive HTML package on which indexing could accidentally depend.
    with service.db() as db:
        _, _, choices, _ = rag.assistant.links.reader.scope(db, owner, pid, aid, None)
        assert not rag.assistant.links._packages(db, owner, pid, choices)
    chapters, _, _ = rag.document_scope(owner, pid, analysis_set_id=aid)
    result = rag.export_index(owner, pid, analysis_set_id=aid)
    assert result["coverage"]["complete"]
    assert result["coverage"]["total_chunks"] == len(list(split(chapters))) > 0
    assert result["retrieval"] == "hybrid" and result["model"] == "text-embedding-3-small"
    by_revision = {c["revision_id"]: c for c in chapters}
    for chunk in result["chunks"]:
        assert chunk["section_id"] in by_revision
        assert chunk["document_id"] == by_revision[chunk["section_id"]]["document_revision_id"]
        values = struct.unpack("<64f", base64.b64decode(chunk["vector"]))
        assert math.isclose(sum(x * x for x in values), 1)
        assert chunk["content"] in by_revision[chunk["section_id"]]["content"]
    raw = json.dumps(result)
    assert "secret_ref" not in raw and "api.openai.com" not in raw and "used_tokens" not in raw
    assert "answer-model" not in raw and calls
    before = len(calls)
    fresh = InteractiveAssistant(service).rag
    assert fresh.export_index(owner, pid, analysis_set_id=aid) == result
    assert len(calls) == before


def test_subset_projection_excludes_unselected_revision_and_other_project_cache(vectors):
    _service, owner, pid, aid, rag, calls = vectors
    chapters, _, _ = rag.document_scope(owner, pid, analysis_set_id=aid)
    selected = chapters[0]["revision_id"]
    result = rag.export_index(owner, pid, analysis_set_id=aid, section_revision_ids=[selected])
    assert {c["section_id"] for c in result["chunks"]} == {selected}
    assert all(c["document_id"] == chapters[0]["document_revision_id"] for c in result["chunks"])
    # Unrelated cache rows (including identical IDs from another project) are
    # outside the SQL scope, before any data or similarity processing.
    _, _, space = rag.config(owner, pid)
    with rag.store.db() as db:
        db.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
            ("another-project", space, result["chunks"][0]["id"], selected, 0, "PRIVATE_OTHER_PROJECT", b"bad"),
        )
        db.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
            (pid, space, "other-revision", "unselected", 0, "PRIVATE_OTHER_REVISION", b"bad"),
        )
    assert rag.export_index(owner, pid, analysis_set_id=aid, section_revision_ids=[selected]) == result
    before = len(calls)
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, analysis_set_id=aid, section_revision_ids=["unselected"])
    assert exc.value.code == "VKS_NOT_FOUND" and len(calls) == before


def test_hidden_cached_chapters_and_missing_export_scope_fail_closed(vectors):
    service, owner, pid, aid, rag, calls = vectors
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "reader-export-snapshot")
    scope = {"release_id": release["id"]}
    all_vectors = rag.export_index(owner, pid, **scope)
    rid = all_vectors["chunks"][0]["section_id"]
    token = service.create_token(owner, pid, {"name": "reader", "scopes": ["knowledge:read", "knowledge:export"]})
    reader = service.authenticate(token["token"], "token")
    assert rag.export_index(reader, pid, **scope)["coverage"]["complete"]
    with service.db() as db:
        art = db.get(Artifact, db.get(Revision, rid).artifact_id)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    before = len(calls)
    result = rag.export_index(reader, pid, **scope)
    assert rid not in {c["section_id"] for c in result["chunks"]}
    with pytest.raises(DomainError) as exc:
        rag.export_index(reader, pid, section_revision_ids=[rid], **scope)
    assert exc.value.code == "VKS_NOT_FOUND" and len(calls) == before
    plain = service.create_token(owner, pid, {"name": "no-export", "scopes": ["knowledge:read"]})
    with pytest.raises(DomainError) as exc:
        rag.export_index(service.authenticate(plain["token"], "token"), pid, **scope)
    assert exc.value.status == 403


@pytest.mark.parametrize("mutation", ["selection", "model_policy", "model_settings"])
def test_corrupt_vector_rebuilt_and_revocation_during_network_does_not_commit(vectors, monkeypatch, mutation):
    service, owner, pid, aid, rag, calls = vectors
    result = rag.export_index(owner, pid, analysis_set_id=aid)
    with rag.store.db() as db:
        db.execute("UPDATE chunks SET embedding=? WHERE project=?", (b"bad", pid))
    before = len(calls)
    assert rag.export_index(owner, pid, analysis_set_id=aid) == result
    assert len(calls) > before
    with rag.store.db() as db:
        db.execute("DELETE FROM chunks WHERE project=?", (pid,))
    original = ai_gateway.request_model

    def revoke(config, payload):
        response = original(config, payload)
        if mutation == "model_settings":
            rag.save(owner, pid, RagSettings(enabled=True, dimensions=128))
        else:
            with service.db() as db:
                if mutation == "selection":
                    analysis = db.get(Analysis, aid)
                    analysis.selection_version += 1
                else:
                    policy = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
                    policy.settings = {**policy.settings, "allow_source": True}
        return response

    monkeypatch.setattr(ai_gateway, "request_model", revoke)
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, analysis_set_id=aid)
    assert exc.value.code == "VKS_VERSION_CONFLICT"
    with rag.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE project=?", (pid,)).fetchone()[0] == 0


def test_snapshot_uses_immutable_document_selection_and_empty_scope_needs_no_ai(vectors):
    service, owner, pid, aid, rag, calls = vectors
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "export-snapshot")
    result = rag.export_index(owner, pid, release_id=release["id"])
    assert result["coverage"]["complete"]
    before = len(calls)
    with service.db() as db:
        policy = db.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = {**policy.settings, "enabled": False}
    empty = rag.export_index(owner, pid, release_id=release["id"], section_revision_ids=[])
    assert empty["coverage"]["complete"] and not empty["chunks"] and len(calls) == before
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, release_id=release["id"])
    assert exc.value.code == "VKS_MODEL_NOT_CONFIGURED"


def test_export_refuses_missing_vectors_when_rag_disabled_and_does_not_send_forbidden_source(vectors):
    service, owner, pid, aid, rag, calls = vectors
    rag.save(owner, pid, RagSettings(enabled=False, dimensions=64))
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, analysis_set_id=aid)
    assert exc.value.code == "VKS_STATE_CONFLICT" and not calls
    preview = rag.export_index(owner, pid, analysis_set_id=aid, ensure=False)
    assert not preview["coverage"]["complete"] and preview["retrieval"] == "keyword"
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    with service.db() as db:
        section = db.scalar(
            select(Revision)
            .join(Selection, Selection.entity_revision_id == Revision.id)
            .join(Entity, Entity.id == Revision.entity_id)
            .where(Selection.analysis_set_id == aid, Entity.type == "section")
        )
        art = db.get(Artifact, section.artifact_id)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, analysis_set_id=aid)
    assert exc.value.code == "VKS_FORBIDDEN" and not calls


def test_background_doc_index_does_not_require_export_permission(vectors):
    service, owner, pid, aid, rag, calls = vectors
    token = service.create_token(owner, pid, {"name": "editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    editor = service.authenticate(token["token"], "token")
    coverage = rag.build_documents(editor, pid, analysis_set_id=aid)
    assert coverage["complete"] and calls
    assert rag.store.state(pid)["state"] == "ready"
    with pytest.raises(DomainError) as exc:
        rag.export_index(editor, pid, analysis_set_id=aid)
    assert exc.value.status == 403


@pytest.mark.parametrize("cancel", [False, True])
def test_install_plain_md_without_prototype_queues_and_fences_automatic_index(materials, monkeypatch, cancel):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    with service.db() as db:
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True, allow_external=True, endpoint="https://api.openai.com/v1", model_id="answers"
                ).model_dump(),
            )
        )
    rag = InteractiveAssistant(service).rag
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    staged = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 普通文档\n\n这是没有任何向量或脚本能力的 Markdown。"
    )
    installed = test_materials.activate(service, owner, manager, pid, aid, [staged])
    assert installed["job_id"]
    from vks_api.models import Job

    with service.db() as db:
        job = db.get(Job, installed["job_id"])
        assert job.input_json["index_documents"] and not job.input_json["index_package_id"]
    calls = []

    def provider(config, payload):
        calls.append(payload)
        if cancel:
            service.cancel_job(owner, installed["job_id"], "Cancel indexing during provider request")
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 63} for i in range(len(payload["input"]))],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    service.process_job(installed["job_id"])
    result = service.get_job(owner, installed["job_id"])
    with rag.store.db() as db:
        count = db.execute("SELECT COUNT(*) FROM chunks WHERE project=?", (pid,)).fetchone()[0]
    assert calls
    if cancel:
        assert result["state"] == "cancelled" and count == 0, result
    else:
        assert result["state"] == "succeeded" and count > 0, result
        exported = rag.export_index(owner, pid, analysis_set_id=aid)
        assert exported["coverage"]["complete"] and len(calls) == 1


def test_export_scope_callback_runs_inside_embedding_workers(vectors, monkeypatch):
    _service, owner, pid, aid, rag, calls = vectors
    cancelled = False
    checks = []

    def fence():
        checks.append(True)
        if cancelled:
            raise DomainError("VKS_CANCELLED", "Stopped by export job fence", 409)

    original = ai_gateway.request_model

    def provider(config, payload):
        nonlocal cancelled
        response = original(config, payload)
        cancelled = True
        return response

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    with pytest.raises(DomainError) as exc:
        rag.export_index(owner, pid, analysis_set_id=aid, validate_scope=fence)
    assert exc.value.code == "VKS_CANCELLED" and calls and len(checks) >= 3
    with rag.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE project=?", (pid,)).fetchone()[0] == 0


@pytest.mark.parametrize("cancel_module", [False, True])
def test_durable_document_build_preserves_module_prewarm_and_its_fence(interactive, monkeypatch, cancel_module):
    _web, service, owner, pid, aid, _grant, package, _section, _raw = interactive
    with service.db() as db:
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True, allow_external=True, endpoint="https://api.openai.com/v1", model_id="answers"
                ).model_dump(),
            )
        )
    rag = InteractiveAssistant(service).rag
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    cancelled, calls = False, []

    def fence():
        if cancelled:
            raise DomainError("VKS_CANCELLED", "Cancelled during module prewarm", 409)

    def provider(config, payload):
        nonlocal cancelled
        calls.append(payload)
        if any(text.startswith("模块：") for text in payload["input"]) and cancel_module:
            cancelled = True
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 63} for i in range(len(payload["input"]))],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", provider)
    form = AssistanceInput(
        analysis_set_id=aid,
        package_id=package,
        target={"route": "/page.html", "frame_path": ["actualPage"], "selector": "body", "label": "Synthetic module"},
        context={"scope": "module"},
    )
    if cancel_module:
        with pytest.raises(DomainError) as exc:
            rag.build_documents(owner, pid, analysis_set_id=aid, form=form, validate_scope=fence)
        assert exc.value.code == "VKS_CANCELLED"
        with rag.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM queries WHERE project=?", (pid,)).fetchone()[0] == 0
    else:
        assert rag.build_documents(owner, pid, analysis_set_id=aid, form=form, validate_scope=fence)["complete"]
        prepared = rag.assistant.prepare(owner, pid, form)
        before = len(calls)
        assert rag.hybrid(owner, pid, form, prepared[1], prepared[2], [])
        assert len(calls) == before
        with rag.store.db() as db:
            keys = {r[0] for r in db.execute("SELECT id FROM queries WHERE project=?", (pid,))}
        assert f"module:{prepared[2]}:{package}:/demo" in keys
        assert f"module:{prepared[2]}:{package}:/page.html" in keys


def test_document_only_http_status_durable_build_dedup_and_snapshot(vectors, workspace):
    service, owner, pid, aid, _rag, calls = vectors
    _app, web, *_ = workspace
    login = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
    assert login.status_code == 200
    headers = {"X-CSRF-Token": login.json()["data"]["csrf_token"], "Idempotency-Key": "build-documents"}
    prefix = f"/api/v3/projects/{pid}/interactive/rag/documents"
    before = web.post(prefix + "/status", json={"analysis_set_id": aid}, headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["data"]["total_sections"] > 0 and before.json()["data"]["indexed_chunks"] == 0
    first = web.post(prefix + "/build", json={"analysis_set_id": aid}, headers=headers)
    assert first.status_code == 200, first.text
    jid = first.json()["data"]["job_id"]
    repeated = web.post(prefix + "/build", json={"analysis_set_id": aid}, headers=headers)
    assert repeated.json()["data"] == first.json()["data"]
    dedup = web.post(
        prefix + "/build", json={"analysis_set_id": aid}, headers={**headers, "Idempotency-Key": "build-again"}
    )
    assert dedup.json()["data"]["job_id"] == jid and not dedup.json()["data"]["started"]
    pending = web.post(prefix + "/status", json={"analysis_set_id": aid}, headers=headers).json()["data"]
    assert pending["state"] == "building" and not calls
    service.process_job(jid)
    assert service.get_job(owner, jid)["state"] == "succeeded"
    after = web.post(prefix + "/status", json={"analysis_set_id": aid}, headers=headers).json()["data"]
    assert after["state"] == "ready" and after["indexed_sections"] == after["total_sections"] and calls
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "doc-rag-http-snapshot")
    frozen = web.post(prefix + "/status", json={"release_id": release["id"]}, headers=headers)
    assert frozen.status_code == 200 and frozen.json()["data"]["state"] == "ready"
    assert web.post(prefix + "/build", json={"release_id": release["id"]}, headers=headers).status_code == 409
    assert web.post(prefix + "/status", json={}, headers=headers).status_code == 422
    assert (
        web.post(
            prefix + "/status", json={"analysis_set_id": aid, "release_id": release["id"]}, headers=headers
        ).status_code
        == 422
    )
    readonly = service.create_token(owner, pid, {"name": "no-manage", "scopes": ["knowledge:read"]})
    assert (
        web.post(
            prefix + "/status", json={"release_id": release["id"]}, headers=_headers(readonly, 0, "denied")
        ).status_code
        == 403
    )
