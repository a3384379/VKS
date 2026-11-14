"""The downloadable local editor returns exact ordinary MD/images to VKS."""

import base64
import copy
import json
from io import BytesIO

import pytest
from PIL import Image
from vks_api.attachments import resolve
from vks_api.document_presentation import PresentationInput, PresentationService
from vks_api.exports import read_original
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.material_parser import parse
from vks_api.materials import ROOT, RestoreMaterialVersion
from vks_api.models import Artifact, Revision, uid
from vks_api.service import DomainError
from vks_api.template_ingest import SCHEMA, read_authoring

from tests.interactive import test_materials

materials = test_materials.materials


def picture():
    output = BytesIO()
    Image.new("RGB", (3, 4), "#14564b").save(output, "PNG")
    return output.getvalue()


def content():
    return {
        "schema": SCHEMA,
        "template_id": "handbook",
        "template_version": "1.0.0",
        "documents": [
            {
                "path": "01业务/收费.md",
                "name": "收费业务",
                "markdown": "# 收费说明\r\n\r\n原始 **加粗** 保留。\r\n\r\n![流程](../assets/flow.png)\r\n",
            },
            {"path": "02技术/接口.md", "markdown": "# 接口说明\n\n精确正文。\n"},
        ],
        "images": [{"path": "assets/flow.png", "mime": "image/png", "base64": base64.b64encode(picture()).decode()}],
    }


def html(envelope):
    body = json.dumps(envelope, ensure_ascii=False).replace("<", "\\u003c")
    return (
        '<!doctype html><meta charset="utf-8"><script>throw new Error("MUST_NOT_EXECUTE")</script>'
        '<script id="vks-template-data" type="application/json">' + body + "</script>"
    ).encode()


def test_authoring_import_preserves_bytes_paths_images_and_default_versioned_template(materials):
    _, service, actor, manager, projects = materials
    pid, aid = projects[0]
    envelope, source = content(), html(content())
    package = test_materials.stage(service, actor, manager, pid, aid, "documents", source, filename="handbook.html")
    test_materials.activate(service, actor, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(actor, pid, aid)
    assert catalog["presentation"]["mode"] == "handbook"
    assert {d["path"] for d in catalog["documents"]} == {d["path"] for d in envelope["documents"]}
    selected = {d["path"]: d for d in catalog["documents"]}
    assert selected["01业务/收费.md"]["name"] == "收费业务"
    with service.db() as s:
        root = s.get(Revision, package["revision_id"])
        assert root.metadata_json[ROOT]["format"] == "handbook-authoring"
        assert root.metadata_json[ROOT]["document_template"]["template_id"] == "handbook"
        assert read_original(service, s.get(Artifact, root.artifact_id)) == source
        for document in envelope["documents"]:
            assert s.get(Revision, selected[document["path"]]["revision_id"]).content == document["markdown"]
    doc = selected["01业务/收费.md"]
    raw, media, disposition, _ = resolve(service, actor, doc["id"], doc["revision_id"], "../assets/flow.png", aid)
    assert raw == picture() and media == "image/png" and disposition == "inline"
    frozen = manager.checkpoint(actor, pid, aid, "Local author import", uid())
    settings = PresentationService(service)
    settings.update(
        actor,
        pid,
        PresentationInput(analysis_set_id=aid, mode="standard"),
        str(settings.get(actor, pid, aid)["selection_version"]),
        uid(),
    )
    restored = manager.restore(
        actor,
        pid,
        RestoreMaterialVersion(analysis_set_id=aid, release_id=frozen["id"]),
        str(settings.get(actor, pid, aid)["selection_version"]),
        uid(),
    )
    assert settings.get(actor, pid, restored["analysis_set_id"])["settings"]["mode"] == "handbook"


@pytest.mark.parametrize("mode", ["auto", "standard", "original"])
def test_import_does_not_override_explicit_reader_choice(materials, mode):
    _, service, actor, manager, projects = materials
    pid, aid = projects[0]
    preferences = PresentationService(service)
    initial = preferences.get(actor, pid, aid)
    preferences.update(
        actor, pid, PresentationInput(analysis_set_id=aid, mode=mode), str(initial["selection_version"]), uid()
    )
    package = test_materials.stage(
        service, actor, manager, pid, aid, "documents", html(content()), filename="local.html"
    )
    test_materials.activate(service, actor, manager, pid, aid, [package])
    assert preferences.get(actor, pid, aid)["settings"]["mode"] == mode


def test_updated_local_html_uses_existing_package_and_keeps_one_logical_document(materials):
    _, service, actor, manager, projects = materials
    pid, aid = projects[0]
    value = content()
    package = test_materials.stage(service, actor, manager, pid, aid, "documents", html(value), filename="local.html")
    test_materials.activate(service, actor, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    before = {d["path"]: d for d in links.catalog(actor, pid, aid)["documents"]}
    value["documents"][0]["markdown"] += "\n新增说明。\n"
    value["documents"][1]["name"] = "接口新名称"
    updated = test_materials.stage(
        service,
        actor,
        manager,
        pid,
        aid,
        "documents",
        html(value),
        package_id=package["package_id"],
        filename="local.html",
    )
    test_materials.activate(service, actor, manager, pid, aid, [updated])
    after = {d["path"]: d for d in links.catalog(actor, pid, aid)["documents"]}
    assert len(after) == 2 and all(after[path]["id"] == before[path]["id"] for path in before)
    assert after["02技术/接口.md"]["name"] == "接口新名称"
    assert len([p for p in links.catalog(actor, pid, aid)["packages"] if p["kind"] == "handbook"]) == 1


def test_plain_txt_and_nfd_markdown_image_references_roundtrip_without_rewriting_content(materials):
    _, service, actor, manager, projects = materials
    pid, aid = projects[0]
    source = content()
    text = "普通 TXT 内容 **保留原格式**。\r\n\r\n![图](../images/cafe\u0301.png)\r\n"
    source["documents"] = [{"path": "docs/notes.txt", "markdown": text}]
    source["images"][0]["path"] = "images/café.png"
    package = test_materials.stage(service, actor, manager, pid, aid, "documents", html(source), filename="notes.html")
    test_materials.activate(service, actor, manager, pid, aid, [package])
    document = InteractiveKnowledgeService(service).catalog(actor, pid, aid)["documents"][0]
    assert document["path"] == "docs/notes.txt"
    with service.db() as s:
        assert s.get(Revision, document["revision_id"]).content == text
    raw, media, _, _ = resolve(service, actor, document["id"], document["revision_id"], "../images/cafe\u0301.png", aid)
    assert raw == picture() and media == "image/png"
    for path in ("../../images/cafe\u0301.png", "https://example.invalid/cafe\u0301.png"):
        with pytest.raises(DomainError):
            resolve(service, actor, document["id"], document["revision_id"], path, aid)
    other_pid, other_aid = projects[1]
    other_source = copy.deepcopy(source)
    other_source["images"][0]["path"] = "images/secret.png"
    foreign = test_materials.stage(
        service, actor, manager, other_pid, other_aid, "documents", html(other_source), filename="other.html"
    )
    test_materials.activate(service, actor, manager, other_pid, other_aid, [foreign])
    with pytest.raises(DomainError):
        resolve(service, actor, document["id"], document["revision_id"], "../images/secret.png", aid)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema="other"),
        lambda p: p.update(api_key="NO_SECRETS"),
        lambda p: p["documents"][0].update(path="../outside.md"),
        lambda p: p["documents"][0].update(path="https://host/file.md"),
        lambda p: p["documents"][0].update(path="folder/%2e%2e/file.md"),
        lambda p: p["documents"].append(copy.deepcopy(p["documents"][0])),
        lambda p: p["images"][0].update(mime="image/svg+xml"),
        lambda p: p["images"][0].update(mime="image/jpeg", path="assets/flow.jpg"),
        lambda p: p["images"][0].update(base64="not base64"),
        lambda p: p["images"][0].update(path="C:/file.png"),
    ],
)
def test_authoring_parser_rejects_ambiguous_paths_undeclared_data_and_invalid_images(change):
    value = content()
    change(value)
    with pytest.raises(DomainError) as error:
        parse("documents", [("local.html", html(value))])
    assert error.value.code == "VKS_INVALID_ARCHIVE"


