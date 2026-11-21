"""Validate references in a portable Studio composition without running its HTML."""

from .vks_ingest import _fail, canonical_json, safe_path


def validate_studio(studio, artifacts, entities):
    by_key = {row["stable_key"]: row for row in entities}
    package_keys, prototype_keys, document_keys = set(), set(), set()

    def check_file(ref, key):
        artifact = artifacts.get(safe_path(ref["path"]))
        if (
            not artifact
            or artifact["sha256"] != ref["sha256"]
            or artifact["source_id"] != key
            or artifact["kind"] != "fixture"
        ):
            _fail("VKS_REFERENCE_INVALID", "Studio 资料文件或摘要不匹配")

    for package in studio["packages"]:
        key = package["key"]
        if not key.startswith("material.") or key in package_keys or key in by_key:
            _fail("VKS_DUPLICATE_LOGICAL_KEY", "Studio 资料包逻辑身份重复或不合法")
        package_keys.add(key)
        for ref in [package["runtime"], *package["inputs"], *package["attachments"]]:
            check_file(ref, key)
        for ref in package["inputs"]:
            name = safe_path(ref["name"])
            if "/" in name or (ref["encoding"] == "base64") != name.lower().endswith(".zip"):
                _fail("VKS_REFERENCE_INVALID", "Studio 原始输入文件名或编码无效")
        if package["kind"] == "prototype":
            prototype_keys.add(key)
            if (
                package["documents"]
                or package["attachments"]
                or package["format"] not in {"html", "static-site", "charge-prototype"}
            ):
                _fail("VKS_REFERENCE_INVALID", "Studio 原型包的文件角色不匹配")
        elif package["format"] not in {
            "markdown",
            "html-document",
            "charge-handbook",
            "static-site",
            "handbook-authoring",
            "standalone-handbook",
        }:
            _fail("VKS_REFERENCE_INVALID", "Studio 文档包的文件角色不匹配")
        paths = set()
        for document in package["documents"]:
            doc_key = document["key"]
            path = safe_path(document["path"])
            doc = by_key.get(doc_key)
            if not doc or doc["type"] != "document" or doc_key in document_keys or path in paths:
                _fail("VKS_REFERENCE_INVALID", "Studio 文档逻辑身份或资料路径不匹配")
            document_keys.add(doc_key)
            paths.add(path)
    if studio["primaryPackage"] is not None and studio["primaryPackage"] not in prototype_keys:
        _fail("VKS_REFERENCE_INVALID", "Studio 主原型必须指向声明的原型包")
    used_keys = set(by_key) | package_keys
    used_targets = set()
    for row in studio["bindings"]:
        signature = {key: value for key, value in row["target"].items() if key != "open_steps"}
        target = canonical_json([row["package"], signature])
        if (
            row["package"] not in prototype_keys
            or target in used_targets
            or row["action"] == row["point"]
            or row["action"] in used_keys
            or row["point"] in used_keys
            or any(by_key.get(key, {}).get("type") != "section" for key in [*row["sections"], *row["inputs"]])
        ):
            _fail("VKS_REFERENCE_INVALID", "Studio 关联的原型、控件或章节引用不匹配")
        used_keys.update((row["action"], row["point"]))
        used_targets.add(target)
    return studio
