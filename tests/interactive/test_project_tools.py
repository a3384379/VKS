"""Shared conversation/MCP tools: exact revisions, preserved history and fresh grants."""

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.mcp_gateway import APIBridge, create_server
from vks_api.models import Analysis, Artifact, Relation, Revision

from tests.interactive import test_assistant, test_knowledge, test_materials
from tests.interactive.test_knowledge import current, form, root
from tests.prototype.test_v3_workflow import _headers

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
assistant = test_assistant.assistant
materials = test_materials.materials


def binding(web, pid, aid, grant):
    return web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")).json()[
        "data"
    ][0]


def test_guided_reassociation_can_rewrite_manual_and_preserves_old_revision(assistant):
    (web, service, _owner, pid, aid, grant, package, section, _), body, calls = assistant
    v = current(web, pid, aid, grant)["selection_version"]
    web.post(root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, v, "manual"))
    old = binding(web, pid, aid, grant)
    guided = {
        **body,
        "instruction": "重点说明适用条件，保留完整业务讲解",
        **{k: old[k] for k in ("binding_version", "point_revision_id")},
        "binding_id": old["id"],
    }
    response = web.post(root(pid) + "/reassociate", json=guided, headers=_headers(grant, 0, "guided"))
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert not preview["saved"] and preview["can_confirm"]
    assert binding(web, pid, aid, grant) == old
    confirmed = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": preview["preview_id"]},
        headers=_headers(grant, 0, "confirm-guided"),
    )
    assert confirmed.status_code == 200 and confirmed.json()["data"]["saved"], confirmed.text
    new = binding(web, pid, aid, grant)
    assert new["id"] == old["id"] and new["point_revision_id"] != old["point_revision_id"]
    assert calls[-1]["writing_instruction"] == guided["instruction"]
    assert calls[-1]["existing_binding"]["summary"] == old["summary"]
    with service.db() as s:
        assert s.get(Revision, old["point_revision_id"]).content == old["summary"]
    assert web.post(root(pid) + "/reassociate", json=guided, headers=_headers(grant, 0, "stale")).status_code == 412
    assert len(calls) == 1


def test_guided_rewrite_wait_does_not_reduce_output_or_change_the_prompt(assistant, monkeypatch):
    (web, service, _owner, pid, _aid, grant, _package, _section, _), body, _calls = assistant
    with service.db() as s:
        policy = s.scalar(select(ai_gateway.ModelPolicy).where(ai_gateway.ModelPolicy.project_id == pid))
        policy.settings = {**policy.settings, "max_output_tokens": 8000}
    seen = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        seen.append((config, payload, data))
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(
        root(pid) + "/reassociate",
        json={**body, "instruction": "保留业务细节并说明前提"},
        headers=_headers(grant, 0, "guided-wait"),
    )
    assert result.status_code == 200 and result.json()["data"]["can_confirm"] and not result.json()["data"]["saved"]
    config, payload, data = seen[0]
    assert config["_request_timeout_seconds"] == 120 and payload["max_output_tokens"] == 8000
    assert data["writing_instruction"] == "保留业务细节并说明前提"


@pytest.mark.parametrize("change", ["manual", "removed", "policy", "frozen"])
def test_guided_result_cannot_overwrite_concurrent_changes(assistant, monkeypatch, change):
    (web, service, owner, pid, aid, grant, package, section, _), body, _ = assistant
    old = None
    if change in {"manual", "removed"}:
        web.post(
            root(pid) + "/bindings",
            json=form(aid, package, section),
            headers=_headers(grant, current(web, pid, aid, grant)["selection_version"], "initial"),
        )
        old = binding(web, pid, aid, grant)

    def model(_config, payload):
        if change == "manual":
            updated = {
                **form(aid, package, section),
                "point": {"title": "Human", "summary": "Concurrent manual change"},
                "binding_id": old["id"],
                **{k: old[k] for k in ("binding_version", "point_revision_id")},
            }
            InteractiveKnowledgeService(service).save(
                owner,
                pid,
                InteractiveBinding.model_validate(updated),
                current(web, pid, aid, grant)["selection_version"],
                "concurrent",
            )
        else:
            with service.db() as s:
                if change == "removed":
                    s.get(Relation, old["id"]).excluded = True
                elif change == "frozen":
                    s.get(Analysis, aid).state = "frozen"
                else:
                    policy = s.scalar(select(ai_gateway.ModelPolicy).where(ai_gateway.ModelPolicy.project_id == pid))
                    policy.settings = {**policy.settings, "allow_external": False}
        data = json.loads(payload["input"][1]["content"])
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    guided = {
        **body,
        "instruction": "重新编写说明",
        **({"binding_id": old["id"], **{k: old[k] for k in ("binding_version", "point_revision_id")}} if old else {}),
    }
    result = web.post(root(pid) + "/reassociate", json=guided, headers=_headers(grant, 0, "guided"))
    assert result.status_code in {409, 412, 503}, result.text
    if change == "manual":
        assert binding(web, pid, aid, grant)["summary"] == "Concurrent manual change"


