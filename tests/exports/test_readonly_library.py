"""A portable reader keeps the same exact, current permission closure as Studio."""

import base64
import gzip
import json
import re
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.documents import DocumentService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.interactive_ai import InteractiveAssistant
from vks_api.materials import ROOT
from vks_api.models import Analysis, Artifact, Entity, Evidence, Job, Relation, Release, Revision, Selection, uid
from vks_api.prototype_edit import _select
from vks_api.readonly_export import projection
from vks_api.service import DomainError
from vks_api.vector_rag import RagSettings

from tests.interactive import test_materials

materials = test_materials.materials


@pytest.fixture
def library(materials, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, document = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    calls = []

    def embeddings(config, body):
        assert config["protocol"] == "embeddings"
        calls.append(body)
        return {
            "data": [
                {"index": i, "embedding": [1.0] + [0.0] * (body["dimensions"] - 1)} for i in range(len(body["input"]))
            ],
            "usage": {"total_tokens": len(body["input"]) * 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    monkeypatch.setenv("VKS_MODEL_API_KEY", "PUBLISHER_KEY_MUST_NEVER_ENTER_EXPORT")
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True,
                    allow_external=True,
                    allow_source=True,
                    endpoint="https://publisher-provider.invalid/private-model-gateway",
                    secret_ref="model-default",
                    model_id="publisher-answer-model",
                ).model_dump(),
            )
        )
    InteractiveAssistant(service).rag.save(owner, pid, RagSettings(enabled=True, dimensions=64))
    return {
        "web": web,
        "service": service,
        "owner": owner,
        "manager": manager,
        "pid": pid,
        "aid": aid,
        "prototype": prototype,
        "document": document,
        "calls": calls,
    }


def snapshot(ctx):
    return ctx["manager"].checkpoint(ctx["owner"], ctx["pid"], ctx["aid"], "固定的成品版本", uid())


def reader(ctx, scopes=("knowledge:read", "knowledge:export")):
    service = ctx["service"]
    token = service.create_token(ctx["owner"], ctx["pid"], {"name": "成品库读者", "scopes": list(scopes)})
    return service.authenticate(token["token"], "token")


def export(ctx, fixed, *, actor=None, include_source=False):
    service, actor = ctx["service"], actor or ctx["owner"]
    job = service.export(
        actor,
        ctx["pid"],
        {"release_id": fixed["id"], "format": "readonly_html", "include_source": include_source},
        uid(),
    )
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded", result
    raw, name = service.download_export(actor, ctx["pid"], job["id"])
    match = re.search(rb'<script\b[^>]*\bid="vks-payload"[^>]*>([^<]+)</script>', raw)
    assert match, "The single HTML must contain its own inert data payload"
    payload = json.loads(gzip.decompress(base64.b64decode(match[1].strip(), validate=True)))
    return payload, raw, name, job


def declare_original_members(ctx):
    """Give the source package an immutable closure, then edit outside that closure.

    A synthetic package declaration exercises the raw-package boundary without
    depending on a proprietary source archive or executing its scripts.
    """
    service = ctx["service"]
    with service.db() as s:
        prior = s.get(Revision, ctx["prototype"]["revision_id"])
        document = s.get(Revision, ctx["document"]["revision_id"])
        # ctx[document] is a package revision; its member revisions are the
        # exact document and chapters that its original bytes described.
        members = list(document.metadata_json[ROOT]["members"])
        metadata = deepcopy(prior.metadata_json)
        metadata[ROOT]["members"] = members
        updated = DocumentService(service)._revision(
            s,
            s.get(Entity, prior.entity_id),
            s.get(Artifact, prior.artifact_id),
            prior.name,
            prior.content,
            prior.locator,
            metadata,
        )
        analysis = s.get(Analysis, ctx["aid"])
        _select(s, analysis, updated)
        analysis.selection_version += 1
        return members


