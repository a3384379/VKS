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


def configure_synthetic_export_index(service, owner, pid, monkeypatch):
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
    monkeypatch.setattr(
        ai_gateway,
        "request_model",
        lambda _config, body: {
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * (body["dimensions"] - 1)} for i in range(len(body["input"]))
            ],
            "usage": {"total_tokens": 10},
        },
    )


@pytest.mark.parametrize("fmt", ["markdown_bundle", "pdf", "readonly_html"])
def test_release_document_export_needs_no_edit_permission_or_new_snapshot(materials, fmt, monkeypatch):
    from vks_api.models import Release
    from vks_api.project_tools import ProjectTools, WorkspaceExport

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    configure_synthetic_export_index(service, owner, pid, monkeypatch)
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# 可导出的文档\n\n选定版本的正文。")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    release = manager.checkpoint(owner, pid, aid, "导出权限基线", "read-export-baseline")
    document = InteractiveKnowledgeService(service).catalog(owner, pid, release_id=release["id"])["documents"][0]
    token = service.create_token(owner, pid, {"name": "export-only", "scopes": ["knowledge:read", "knowledge:export"]})
    reader = service.authenticate(token["token"], "token")
    with service.db() as s:
        before = (set(s.scalars(select(Analysis.id))), set(s.scalars(select(Release.id))))
    tools = ProjectTools(service)
    request = WorkspaceExport(release_id=release["id"], document_ids=[document["id"]], format=fmt)
    job = tools.export(reader, pid, request, "export-only-" + fmt)
    assert tools.export(reader, pid, request, "export-only-" + fmt)["id"] == job["id"]
    service.process_job(job["id"])
    outcome = service.get_job(reader, job["id"])
    assert outcome["state"] == "succeeded", json.dumps(outcome, ensure_ascii=False)
    assert service.download_export(reader, pid, job["id"])[0]
    with service.db() as s:
        assert before == (set(s.scalars(select(Analysis.id))), set(s.scalars(select(Release.id))))


@pytest.mark.parametrize("baseline", ["draft", "release"])
def test_selected_document_export_keeps_updated_attachment_without_other_documents(materials, baseline, monkeypatch):
    import base64
    import gzip
    import re
    from io import BytesIO
    from zipfile import ZipFile

    from PIL import Image
    from vks_api.project_tools import ProjectTools, WorkspaceExport

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    configure_synthetic_export_index(service, owner, pid, monkeypatch)

    def package(color):
        image, archive = BytesIO(), BytesIO()
        Image.new("RGB", (3, 2), color).save(image, "PNG")
        with ZipFile(archive, "w") as files:
            files.writestr("guide.md", "# Selected\n\n![selected diagram](assets/flow.png)")
            files.writestr("other.md", "# Other\n\nUNSELECTED_DOCUMENT_MUST_NOT_BE_EXPORTED")
            files.writestr("assets/flow.png", image.getvalue())
        return archive.getvalue(), image.getvalue()

    first, _old_image = package("red")
    initial = test_materials.stage(service, owner, manager, pid, aid, "documents", first, filename="docs.zip")
    test_materials.activate(service, owner, manager, pid, aid, [initial])
    links = InteractiveKnowledgeService(service)
    previous = next(row for row in links.catalog(owner, pid, aid)["documents"] if row["name"] == "Selected")
    second, image = package("blue")
    updated = test_materials.stage(
        service, owner, manager, pid, aid, "documents", second, filename="docs.zip", package_id=initial["package_id"]
    )
    test_materials.activate(service, owner, manager, pid, aid, [updated])
    current = next(row for row in links.catalog(owner, pid, aid)["documents"] if row["name"] == "Selected")
    assert previous["revision_id"] == current["revision_id"]
    scope = {"analysis_set_id": aid}
    if baseline == "release":
        scope = {"release_id": manager.checkpoint(owner, pid, aid, "附件版本基线", "asset-baseline")["id"]}
    job = ProjectTools(service).export(
        owner, pid, WorkspaceExport(**scope, document_ids=[current["id"]]), "selected-asset-" + baseline
    )
    service.process_job(job["id"])
    outcome = service.get_job(owner, job["id"])
    assert outcome["state"] == "succeeded", json.dumps(outcome, ensure_ascii=False)
    raw, _ = service.download_export(owner, pid, job["id"])
    packed = re.search(rb'<script\b[^>]*\bid="vks-payload"[^>]*>([^<]+)</script>', raw)
    payload = json.loads(gzip.decompress(base64.b64decode(packed[1].strip())))
    assert len(payload["documents"]) == 1
    assert "UNSELECTED_DOCUMENT_MUST_NOT_BE_EXPORTED" not in json.dumps(payload)
    images = payload["documents"][0]["images"]
    assert images and all(base64.b64decode(value.split(",", 1)[1]) == image for value in images.values())