def question(body, text):
    return {**body, "question": text, "history": []}


def test_conversation_binding_write_and_howto_does_not_write(assistant):
    (web, _service, _owner, pid, aid, grant, _, _, _), body, calls = assistant
    r = web.post(
        root(pid) + "/conversation",
        json=question(body, "请重新关联，重点说明办理前提和结果"),
        headers=_headers(grant, 0, "chat-bind"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["operations"][0]["kind"] == "binding"
    old = binding(web, pid, aid, grant)
    repeated = web.post(
        root(pid) + "/conversation",
        json=question(body, "请重新关联，重点说明办理前提和结果"),
        headers=_headers(grant, 0, "chat-bind"),
    )
    assert repeated.status_code == 200 and len(calls) == 1
    r = web.post(
        root(pid) + "/conversation", json=question(body, "怎么修改关联文案？"), headers=_headers(grant, 0, "chat-how")
    )
    assert r.status_code == 200, r.text
    assert not r.json()["data"]["operations"]
    assert binding(web, pid, aid, grant)["point_revision_id"] == old["point_revision_id"]


def test_conversation_document_patch_preserves_others_and_is_idempotent(assistant, monkeypatch):
    (web, service, owner, pid, aid, grant, _, _, _), body, _ = assistant
    catalog = current(web, pid, aid, grant)
    doc = catalog["documents"][0]
    old = service.get_document(owner, doc["id"], doc["revision_id"])
    sent = []

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        sent.append(data)
        section = data["document_sections"][0]
        answer = {
            "patches": [
                {
                    "section_revision_id": section["section_revision_id"],
                    "markdown": section["markdown"] + "\n\n用户新增的准确说明。",
                }
            ],
            "reason": "按用户要求补充",
        }
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 80},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
        }

    monkeypatch.setattr(ai_gateway, "request_model", model)
    body = {**body, "package_id": None, "document_revision_id": doc["revision_id"]}
    q = question(body, "请修改当前文档，补充：用户新增的准确说明。")
    result = web.post(root(pid) + "/conversation", json=q, headers=_headers(grant, 0, "doc-edit"))
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["operations"][0]["kind"] == "document" and data["source_fingerprint"] != catalog["source_fingerprint"]
    new_id = data["operations"][0]["revision_id"]
    new = service.get_document(owner, doc["id"], new_id)
    assert "用户新增的准确说明" in new["markdown"]
    for section in old["sections"][1:]:
        assert section["content"] in new["markdown"]
    assert service.get_document(owner, doc["id"], doc["revision_id"])["markdown"] == old["markdown"]
    repeat = web.post(root(pid) + "/conversation", json=q, headers=_headers(grant, 0, "doc-edit"))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["operations"][0]["revision_id"] == new_id and len(sent) == 1
    changed = web.post(
        root(pid) + "/conversation",
        json={**q, "question": "请修改当前文档成别的"},
        headers=_headers(grant, 0, "doc-edit"),
    )
    assert changed.status_code == 409, changed.text