def edit_document(ctx, markdown):
    service, owner, pid, aid = (ctx[k] for k in ("service", "owner", "pid", "aid"))
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    current = catalog["documents"][0]
    return DocumentService(service).save_draft(
        owner,
        current["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": current["revision_id"],
            "markdown": markdown,
            "locked_section_ids": [],
        },
        1,
    )


def test_generic_html_and_markdown_export_as_self_contained_exact_library(library):
    ctx = library
    fixed = snapshot(ctx)
    actor = reader(ctx)
    payload, raw, name, job = export(ctx, fixed, actor=actor)
    assert name.endswith("-readonly.html") and raw.startswith(b"<!doctype html>")
    assert payload["snapshot"]["id"] == fixed["id"]
    assert [p["id"] for p in payload["prototypes"]] == [ctx["prototype"]["artifact_id"]]
    assert payload["prototypes"][0]["format"] == "html"
    assert "新增收费项需要填写名称" in payload["documents"][0]["markdown"]
    sections = {section["id"]: section for doc in payload["documents"] for section in doc["sections"]}
    assert payload["rag"]["coverage"]["complete"] and ctx["calls"]
    assert {c["section_id"] for c in payload["rag"]["chunks"]} == set(sections)
    assert all(chunk["content"] in sections[chunk["section_id"]]["content"] for chunk in payload["rag"]["chunks"])
    assert payload["bindings"][0]["section_ids"]
    assert all(set(binding["section_ids"]) <= sections.keys() for binding in payload["bindings"])
    assert "必须保留的人工说明" in json.dumps(payload, ensure_ascii=False)
    rendered = raw.decode()
    assert "VKS_READER_PAYLOAD" not in rendered and "/* VKS_READER_JS */" not in rendered
    assert "PUBLISHER_KEY_MUST_NEVER_ENTER_EXPORT" not in rendered + json.dumps(payload)
    assert "publisher-provider.invalid" not in json.dumps(payload)
    assert "model-default" not in json.dumps(payload) and "publisher-answer-model" not in json.dumps(payload)
    with ctx["service"].db() as s:
        stored = s.get(Job, job["id"])
        assert ctx["prototype"]["artifact_id"] in stored.input_json["_export_artifact_ids"]
        assert set(sections) <= set(stored.input_json["_export_revision_ids"])


def test_source_opt_in_is_honored_for_runtime_bytes_and_associations(library):
    ctx = library
    fixed = snapshot(ctx)
    with ctx["service"].db() as s:
        art = s.get(Artifact, ctx["prototype"]["artifact_id"])
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
    public, *_ = export(ctx, fixed, include_source=False)
    assert public["prototypes"] == [] and public["bindings"] == []
    assert public["documents"] and public["rag"]["coverage"]["complete"]
    private, *_ = export(ctx, fixed, include_source=True)
    assert len(private["prototypes"]) == 1 and private["bindings"]
    with pytest.raises(DomainError) as exc:
        export(ctx, fixed, actor=reader(ctx), include_source=True)
    assert exc.value.status == 403


def test_package_with_hidden_original_member_is_omitted_before_names_and_vectors(library):
    ctx = library
    members = declare_original_members(ctx)
    extra = test_materials.stage(
        ctx["service"],
        ctx["owner"],
        ctx["manager"],
        ctx["pid"],
        ctx["aid"],
        "documents",
        "# 独立可见文档\n\n读者可以看到此规则。",
        name="可见的文档包",
        filename="visible.md",
    )
    test_materials.activate(ctx["service"], ctx["owner"], ctx["manager"], ctx["pid"], ctx["aid"], [extra])
    fixed = snapshot(ctx)
    with ctx["service"].db() as s:
        original = s.get(Artifact, s.get(Revision, members[0]).artifact_id)
        original.metadata_json = {**original.metadata_json, "requires_source_read": True}
    payload, *_ = export(ctx, fixed, actor=reader(ctx))
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["prototypes"] == [] and payload["bindings"] == []
    assert "独立可见文档" in text and "读者可以看到此规则" in text
    assert "新增收费项需要填写名称" not in text and "必须保留的人工说明" not in text
    assert ctx["prototype"]["artifact_id"] not in text
    assert not set(members) & {c["section_id"] for c in payload["rag"]["chunks"]}