@pytest.mark.parametrize(
    "text",
    [
        "能不能不要重新关联，只解释一下这个按钮的作用？",
        "可以不要修改文档吗？",
        "请解释如何修改文档",
        "材料里写着请修改文档",
        "请把当前文档不要导出",
        "可以先只解释重新关联的效果吗？",
        "重新关联当前控件会覆盖已有说明吗？",
        "修改当前文档会保留历史版本吗",
        "导出整个项目会包含什么？",
    ],
)
def test_conversation_read_requests_leave_the_selection_and_jobs_unchanged(assistant, text):
    from vks_api.models import Job, Release

    (web, service, _owner, pid, aid, grant, *_), body, _calls = assistant

    def state():
        with service.db() as s:
            return (
                s.get(Analysis, aid).selection_version,
                *[
                    list(s.scalars(select(row.id).where(row.project_id == pid)))
                    for row in (Revision, Relation, Release, Job)
                ],
            )

    before = state()
    response = web.post(
        root(pid) + "/conversation",
        json=question(body, text),
        headers=_headers(grant, 0, "read-only-chat"),
    )
    assert response.status_code == 200, response.text
    assert not response.json()["data"]["operations"]
    assert state() == before


def test_document_export_scope_matches_exact_names_and_requires_explicit_project():
    from vks_api.conversation_tools import export_scope
    from vks_api.service import DomainError

    short = {"id": "short", "name": "收费", "revision_id": "short-rev"}
    long = {"id": "long", "name": "收费规则", "revision_id": "long-rev"}
    documents = [short, long]
    assert export_scope("请只导出《收费规则》为 Markdown", documents, None) == ("documents", ["long"])
    assert export_scope("请将当前文档导出为 PDF", documents, "short-rev") == ("documents", ["short"])
    assert export_scope("导出全项目为 VKSBundle", documents, None) == ("project", [])
    assert export_scope("请导出收费规则为 Markdown", documents, None) == ("documents", ["long"])
    for request in ("导出《不存在的文档》为 Markdown", "导出当前文档", "导出 Markdown"):
        with pytest.raises(DomainError):
            export_scope(request, documents, None)
    with pytest.raises(DomainError, match="同名"):
        export_scope("导出《收费规则》", [*documents, {**long, "id": "other", "revision_id": "other-rev"}], None)
    with pytest.raises(DomainError):
        export_scope("只导出项目资料为 PDF", documents, None)
    collision = {"id": "same-name", "name": "全项目", "revision_id": "same-name-rev"}
    with pytest.raises(DomainError, match="同名"):
        export_scope("导出全项目为 PDF", [collision], None)
    assert export_scope("导出《全项目》为 PDF", [collision], None) == ("documents", ["same-name"])