@pytest.mark.parametrize("kind", ["locked", "foreign", "truncated", "concurrent", "revoke"])
def test_conversation_document_rejects_invalid_or_racing_edits(assistant, monkeypatch, kind):
    (web, service, owner, pid, aid, grant, _, _, _), body, _ = assistant
    doc = current(web, pid, aid, grant)["documents"][0]
    original = service.get_document(owner, doc["id"], doc["revision_id"])
    if kind == "locked":
        locked = service.save_document(
            owner,
            doc["id"],
            {
                "analysis_set_id": aid,
                "base_revision_id": doc["revision_id"],
                "markdown": original["markdown"],
                "locked_section_ids": [original["sections"][0]["entity_id"]],
            },
            original["row_version"],
        )
        doc = {**doc, "revision_id": locked["entity_revision_id"]}

    def model(config, payload):
        data = json.loads(payload["input"][1]["content"])
        rid = data["document_sections"][0]["section_revision_id"]
        if kind == "concurrent":
            service.save_document(
                owner,
                doc["id"],
                {
                    "analysis_set_id": aid,
                    "base_revision_id": doc["revision_id"],
                    "markdown": original["markdown"] + "\n\nConcurrent human content",
                    "locked_section_ids": [],
                },
                original["row_version"],
            )
        if kind == "revoke":
            with service.db() as s:
                art = s.get(Artifact, s.get(Revision, doc["revision_id"]).artifact_id)
                art.metadata_json = {**art.metadata_json, "requires_source_read": True}
        answer = {
            "patches": [
                {
                    "section_revision_id": "foreign-id" if kind == "foreign" else rid,
                    "markdown": "# Replaced\n\nMachine replacement",
                }
            ],
            "reason": "changed",
        }
        return {
            "status": "incomplete" if kind == "truncated" else "completed",
            "usage": {"input_tokens": 100, "output_tokens": 80},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
        }

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(
        root(pid) + "/conversation",
        json=question({**body, "package_id": None, "document_revision_id": doc["revision_id"]}, "请修改当前文档正文"),
        headers=_headers(grant, 0, "edit-invalid"),
    )
    assert result.status_code in {403, 404, 409, 412, 422}, result.text
    latest = service.workspace(owner, pid, aid)
    selected = next(r for r in latest["entities"] if r["type"] == "document" and r["entity_id"] == doc["id"])
    new = service.get_document(owner, doc["id"], selected["entity_revision_id"])
    assert "Machine replacement" not in new["markdown"]


@pytest.mark.asyncio
async def test_sdk_new_tools_read_write_export_conflict_and_revoke(interactive):
    web, service, owner, pid, aid, _grant, package, section, _ = interactive
    token = service.create_token(
        owner,
        pid,
        {"name": "project tools", "scopes": ["knowledge:read", "knowledge:edit", "knowledge:export", "source:read"]},
    )
    server = create_server(APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=web.app)))
    async with create_connected_server_and_client_session(server) as sdk:

        async def call(name, args):
            result = await sdk.call_tool(name, {"project_id": pid, **args})
            assert not result.isError, result.content
            return result.structuredContent["data"]

        body = {k: v for k, v in form(aid, package, section).items() if k not in {"point", "section_revision_ids"}}
        context = await call("get_interactive_context", body)
        write = {
            **form(aid, package, section),
            "selection_version": context["catalog"]["selection_version"],
            "idempotency_key": "sdk-bind",
        }
        saved = await call("save_interactive_binding", write)
        assert (await call("save_interactive_binding", write)) == saved
        stale = await sdk.call_tool(
            "save_interactive_binding", {"project_id": pid, **write, "idempotency_key": "new-stale"}
        )
        assert stale.isError
        doc = context["catalog"]["documents"][0]
        read = await call(
            "get_workspace_document",
            {"analysis_set_id": aid, "document_id": doc["id"], "revision_id": doc["revision_id"]},
        )
        update = {
            "analysis_set_id": aid,
            "document_id": doc["id"],
            "base_revision_id": doc["revision_id"],
            "row_version": read["row_version"],
            "markdown": read["markdown"] + "\n\nSDK text",
            "idempotency_key": "sdk-doc",
        }
        new = await call("save_workspace_document", update)
        assert (await call("save_workspace_document", update))["entity_revision_id"] == new["entity_revision_id"]
        export = await call(
            "export_workspace_documents",
            {"analysis_set_id": aid, "format": "markdown_bundle", "idempotency_key": "sdk-export"},
        )
        service.process_job(export["id"])
        job = await call("get_job", {"job_id": export["id"]})
        assert job["state"] == "succeeded", job
        assert (
            await call(
                "export_workspace_documents",
                {"analysis_set_id": aid, "format": "markdown_bundle", "idempotency_key": "sdk-export"},
            )
        )["id"] == export["id"]
        with service.db() as s:
            assert s.get(Analysis, aid).state != "frozen"
        # Current permission changes must apply even to idempotent replays.
        service.revoke_token(owner, pid, token["id"])
        denied = await sdk.call_tool("save_workspace_document", {"project_id": pid, **update})
        assert denied.isError