def test_download_rechecks_original_member_even_after_it_is_no_longer_selected(library):
    ctx = library
    members = declare_original_members(ctx)
    edit_document(ctx, "# 收费项\n\n新的人工维护文档。")
    fixed = snapshot(ctx)
    actor = reader(ctx)
    payload, _raw, _name, job = export(ctx, fixed, actor=actor)
    assert len(payload["prototypes"]) == 1
    assert "新的人工维护文档" in payload["documents"][0]["markdown"]
    with ctx["service"].db() as s:
        current_ids = set(
            s.scalars(select(Selection.entity_revision_id).where(Selection.analysis_set_id == ctx["aid"]))
        )
        assert set(members) - current_ids
        assert set(members) <= set(s.get(Job, job["id"]).input_json["_export_revision_ids"])
        original = s.get(Artifact, s.get(Revision, members[0]).artifact_id)
        original.metadata_json = {**original.metadata_json, "requires_trace_read": True}
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.status == 403


def test_frozen_export_keeps_old_prototype_chapters_and_citations(library):
    ctx = library
    fixed = snapshot(ctx)
    before, *_ = export(ctx, fixed)
    p2 = test_materials.stage(
        ctx["service"],
        ctx["owner"],
        ctx["manager"],
        ctx["pid"],
        ctx["aid"],
        "prototype",
        '<title>全新原型</title><button id="next">新的控件</button>',
        package_id=ctx["prototype"]["package_id"],
    )
    d2 = test_materials.stage(
        ctx["service"],
        ctx["owner"],
        ctx["manager"],
        ctx["pid"],
        ctx["aid"],
        "documents",
        "# 全新文档\n\n后续版本中的全新规则。",
        package_id=ctx["document"]["package_id"],
    )
    test_materials.activate(ctx["service"], ctx["owner"], ctx["manager"], ctx["pid"], ctx["aid"], [p2, d2])
    with ctx["service"].db() as s:
        # Current relationship state is independently mutable; the release's
        # frozen payload must remain authoritative for exported validity.
        s.get(Relation, before["bindings"][0]["id"]).excluded = True
    frozen, *_ = export(ctx, fixed)
    assert frozen == before
    assert "后续版本中的全新规则" not in json.dumps(frozen, ensure_ascii=False)


def test_download_rechecks_runtime_input_artifact_and_output_digest(library):
    ctx = library
    fixed = snapshot(ctx)
    actor = reader(ctx)
    _payload, _raw, _name, job = export(ctx, fixed, actor=actor)
    with ctx["service"].db() as s:
        runtime = s.get(Artifact, ctx["prototype"]["artifact_id"])
        original = s.get(Artifact, runtime.metadata_json["prototype_input_artifact_ids"][0])
        original_id = original.id
        original.metadata_json = {**original.metadata_json, "requires_source_read": True}
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.status == 403
    # The content-addressed published object must also remain byte-exact.
    with ctx["service"].db() as s:
        s.get(Artifact, original_id).metadata_json = {}
        key = s.get(Job, job["id"]).result_json["object_key"]
    (ctx["service"].data_dir / "objects" / key).write_bytes(b"CORRUPTED_EXPORT")
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.code == "VKS_INTEGRITY_ERROR"