def test_document_title_does_not_enqueue_project_export(materials):
    from vks_api.models import Job

    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    packs = [
        test_materials.stage(
            service,
            owner,
            manager,
            pid,
            aid,
            "documents",
            f"# {name}\n\n合成测试正文。",
            name=name,
            filename=f"guide-{i}.md",
        )
        for i, name in enumerate(("项目资料", "范围外文档"))
    ]
    test_materials.activate(service, owner, manager, pid, aid, packs)
    release = manager.checkpoint(owner, pid, aid, "导出范围复审", "title-collision-baseline")
    docs = InteractiveKnowledgeService(service).catalog(owner, pid, release_id=release["id"])["documents"]
    expected = next(doc["id"] for doc in docs if doc["name"] == "项目资料")
    token = service.create_token(owner, pid, {"name": "审计导出", "scopes": ["knowledge:read", "knowledge:export"]})
    result = web.post(
        root(pid) + "/conversation",
        json={
            "question": "只导出项目资料为 PDF",
            "release_id": release["id"],
            "target": {"route": "/", "frame_path": [], "selector": "body", "label": "文档"},
        },
        headers=_headers(token, 0, "title-collision"),
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["title"] == "文档导出"
    with service.db() as s:
        job = s.get(Job, data["operations"][0]["job_id"])
        assert job.input_json["document_ids"] == [expected]


def test_concurrent_conversation_retries_share_one_model_call(assistant, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    from fastapi.testclient import TestClient
    from vks_api import conversation_tools
    from vks_api.app import create_app

    (web, service, _owner, pid, aid, grant, *_), body, calls = assistant
    other_app = create_app(service.data_dir, enable_worker=False)
    generating, waiting, release_model = Event(), Event(), Event()
    request_model = ai_gateway.request_model

    def pause_model(config, request):
        generating.set()
        assert release_model.wait(8), "合成模型测试门闩超时"
        return request_model(config, request)

    def wait_for_plan(seconds):
        waiting.set()
        sleep(seconds)

    monkeypatch.setattr(ai_gateway, "request_model", pause_model)
    monkeypatch.setattr(conversation_tools, "sleep", wait_for_plan)
    headers = _headers(grant, 0, "concurrent-reassociation")
    payload = question(body, "请重新关联，说明办理条件")
    path = root(pid) + "/conversation"
    try:
        with TestClient(other_app) as other, ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(web.post, path, json=payload, headers=headers)
            try:
                assert generating.wait(5)
                second = executor.submit(other.post, path, json=payload, headers=headers)
                assert waiting.wait(5)
                conflict = other.post(path, json=question(body, "请重新关联，改写另一条说明"), headers=headers)
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "VKS_IDEMPOTENCY_CONFLICT"
            finally:
                release_model.set()
            replies = [first.result(timeout=8), second.result(timeout=8)]
        assert [reply.status_code for reply in replies] == [200, 200], [reply.text for reply in replies]
        assert replies[0].json()["data"] == replies[1].json()["data"]
        links = web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=headers).json()["data"]
        assert len(calls) == len(links) == 1
    finally:
        other_app.state.service.engine.dispose()


def test_conversation_retry_can_recover_an_abandoned_generation(assistant):
    from vks_api.interactive_ai import InteractiveQuestionInput
    from vks_api.models import Idempotency
    from vks_api.service import canonical, sha

    (web, service, _owner, pid, _aid, grant, *_), body, calls = assistant
    actor = service.authenticate(grant["token"], "token")
    payload = question(body, "请重新关联，说明办理条件")
    key = "expired-reassociation"
    with service.db() as s:
        s.add(
            Idempotency(
                project_id=pid,
                actor_id=actor.id,
                operation="interactive.binding.chat.plan",
                key=key,
                payload_hash=sha(canonical(InteractiveQuestionInput.model_validate(payload).model_dump())),
                response_json={"attempt": "old-worker", "expires_at": 0},
            )
        )
    result = web.post(root(pid) + "/conversation", json=payload, headers=_headers(grant, 0, key))
    assert result.status_code == 200, result.text
    assert result.json()["data"]["saved"] and len(calls) == 1
    with service.db() as s:
        plan = s.scalar(
            select(Idempotency).where(
                Idempotency.operation == "interactive.binding.chat.plan",
                Idempotency.actor_id == actor.id,
                Idempotency.key == key,
            )
        )
        assert plan.response_json["preview_id"]
        assert "attempt" not in plan.response_json


def test_reassociation_retries_the_persisted_plan_when_receipt_write_fails(assistant, monkeypatch):
    from vks_api.service import DomainError

    (web, service, _owner, pid, aid, grant, *_), body, calls = assistant
    request = question(body, "请重新关联，重点说明办理条件")
    idem = service.idem

    def fail(s, actor, project, operation, key, payload, create):
        if operation == "interactive.binding.chat":
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "synthetic pre-commit failure", 503)
        return idem(s, actor, project, operation, key, payload, create)

    headers = _headers(grant, 0, "retry-reassociation")
    with monkeypatch.context() as patch:
        patch.setattr(service, "idem", fail)
        first = web.post(root(pid) + "/conversation", json=request, headers=headers)
        assert first.status_code == 503, first.text
    assert web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=headers).json()["data"] == []
    assert len(calls) == 1
    retry = web.post(root(pid) + "/conversation", json=request, headers=headers)
    assert retry.status_code == 200, retry.text
    saved = binding(web, pid, aid, grant)
    replay = web.post(root(pid) + "/conversation", json=request, headers=headers)
    assert replay.json()["data"] == retry.json()["data"]
    assert binding(web, pid, aid, grant)["point_revision_id"] == saved["point_revision_id"]
    assert len(calls) == 1
    conflict = web.post(
        root(pid) + "/conversation", json=question(body, "请重新关联，说明另一种办理条件"), headers=headers
    )
    assert conflict.status_code == 409 and len(calls) == 1