@pytest.mark.parametrize(
    "text,expected",
    [
        ("请修改文档中的导出说明", "document"),
        ("请帮我分析导出慢的原因", None),
        ("请不要导出项目", None),
        ("补充后的文档说明是什么？", None),
        ("文档说“请导出全部资料”，这是什么意思？", None),
        ("请把关联文案改为重点解释退款条件", "binding"),
        ("请将当前文档导出为 PDF", "export"),
        ("请修改当前文档然后导出 PDF", "multiple"),
    ],
)
def test_only_current_explicit_operation_enables_tools(text, expected):
    from vks_api.conversation_tools import intent

    assert intent(text) == expected


@pytest.mark.parametrize("fmt", ["markdown_bundle", "pdf", "readonly_html"])
def test_scoped_document_exports_do_not_include_other_document(materials, fmt, monkeypatch):
    import base64
    import gzip
    import re
    from io import BytesIO
    from zipfile import ZipFile

    from vks_api.project_tools import ProjectTools, WorkspaceExport

    from tests.interactive import test_materials
    from tests.interactive.test_document_navigation import zip_docs

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs(
            [
                ("first.md", "# Export First\n\nFIRST_ONLY_MARKER"),
                ("second.md", "# Export Second\n\nSECOND_MUST_BE_ABSENT"),
            ]
        ),
        filename="two-documents.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    doc = next(d for d in catalog["documents"] if d["name"] == "Export First")
    if fmt == "readonly_html":
        from vks_api.ai_gateway import ModelPolicy, PolicyInput
        from vks_api.interactive_ai import InteractiveAssistant
        from vks_api.models import uid
        from vks_api.vector_rag import RagSettings

        with service.db() as s:
            s.add(
                ModelPolicy(
                    id=uid(),
                    project_id=pid,
                    settings=PolicyInput(
                        enabled=True, allow_external=True, model_id="synthetic", endpoint="https://api.openai.com/v1"
                    ).model_dump(),
                )
            )
        InteractiveAssistant(service).rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))

        def embeddings(config, body):
            return {
                "data": [
                    {"index": i, "embedding": [1.0] + [0.0] * (body["dimensions"] - 1)}
                    for i in range(len(body["input"]))
                ],
                "usage": {"total_tokens": 10},
            }

        monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    job = ProjectTools(service).export(
        owner, pid, WorkspaceExport(analysis_set_id=aid, document_ids=[doc["id"]], format=fmt), "document-" + fmt
    )
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded", json.dumps(result)
    raw, _ = service.download_export(owner, pid, job["id"])
    if fmt == "markdown_bundle":
        with ZipFile(BytesIO(raw)) as archive:
            text = "\n".join(archive.read(n).decode() for n in archive.namelist())
            assert len([n for n in archive.namelist() if n.startswith("knowledge/")]) == 1
    elif fmt == "pdf":
        from pypdf import PdfReader

        text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(raw)).pages)
    else:
        packed = re.search(rb'<script\b[^>]*\bid="vks-payload"[^>]*>([^<]+)</script>', raw)
        payload = json.loads(gzip.decompress(base64.b64decode(packed[1].strip())))
        assert len(payload["documents"]) == 1 and not payload["prototypes"]
        text = json.dumps(payload)
    assert "FIRST_ONLY_MARKER" in text and "SECOND_MUST_BE_ABSENT" not in text
    with service.db() as s:
        assert s.get(Analysis, aid).state != "frozen"
