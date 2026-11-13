"""Read supported offline HTML envelopes as data and emit a normal VKS bundle."""

import base64
import gzip
import re
import zlib
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZipFile

from .cancellation import checkpoint
from .vks_ingest import MAX_BUNDLE_BYTES, MAX_FILE_BYTES, _fail, _json, canonical_json, safe_path, source_digest

HANDBOOK = "fh-pack-data"
PROTOTYPE = "prototypePackPayload"
MAX_INFLATED = 80 * 1024 * 1024


class _ScriptData(HTMLParser):
    def __init__(self, wanted):
        super().__init__(convert_charrefs=False)
        self.wanted = wanted
        self.active = None
        self.records = {}

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        key = attrs_dict.get("id")
        if key not in self.wanted:
            return
        if key in self.records or len(attrs_dict) != len(attrs) or "src" in attrs_dict:
            _fail("VKS_INVALID_ARCHIVE", "离线包的数据节点有重复或外部引用")
        if attrs_dict.get("type") != self.wanted[key]:
            _fail("VKS_INVALID_ARCHIVE", "离线包的数据节点类型不匹配")
        self.active = key
        self.records[key] = []

    def handle_data(self, data):
        if self.active:
            self.records[self.active].append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = None


def _scripts(raw, wanted):
    parser = _ScriptData(wanted)
    try:
        parser.feed(raw.decode("utf-8-sig"))
        parser.close()
    except UnicodeError:
        _fail("VKS_ENCODING_INVALID", "离线包必须使用 UTF-8")
    if parser.active:
        _fail("VKS_INVALID_ARCHIVE", "离线包的数据节点未闭合")
    return {key: "".join(parts).strip() for key, parts in parser.records.items()}


def _base64(text, limit):
    if not isinstance(text, str) or len(text) > (limit + 2) // 3 * 4:
        _fail("VKS_ARTIFACT_TOO_LARGE", "离线包的数据项超过容量限制")
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        _fail("VKS_INVALID_ARCHIVE", "离线包含无效的 Base64 数据")


def _inflate(text):
    raw = _base64(text, MAX_FILE_BYTES)
    result = bytearray()
    try:
        with gzip.GzipFile(fileobj=BytesIO(raw)) as source:
            while chunk := source.read(1024 * 1024):
                checkpoint()
                result.extend(chunk)
                if len(result) > MAX_INFLATED:
                    _fail("VKS_BUNDLE_TOO_LARGE", "离线包解压后超过 80 MiB")
    except (OSError, EOFError, zlib.error):
        _fail("VKS_INVALID_ARCHIVE", "离线包 gzip 数据损坏")
    return bytes(result)


def _handbook(data, meta):
    if (
        meta.get("format") != "fh-gzip-html-1"
        or len(data) != meta.get("bytes")
        or sha256(data).hexdigest() != meta.get("sha256")
    ):
        _fail("VKS_HASH_MISMATCH", "离线手册解压后的字节与清单不一致")
    scripts = _scripts(data, {"source-data": "application/json"})
    docs = _json(scripts.get("source-data", "").encode())
    if not 1 <= len(docs) <= 4999:
        _fail("VKS_BUNDLE_TOO_LARGE", "离线手册文档数量超出支持范围")
    for row in docs.values():
        checkpoint()
        if not isinstance(row, dict):
            _fail("VKS_REFERENCE_INVALID", "离线手册原文索引无效")
        path = safe_path(row.get("path"))
        if PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}:
            _fail("VKS_MEDIA_TYPE_INVALID", "离线手册原文必须是 Markdown")
        content = _base64(row.get("base64"), MAX_FILE_BYTES)
        if sha256(content).hexdigest() != row.get("sha256"):
            _fail("VKS_HASH_MISMATCH", "离线手册内的原文摘要不一致")
        yield f"handbook/{path}", content


