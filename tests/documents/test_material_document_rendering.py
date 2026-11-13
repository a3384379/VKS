"""Document formatting and images survive imports and exact package selection."""

import base64
import json
import posixpath
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image
from vks_api.attachments import resolve
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.material_parser import html_markdown, parse
from vks_api.materials import ROOT
from vks_api.models import Artifact, Revision, uid
from vks_api.readonly_export import document_images
from vks_api.service import DomainError

from tests.interactive import test_materials

materials = test_materials.materials


def raster(color):
    output = BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format="PNG")
    return output.getvalue()


def package(image=None, *, markdown="# 完整文档\n\n**强调内容**\n\n![流程图](../assets/flow.png)\n"):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("manual/guide.md", markdown)
        if image is not None:
            archive.writestr("assets/flow.png", image)
    return output.getvalue()


def install(service, owner, manager, pid, aid, image=None, package_id=None, **kw):
    staged = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        package(image, **kw),
        package_id=package_id,
        filename="manual.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    return staged, catalog["documents"][0]


def test_studio_images_use_the_selected_package_even_when_markdown_revision_reused(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    red, blue = raster("red"), raster("blue")
    first, old_doc = install(service, owner, manager, pid, aid, red)
    frozen = manager.checkpoint(owner, pid, aid, "Red image", uid())
    second, current_doc = install(service, owner, manager, pid, aid, blue, first["package_id"])
    assert old_doc["revision_id"] == current_doc["revision_id"]
    assert first["revision_id"] != second["revision_id"]
    args = (service, owner, current_doc["id"], current_doc["revision_id"], "../assets/flow.png")
    assert resolve(*args, analysis_set_id=aid)[0] == blue
    version = InteractiveKnowledgeService(service).catalog(owner, pid, aid)["selection_version"]
    assert resolve(*args, analysis_set_id=aid, selection_version=version)[0] == blue
    with pytest.raises(DomainError) as exc:
        resolve(*args, analysis_set_id=aid, selection_version=version - 1)
    assert exc.value.status == 412
    assert resolve(*args, release_id=frozen["id"])[0] == red
    assert resolve(*args)[0] == red  # Existing unscoped links stay source-pinned.
    _third, still_same_doc = install(service, owner, manager, pid, aid, None, first["package_id"])
    assert still_same_doc["revision_id"] == current_doc["revision_id"]
    with pytest.raises(DomainError) as exc:
        resolve(*args, analysis_set_id=aid)
    assert exc.value.status == 404  # Explicit empty asset list cannot revive an old image.


def test_scoped_image_endpoint_requires_exact_document_and_dynamic_input_permissions(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    staged, doc = install(service, owner, manager, pid, aid, raster("green"))
    token = service.create_token(owner, pid, {"name": "image-reader", "scopes": ["knowledge:read", "knowledge:edit"]})
    reader = service.authenticate(token["token"], "token")
    endpoint = f"/api/v1/documents/{doc['id']}/revisions/{doc['revision_id']}/attachments"
    headers = {"Authorization": "Bearer " + token["token"]}
    params = {"path": "../assets/flow.png", "analysis_set_id": aid}
    response = web.get(endpoint, params=params, headers=headers)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    with pytest.raises(DomainError):
        resolve(service, reader, doc["id"], doc["revision_id"], "../assets/flow.png", analysis_set_id=projects[1][1])
    with pytest.raises(DomainError) as exc:
        resolve(
            service, owner, doc["id"], doc["revision_id"], "../assets/flow.png", analysis_set_id=aid, release_id=uid()
        )
    assert exc.value.status == 422
    with service.db() as db:
        root = db.get(Revision, staged["revision_id"])
        image = db.get(Artifact, root.metadata_json[ROOT]["attachment_artifact_ids"][0])
        raw_input = db.get(Artifact, image.metadata_json["prototype_input_artifact_ids"][0])
        raw_input.metadata_json = {**raw_input.metadata_json, "requires_source_read": True}
    response = web.get(endpoint, params=params, headers=headers)
    assert response.status_code in (403, 404)
    assert raster("green") not in response.content


def test_scoped_image_endpoint_checks_content_digest_and_rejects_bad_paths(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    staged, doc = install(service, owner, manager, pid, aid, raster("green"))
    args = (service, owner, doc["id"], doc["revision_id"])
    for path in ("https://[", "../assets/flow.png?remote=1", "//host/x", "../assets/%00.png", "../../../../secret"):
        with pytest.raises(DomainError):
            resolve(*args, path, analysis_set_id=aid)
    with service.db() as db:
        root = db.get(Revision, staged["revision_id"])
        image = db.get(Artifact, root.metadata_json[ROOT]["attachment_artifact_ids"][0])
        object_key = image.object_key
    (service.data_dir / "objects" / object_key).write_bytes(b"corrupted image")
    with pytest.raises(DomainError) as exc:
        resolve(*args, "../assets/flow.png", analysis_set_id=aid)
    assert exc.value.code == "VKS_INTEGRITY_ERROR"


def test_published_read_only_images_and_full_markdown_reference_context(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    markdown = "# Image manual\n\n![Reference image][flow]\n\n## Definitions\n\n[flow]: ../assets/flow.png\n\n<img src='../assets/flow.png' alt='HTML image' onerror='DO_NOT_EXECUTE()'>\n"
    image = raster("yellow")
    staged, doc = install(service, owner, manager, pid, aid, image, markdown=markdown)
    frozen = manager.checkpoint(owner, pid, aid, "Public image reader", uid())
    token = service.create_token(owner, pid, {"name": "published-image-reader", "scopes": ["knowledge:read"]})
    endpoint = f"/api/v1/documents/{doc['id']}/revisions/{doc['revision_id']}/attachments"
    headers = {"Authorization": "Bearer " + token["token"]}
    response = web.get(endpoint, params={"path": "../assets/flow.png", "release_id": frozen["id"]}, headers=headers)
    assert response.status_code == 200 and response.content == image
    denied = web.get(endpoint, params={"path": "../assets/flow.png", "analysis_set_id": aid}, headers=headers)
    assert denied.status_code == 403
    with service.db() as db:
        revision = db.get(Revision, doc["revision_id"])
        root = db.get(Revision, staged["revision_id"])
        images, dependencies, missing = document_images(
            service,
            db,
            owner,
            pid,
            revision,
            [{"content": "# Image manual\n\n![Reference image][flow]"}],
            {"allowed_source": True, "allowed_confidential": True, "allowed_trace": True},
            root.metadata_json[ROOT]["attachment_artifact_ids"],
            markdown=markdown,
        )
    assert base64.b64decode(images["../assets/flow.png"].split(",", 1)[1]) == image
    assert missing == 0 and dependencies


def test_html_import_retains_markdown_reading_structure_without_executable_source():
    raw = b"""<!doctype html><html><head><style>.x{color:red}</style></head><body>
      <h1>Title</h1><p>Use <strong>required</strong> and <em>carefully</em> with <code>a_b</code>.</p>
      <ul><li>Outer<ul><li>Inner</li></ul></li></ul>
      <pre><code class="language-python">if x &lt; 2:\n    print("ok")</code></pre>
      <table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td><b>yes</b></td></tr></table>
      <p><img src="assets/flow.png" alt="Workflow" onerror="steal()"></p>
      <p><a href="other.md#part">Read more</a></p><blockquote>Important note</blockquote>
      <script>window.SOURCE_MUST_NOT_EXECUTE=true</script><iframe src="https://remote.invalid"></iframe>
      </body></html>"""
    result = html_markdown(raw, "guide.html")
    assert "# Title" in result and "**required**" in result and "*carefully*" in result
    assert "`a_b`" in result and "- Outer\n  - Inner" in result
    assert '```python\nif x < 2:\n    print("ok")\n```' in result
    assert "| Name | Value |" in result and "**yes**" in result
    assert "![Workflow](assets/flow.png)" in result and "[Read more](other.md#part)" in result
    assert "> Important note" in result
    assert all(
        value not in result
        for value in ("SOURCE_MUST_NOT_EXECUTE", "onerror", "steal()", "remote.invalid", "color:red")
    )


def test_ordinary_markdown_import_keeps_source_whitespace_and_markup():
    markdown = "介绍段落，原文不重排。\r\n\r\n# **标题**\r\n\r\n**加粗**，_斜体_，~~删除~~。\r\n\r\n![图](assets/pic.png)\r\n\r\n```mermaid\r\nflowchart LR\r\n    A --> B\r\n```\r\n\r\n[ref]: other.md\r\n"
    result = parse("documents", [("guide.md", markdown.encode())])
    assert result["documents"] == [("guide.md", markdown)]


def test_html_document_with_assets_preserves_original_view_and_separate_markdown(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    html = b'<!doctype html><html><head><title>Original reader</title><link rel="stylesheet" href="assets/style.css"></head><body><h1>Original layout</h1><p><b>Required field</b></p><img alt="Original diagram" src="assets/flow.png"></body></html>'
    css = b"body{background:#f0f8ff;font-family:serif}strong,b{color:#123456}"
    image = raster("purple")
    bundle = BytesIO()
    with ZipFile(bundle, "w") as archive:
        archive.writestr("guide.html", html)
        archive.writestr("assets/style.css", css)
        archive.writestr("assets/flow.png", image)
    raw = bundle.getvalue()
    parsed = parse("documents", [("reader.zip", raw)])
    assert parsed["original"] == html and parsed["entry"] == "guide.html" and parsed["format"] == "html-document"
    assert "**Required field**" in parsed["documents"][0][1]
    staged = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="reader.zip")
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    with service.db() as db:
        root = db.get(Revision, staged["revision_id"])
        runtime = db.get(Artifact, root.artifact_id)
        source = db.get(Artifact, runtime.metadata_json["prototype_input_artifact_ids"][0])
        runtime_bytes = (service.data_dir / "objects" / runtime.object_key).read_bytes()
        source_bytes = (service.data_dir / "objects" / source.object_key).read_bytes()
    from bs4 import BeautifulSoup

    payload = json.loads(BeautifulSoup(runtime_bytes, "html.parser").find(id="vks-site").string)
    assert base64.b64decode(payload["guide.html"]["data"]) == html
    assert base64.b64decode(payload["assets/style.css"]["data"]) == css
    assert base64.b64decode(payload["assets/flow.png"]["data"]) == image
    assert source_bytes == raw and runtime_bytes != html
    doc = InteractiveKnowledgeService(service).catalog(owner, pid, aid)["documents"][0]
    assert resolve(service, owner, doc["id"], doc["revision_id"], "assets/flow.png", analysis_set_id=aid)[0] == image


@pytest.mark.parametrize("legacy_registry", [False, True])
def test_unscoped_material_attachment_cannot_download_raw_package_with_hidden_member(materials, legacy_registry):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    image = raster("cyan")
    archive = BytesIO()
    with ZipFile(archive, "w") as package:
        package.writestr("manual/public.md", "# Public guide\n\n![Public image](../assets/public.png)\n")
        package.writestr("manual/private.md", "# Private rules\n\nRESTRICTED_PACKAGE_MEMBER_MUST_NOT_LEAK\n")
        package.writestr("assets/public.png", image)
    staged = test_materials.stage(
        service, owner, manager, pid, aid, "documents", archive.getvalue(), filename="mixed-docs.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [staged])
    catalog = InteractiveKnowledgeService(service).catalog(owner, pid, aid)
    public = next(doc for doc in catalog["documents"] if doc["name"] == "Public guide")
    private = next(doc for doc in catalog["documents"] if doc["name"] == "Private rules")
    manager.checkpoint(owner, pid, aid, "Fixed package", uid())
    with service.db() as db:
        hidden = db.get(Artifact, db.get(Revision, private["revision_id"]).artifact_id)
        hidden.metadata_json = {**hidden.metadata_json, "requires_source_read": True}
        root = db.get(Revision, staged["revision_id"])
        if legacy_registry:
            metadata = dict(root.metadata_json[ROOT])
            metadata.pop("attachment_artifact_ids")
            root.metadata_json = {**root.metadata_json, ROOT: metadata}
        runtime = db.get(Artifact, root.artifact_id)
        raw_input = next(
            db.get(Artifact, x)
            for x in runtime.metadata_json["prototype_input_artifact_ids"]
            if db.get(Artifact, x).relative_path.startswith("inputs/")
        )
        public_source = db.get(Artifact, db.get(Revision, public["revision_id"]).artifact_id)
        paths = [
            posixpath.relpath(asset.relative_path, posixpath.dirname(public_source.relative_path))
            for asset in (raw_input, runtime, hidden)
        ]
    token = service.create_token(owner, pid, {"name": "public-package-reader", "scopes": ["knowledge:read"]})
    headers = {"Authorization": "Bearer " + token["token"]}
    endpoint = f"/api/v1/documents/{public['id']}/revisions/{public['revision_id']}/attachments"
    genuine = web.get(endpoint, params={"path": "../assets/public.png"}, headers=headers)
    assert genuine.status_code == 200 and genuine.content == image
    for path in paths:
        denied = web.get(endpoint, params={"path": path}, headers=headers)
        assert denied.status_code == 404
        assert b"RESTRICTED_PACKAGE_MEMBER_MUST_NOT_LEAK" not in denied.content