def test_hidden_model_input_provenance_cannot_leak_derived_binding(library):
    ctx = library
    fixed = snapshot(ctx)
    actor = reader(ctx)
    with ctx["service"].db() as s:
        # Tighten the full model-input provenance independently of the selected
        # citation. The same rule must protect output wording and its title.
        relation = next(
            row
            for row in s.get(Release, fixed["id"]).manifest_json["relations"]
            if row["relation_type"] == "has_business_point"
        )
        stored = s.get(Relation, relation["id"])
        runtime = s.get(Artifact, ctx["prototype"]["artifact_id"])
        raw = b"A separate source input used to derive the model wording."
        key = ctx["service"]._put_object(raw)
        hidden = Artifact(
            id=uid(),
            project_id=ctx["pid"],
            snapshot_id=runtime.snapshot_id,
            relative_path="extra-model-input.txt",
            sha256=key,
            size_bytes=len(raw),
            kind="source",
            media_type="text/plain",
            sensitivity="internal",
            object_key=key,
            metadata_json={},
        )
        s.add(hidden)
        s.flush()
        stored.metadata_json = {"generation_kind": "model", "model_provenance_artifact_ids": [hidden.id]}
        payload, deps, _ = projection(
            ctx["service"],
            s,
            actor,
            ctx["pid"],
            fixed["id"],
            {"allowed_source": False, "allowed_confidential": False, "allowed_trace": False},
        )
    assert payload["prototypes"] and payload["documents"]
    assert payload["bindings"] == []
    assert "必须保留的人工说明" not in json.dumps(payload, ensure_ascii=False)
    assert relation["id"] not in deps["_export_relation_ids"]


def test_hidden_evidence_omits_its_binding_and_preserves_other_readable_content(library):
    ctx = library
    with ctx["service"].db() as s:
        raw = b"Independent source evidence."
        key = ctx["service"]._put_object(raw)
        runtime = s.get(Artifact, ctx["prototype"]["artifact_id"])
        hidden = Artifact(
            id=uid(),
            project_id=ctx["pid"],
            snapshot_id=runtime.snapshot_id,
            relative_path="independent-source-evidence.txt",
            sha256=key,
            size_bytes=len(raw),
            kind="source",
            media_type="text/plain",
            sensitivity="internal",
            object_key=key,
            metadata_json={},
        )
        s.add(hidden)
        evidence = Evidence(
            id=uid(),
            project_id=ctx["pid"],
            analysis_set_id=ctx["aid"],
            artifact_id=hidden.id,
            stable_key="separate-source-evidence",
            evidence_kind="declared",
            content_sha256=key,
            locator={"kind": "file_lines", "startLine": 1, "endLine": 1},
            excerpt=raw.decode(),
            claim="Declared evidence",
        )
        s.add(evidence)
        edge = s.scalar(
            select(Relation).where(
                Relation.analysis_set_id == ctx["aid"], Relation.relation_type == "has_business_point"
            )
        )
        edge.evidence_ids = [evidence.id]
    payload, *_ = export(ctx, snapshot(ctx), actor=reader(ctx))
    assert payload["bindings"] == []
    assert payload["prototypes"] and payload["documents"] and payload["rag"]["coverage"]["complete"]
    assert "必须保留的人工说明" not in json.dumps(payload, ensure_ascii=False)


def test_runtime_scope_changed_during_embedding_cannot_publish_a_stale_export(library, monkeypatch):
    ctx = library
    fixed = snapshot(ctx)
    actor = reader(ctx)
    original = ai_gateway.request_model

    def tighten_runtime_scope(config, body):
        result = original(config, body)
        with ctx["service"].db() as s:
            runtime = s.get(Artifact, ctx["prototype"]["artifact_id"])
            uploaded = s.get(Artifact, runtime.metadata_json["prototype_input_artifact_ids"][0])
            uploaded.metadata_json = {**uploaded.metadata_json, "requires_source_read": True}
        return result

    monkeypatch.setattr(ai_gateway, "request_model", tighten_runtime_scope)
    job = ctx["service"].export(
        actor, ctx["pid"], {"release_id": fixed["id"], "format": "readonly_html", "include_source": False}, uid()
    )
    ctx["service"].process_job(job["id"])
    result = ctx["service"].get_job(actor, job["id"])
    assert result["state"] == "failed" and result["error"]["code"] == "VKS_VERSION_CONFLICT"
    with ctx["service"].db() as s:
        assert "object_key" not in s.get(Job, job["id"]).result_json
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.status == 404