def _prototype(data):
    routes, pages, resources, assets = (data.get(key) for key in ("routeMap", "pages", "resources", "repoAssets"))
    if (
        not isinstance(routes, dict)
        or not 1 <= len(routes) <= 512
        or not isinstance(pages, dict)
        or not isinstance(resources, list)
        or not isinstance(assets, dict)
        or any(len(items) > 4999 for items in (pages, resources, assets))
    ):
        _fail("VKS_REFERENCE_INVALID", "离线原型包的路由、页面或资源索引无效")
    for name, index in assets.items():
        safe_path(name)
        if type(index) is not int or not 0 <= index < len(resources):
            _fail("VKS_REFERENCE_INVALID", "离线原型资源索引越界")
    grouped, aliases_seen = {}, set()
    for route, target in routes.items():
        checkpoint()
        route = "/" + route.removeprefix("/")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,255}", route) or route in aliases_seen:
            _fail("VKS_REFERENCE_INVALID", "离线原型含无效路由")
        aliases_seen.add(route)
        safe_path(route[1:])
        target = safe_path(target)
        page = pages.get(target)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("html"), str)
            or not isinstance(page.get("title"), str)
        ):
            _fail("VKS_REFERENCE_INVALID", "离线原型路由缺少声明页面")
        grouped.setdefault(target, []).append(route)
    for target, aliases in grouped.items():
        checkpoint()
        name = pages[target]["title"].strip() or PurePosixPath(target).stem
        yield {
            "pageKey": "charge-offline." + sha256(target.encode()).hexdigest()[:24],
            "name": name[:160],
            "routeAliases": sorted(aliases),
            "defaultStateKey": "default",
            "states": [
                {
                    "stateKey": "default",
                    "name": "默认状态",
                    "origin": "declared",
                    "anchors": [],
                }
            ],
            "actions": [],
        }


def offline_bundle(raw, project_key, baseline_key):
    if not any(marker.encode() in raw for marker in (HANDBOOK, PROTOTYPE, "fh-pack-meta")):
        return None
    data = _scripts(
        raw,
        {
            HANDBOOK: "application/octet-stream",
            PROTOTYPE: "application/octet-stream",
            "fh-pack-meta": "application/json",
        },
    )
    if not data:
        return None
    if (HANDBOOK in data) == (PROTOTYPE in data) or (("fh-pack-meta" in data) != (HANDBOOK in data)):
        _fail("VKS_INVALID_ARCHIVE", "离线包签名不完整或有冲突")
    is_handbook = HANDBOOK in data
    role = "documentation" if is_handbook else "prototype"
    source = "charge-offline-handbook" if is_handbook else "charge-offline-prototype"
    records, names, total = [], set(), 0
    identity = {"schema_version": "1.0", "project_key": project_key, "baseline_key": baseline_key}
    output = BytesIO()
    with ZipFile(output, "w") as archive:

        def add(path, content, kind, media):
            nonlocal total
            path = safe_path(path)
            total += len(content)
            if total > MAX_BUNDLE_BYTES or len(content) > MAX_FILE_BYTES:
                _fail("VKS_BUNDLE_TOO_LARGE", "离线包制品超出容量限制")
            if path.casefold() in names:
                _fail("VKS_DUPLICATE_PATH", "离线包包含重复或大小写歧义路径")
            names.add(path.casefold())
            digest = sha256(content).hexdigest()
            records.append(
                {
                    "path": path,
                    "kind": kind,
                    "media_type": media,
                    "source_id": source,
                    "size_bytes": len(content),
                    "sensitivity": "internal",
                    "sha256": digest,
                }
            )
            archive.writestr(path, content)
            return {"path": path, "sha256": digest}

        add("offline/original.html", raw, "fixture", "text/html")
        if is_handbook:
            for path, content in _handbook(_inflate(data[HANDBOOK]), _json(data["fh-pack-meta"].encode())):
                add(path, content, "markdown", "text/markdown")
            report = {"format": "charge-handbook", "documents": len(records) - 1}
        else:
            decoded = _json(_inflate(data[PROTOTYPE]))
            pages = list(_prototype(decoded))
            sidecar = {
                "schemaVersion": "1.0",
                "projectKey": project_key,
                "baselineKey": baseline_key,
                "sourceKey": source,
                "pages": pages,
            }
            identity["prototypeManifest"] = add(
                "offline/prototype.json", canonical_json(sidecar), "fixture", "application/json"
            )
            report = {
                "format": "charge-prototype",
                "routes": len(decoded["routeMap"]),
                "resources": len(decoded["resources"]),
                "pages": len(pages),
                "missing_surfaces": len(pages),
            }
        archive.writestr(
            "manifest.json",
            canonical_json(
                {
                    **identity,
                    "import_key": sha256(raw).hexdigest(),
                    "artifacts": records,
                    "sources": [
                        {
                            "source_id": source,
                            "role": role,
                            "provider": "local",
                            "root": ".",
                            "content_sha256": source_digest(records),
                        }
                    ],
                }
            ),
        )
    return output.getvalue(), report