def test_empty_template_duplicate_data_and_size_limits_are_explicit():
    value = content()
    value["documents"] = []
    value["images"] = []
    assert read_authoring(html(value))["documents"] == []
    with pytest.raises(DomainError, match="目前为空"):
        parse("documents", [("local.html", html(value))])
    raw = html(content())
    with pytest.raises(DomainError):
        read_authoring(raw + raw)
    value = content()
    value["documents"][0]["markdown"] = "中" * (2 * 1024 * 1024 // 3 + 1)
    with pytest.raises(DomainError, match="2 MiB"):
        read_authoring(html(value))


@pytest.mark.parametrize("field", ["markdown", "path", "name"])
def test_invalid_unicode_is_rejected_without_a_server_error(field):
    value = content()
    value["documents"][0][field] = "\ud800" + (".md" if field == "path" else "")
    encoded = json.dumps(value, ensure_ascii=True)
    raw = ('<script id="vks-template-data" type="application/json">' + encoded + "</script>").encode()
    with pytest.raises(DomainError) as rejected:
        read_authoring(raw)
    assert rejected.value.code == "VKS_INVALID_ARCHIVE"


def test_template_download_is_authenticated_empty_and_independent_of_private_project(materials, monkeypatch):
    web, service, actor, manager, projects = materials
    path = "/api/v3/document-templates/handbook/download"
    assert web.get(path).status_code == 401
    monkeypatch.setenv("VKS_MODEL_API_KEY", "PRIVATE_KEY_MUST_NOT_BE_DOWNLOADED")
    pid, aid = projects[0]
    package = test_materials.stage(service, actor, manager, pid, aid, "documents", "# PRIVATE_PROJECT_CONTENT_SENTINEL")
    test_materials.activate(service, actor, manager, pid, aid, [package])
    assert web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).status_code == 200
    downloaded = web.get(path)
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.headers["content-type"].startswith("text/html")
    assert b"PRIVATE_PROJECT_CONTENT_SENTINEL" not in downloaded.content
    assert b"PRIVATE_KEY_MUST_NOT_BE_DOWNLOADED" not in downloaded.content
    parsed = read_authoring(downloaded.content)
    assert parsed["documents"] == [] and parsed["files"] == []