def image_package(color):
    image = BytesIO()
    Image.new("RGB", (3, 2), color).save(image, format="PNG")
    archive = BytesIO()
    with ZipFile(archive, "w") as z:
        z.writestr("manual/guide.md", "# 图片手册\n\n![本地流程图](../assets/flow.png)\n\n[其他章节](../other.md)\n")
        z.writestr("other.md", "# 其他章节\n\n可在平台文档间跳转。")
        z.writestr("assets/flow.png", image.getvalue())
        z.writestr("assets/untrusted.svg", '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')
        z.writestr("assets/source.js", "SOURCE_ATTACHMENT_MUST_NOT_EXECUTE_OR_ENTER_READER")
    return archive.getvalue(), image.getvalue()


def install_image_package(ctx, color, package_id=None):
    raw, image = image_package(color)
    staged = test_materials.stage(
        ctx["service"],
        ctx["owner"],
        ctx["manager"],
        ctx["pid"],
        ctx["aid"],
        "documents",
        raw,
        package_id=package_id,
        name="图片资料包",
        filename="manual.zip",
    )
    test_materials.activate(ctx["service"], ctx["owner"], ctx["manager"], ctx["pid"], ctx["aid"], [staged])
    return staged, image


def test_embedded_images_follow_fixed_package_version_when_markdown_revision_is_reused(library):
    ctx = library
    first, old_image = install_image_package(ctx, "red")
    old_snapshot = snapshot(ctx)
    old_payload, *_ = export(ctx, old_snapshot)
    second, new_image = install_image_package(ctx, "blue", first["package_id"])
    with ctx["service"].db() as s:
        assert (
            s.get(Revision, first["revision_id"]).metadata_json[ROOT]["members"]
            == s.get(Revision, second["revision_id"]).metadata_json[ROOT]["members"]
        )
    new_payload, *_ = export(ctx, snapshot(ctx))
    frozen, *_ = export(ctx, old_snapshot)
    assert frozen == old_payload
    old_doc = next(doc for doc in old_payload["documents"] if doc["title"] == "图片手册")
    new_doc = next(doc for doc in new_payload["documents"] if doc["title"] == "图片手册")
    assert old_doc["id"] == new_doc["id"] and old_doc["path"] == "manual/guide.md"
    assert base64.b64decode(old_doc["images"]["../assets/flow.png"].split(",", 1)[1]) == old_image
    assert base64.b64decode(new_doc["images"]["../assets/flow.png"].split(",", 1)[1]) == new_image
    assert old_image != new_image
    assert "SOURCE_ATTACHMENT_MUST_NOT_EXECUTE_OR_ENTER_READER" not in json.dumps(new_payload)
    assert "onload=" not in json.dumps(new_payload)


def test_image_permissions_filter_bytes_and_recheck_download_dependencies(library):
    ctx = library
    package, image = install_image_package(ctx, "green")
    fixed = snapshot(ctx)
    actor = reader(ctx)
    visible, _raw, _name, job = export(ctx, fixed, actor=actor)
    with ctx["service"].db() as s:
        assets = s.get(Revision, package["revision_id"]).metadata_json[ROOT]["attachment_artifact_ids"]
        attachment = next(
            s.get(Artifact, aid) for aid in assets if s.get(Artifact, aid).relative_path.endswith("flow.png")
        )
        assert attachment.id in s.get(Job, job["id"]).input_json["_export_artifact_ids"]
        attachment.metadata_json = {**attachment.metadata_json, "requires_trace_read": True}
    assert any(doc["images"] for doc in visible["documents"])
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.status == 403
    hidden, *_ = export(ctx, fixed, actor=actor)
    doc = next(doc for doc in hidden["documents"] if doc["title"] == "图片手册")
    assert doc["images"] == {} and "本地流程图" in doc["markdown"]
    assert base64.b64encode(image).decode() not in json.dumps(hidden)
    assert hidden["rag"]["coverage"]["complete"]


