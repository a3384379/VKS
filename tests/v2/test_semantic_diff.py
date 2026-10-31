from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json, zipfile
from test_journeys import store as store  # pytest fixture registration
from v2_support import published
from packages.analyzers.java_spring import java_lexical_sha
from packages.analyzers.vks_ingest import source_digest
from vks_api.knowledge import KnowledgeService


def test_java_comments_and_spacing_do_not_hide_changes_inside_string_literals():
    raw = b'class A { String a() { return "// literal"; } }'
    assert java_lexical_sha(raw) == java_lexical_sha(b"/* new comment */\n" + raw + b"// tail\n")
    assert java_lexical_sha(raw) != java_lexical_sha(raw.replace(b"// literal", b"// changed"))
    assert java_lexical_sha(b"class A {") is None


def test_actual_fixed_source_comment_reimport_is_non_semantic_and_old_release_stays_exact(store):
    service, actor, pid, w, _, _ = store
    release, w = published(service, actor, {"id": pid})
    original = service.workspace(actor, pid, release_id=release["id"])
    clone = KnowledgeService(service).clone(actor, pid, w["selected_analysis_set"]["id"], "Comment update", "clone")
    with zipfile.ZipFile(Path("fixtures/charge-demo.vksbundle.zip")) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    manifest = json.loads(files["manifest.json"])
    path = next(row["path"] for row in manifest["artifacts"] if row["path"].endswith(".java"))
    files[path] = b"// Synthetic comment-only source update\n" + files[path]
    changed_sha = sha256(files[path]).hexdigest()

    def relocate(value):
        if isinstance(value, dict):
            if value.get("artifactPath") == path:
                if "sha256" in value:
                    value["sha256"] = changed_sha
                for key in ("startLine", "endLine"):
                    if key in value:
                        value[key] += 1
                if isinstance(value.get("locator"), dict):
                    for key in ("startLine", "endLine"):
                        if key in value["locator"]:
                            value["locator"][key] += 1
            for child in value.values():
                relocate(child)
        elif isinstance(value, list):
            for child in value:
                relocate(child)

    for reference in ("uiManifest", "bindings"):
        target = manifest[reference]["path"]
        document = json.loads(files[target])
        relocate(document)
        files[target] = json.dumps(document, ensure_ascii=False).encode()
        manifest[reference]["sha256"] = sha256(files[target]).hexdigest()
    for row in manifest["artifacts"]:
        row.update(sha256=sha256(files[row["path"]]).hexdigest(), size_bytes=len(files[row["path"]]))
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [row for row in manifest["artifacts"] if row["source_id"] == source["source_id"]]
        )
    files["manifest.json"] = json.dumps(manifest).encode()
    raw = BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    parsed = service._parse(raw.getvalue(), "charge-demo")
    with service.db() as s:
        service._commit_bundle(s, pid, actor.id, parsed, clone["id"])
    diff = service.diff(actor, pid, release["id"], clone["id"])
    assert any(row["change_kind"] == "non_semantic" and row["entity_type"] == "symbol" for row in diff["changes"])
    assert service.workspace(actor, pid, release_id=release["id"])["selected_release"] == original["selected_release"]
    assert service.workspace(actor, pid, release_id=release["id"])["entities"] == original["entities"]