@pytest.mark.parametrize("legacy", [False, True])
def test_extracted_markdown_and_vectors_inherit_original_upload_restrictions(library, legacy):
    ctx = library
    if legacy:
        with ctx["service"].db() as s:
            package = s.get(Revision, ctx["document"]["revision_id"])
            for rid in package.metadata_json[ROOT]["members"]:
                art = s.get(Artifact, s.get(Revision, rid).artifact_id)
                art.metadata_json = {
                    key: value for key, value in art.metadata_json.items() if key != "prototype_input_artifact_ids"
                }
    fixed = snapshot(ctx)
    actor = reader(ctx)
    original, _raw, _name, job = export(ctx, fixed, actor=actor)
    assert original["documents"] and original["rag"]["chunks"]
    with ctx["service"].db() as s:
        package = s.get(Revision, ctx["document"]["revision_id"])
        runtime = s.get(Artifact, package.artifact_id)
        upload = s.get(Artifact, runtime.metadata_json["prototype_input_artifact_ids"][0])
        upload.metadata_json = {**upload.metadata_json, "requires_source_read": True}
    with pytest.raises(DomainError) as exc:
        ctx["service"].download_export(actor, ctx["pid"], job["id"])
    assert exc.value.status == 403
    assert ctx["service"].workspace(actor, ctx["pid"], release_id=fixed["id"])["documents"] == []
    assert (
        ctx["service"].search(actor, ctx["pid"], {"release_id": fixed["id"], "query": "新增收费项需要填写名称"}) == []
    )
    before_calls = len(ctx["calls"])
    hidden, *_ = export(ctx, fixed, actor=actor)
    assert hidden["documents"] == [] and hidden["rag"]["chunks"] == []
    assert hidden["bindings"] == [] and hidden["prototypes"]
    assert "新增收费项需要填写名称" not in json.dumps(hidden, ensure_ascii=False)
    assert len(ctx["calls"]) == before_calls


@pytest.mark.parametrize("corruption", ["url", "png"])
def test_malformed_image_cannot_prevent_exporting_readable_text(library, corruption):
    ctx = library
    markdown = "# 异常图片引用\n\n![异常图片](https://[)\n\n其余正文应当正常阅读。"
    content, filename = markdown, "bad-image.md"
    if corruption == "png":
        image = BytesIO()
        Image.new("RGB", (3, 2), "red").save(image, format="PNG")
        damaged = bytearray(image.getvalue())
        damaged[damaged.index(b"IDAT") + 4] ^= 1
        archive = BytesIO()
        with ZipFile(archive, "w") as z:
            z.writestr("bad-image.md", markdown.replace("https://[", "broken.png"))
            z.writestr("broken.png", damaged)
        content, filename = archive.getvalue(), "bad-image.zip"
    staged = test_materials.stage(
        ctx["service"],
        ctx["owner"],
        ctx["manager"],
        ctx["pid"],
        ctx["aid"],
        "documents",
        content,
        filename=filename,
    )
    test_materials.activate(ctx["service"], ctx["owner"], ctx["manager"], ctx["pid"], ctx["aid"], [staged])
    payload, *_ = export(ctx, snapshot(ctx))
    document = next(doc for doc in payload["documents"] if doc["title"] == "异常图片引用")
    assert document["images"] == {} and "其余正文应当正常阅读" in document["markdown"]
    assert payload["warnings"] and payload["rag"]["coverage"]["complete"]
