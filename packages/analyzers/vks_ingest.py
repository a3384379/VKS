"""Validate a VKS bundle without extracting or executing imported material.

All claims from a bundle are untrusted data. The parser verifies structure and
locators; business meaning remains a candidate for human review. No network or
subprocess APIs are used by this module.
"""

from __future__ import annotations

import json
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

from .cancellation import checkpoint

MAX_FILES = 5000
MAX_BUNDLE_BYTES = 100 * 1024 * 1024  # declared material bytes, excluding the container manifest
MAX_TRANSPORT_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
ANALYZER_VERSION = "vks-deterministic-0.1"
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "contracts"


class BundleError(ValueError):
    """A safe, structured rejection. Never includes imported file contents."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


@dataclass
class ParsedBundle:
    manifest: dict
    sources: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    analysis_metrics: dict = field(default_factory=dict)


def _fail(code: str, message: str, **details: Any) -> None:
    raise BundleError(code, message, details)


def canonical_json(value: Any) -> bytes:
    """vks-file-list-v1: sorted keys, compact UTF-8 JSON, integer sizes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def source_digest(artifacts: list[dict]) -> str:
    records = [{k: a[k] for k in ("path", "sha256", "size_bytes")} for a in artifacts]
    records.sort(key=lambda a: a["path"])
    return sha256(canonical_json(records)).hexdigest()


def safe_path(value: str, *, root: bool = False) -> str:
    if root and value == ".":
        return value
    if not isinstance(value, str) or not value or len(value) > 1024:
        _fail("VKS_UNSAFE_PATH", "包内路径格式无效")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        _fail("VKS_UNSAFE_PATH", "拒绝绝对路径或反斜杠路径")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        _fail("VKS_UNSAFE_PATH", "路径包含控制字符")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        _fail("VKS_UNSAFE_PATH", "拒绝越界或歧义路径")
    if unicodedata.normalize("NFC", value) != value:
        _fail("VKS_UNSAFE_PATH", "路径必须使用 NFC Unicode 规范形式")
    return str(PurePosixPath(value))


def _unique_json(pairs: list[tuple]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            _fail("VKS_DUPLICATE_KEY", "JSON 清单含重复字段")
        out[key] = value
    return out


def _json(data: bytes) -> dict:
    try:
        result = json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=_unique_json,
            parse_constant=lambda _: _fail("VKS_SCHEMA_INVALID", "JSON 不允许非有限数字"),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("VKS_SCHEMA_INVALID", "清单不是有效的 UTF-8 JSON")
    if not isinstance(result, dict):
        _fail("VKS_SCHEMA_INVALID", "清单必须是 JSON 对象")
    return result


def _schema(value: dict, name: str, *, definition: str | None = None) -> None:
    schema = _json((SCHEMA_ROOT / name).read_bytes())
    if definition:
        schema = {"$schema": schema["$schema"], "$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # Do not reflect arbitrary imported values (which may contain secrets).
        _fail(
            "VKS_SCHEMA_INVALID",
            "导入清单不符合正式契约",
            schema=name,
            field=".".join(str(p) for p in errors[0].absolute_path),
            rule=errors[0].validator,
        )


def _read_zip(data: bytes, require_manifest: bool = True) -> dict[str, bytes]:
    if not isinstance(data, bytes) or len(data) > MAX_TRANSPORT_BYTES:
        _fail("VKS_BUNDLE_TOO_LARGE", "ZIP 传输包超过 128 MiB 限制")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        _fail("VKS_INVALID_ARCHIVE", "导入文件不是有效 ZIP")
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2 + 1:
            _fail("VKS_BUNDLE_TOO_LARGE", "包内文件数量超过限制")
        total = 0
        files: dict[str, bytes] = {}
        seen = set()
        for entry in entries:
            checkpoint()
            path = safe_path(entry.filename[:-1] if entry.is_dir() else entry.filename)
            normalized = path.casefold()
            if normalized in seen:
                _fail("VKS_DUPLICATE_PATH", "包内包含重复或大小写歧义路径")
            seen.add(normalized)
            mode = (entry.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                _fail("VKS_UNSAFE_ARCHIVE", "不允许符号链接或特殊文件")
            if entry.flag_bits & 1:
                _fail("VKS_UNSAFE_ARCHIVE", "不接受加密 ZIP")
            if entry.is_dir():
                continue
            total += entry.file_size
            if entry.file_size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES + MAX_MANIFEST_BYTES:
                _fail("VKS_BUNDLE_TOO_LARGE", "解压后材料超过大小限制")
            if entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * MAX_COMPRESSION_RATIO:
                _fail("VKS_COMPRESSION_LIMIT", "压缩比超过安全限制")
            if path.lower().endswith((".zip", ".jar", ".war", ".tar", ".tgz", ".gz", ".7z", ".rar")):
                _fail("VKS_NESTED_ARCHIVE", "不接受嵌套压缩包")
            try:
                with archive.open(entry) as reader:
                    content = reader.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
                _fail("VKS_INVALID_ARCHIVE", "ZIP 文件校验失败")
            if len(content) != entry.file_size:
                _fail("VKS_INVALID_ARCHIVE", "ZIP 文件大小不一致")
            if content[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
                _fail("VKS_NESTED_ARCHIVE", "文件内容为嵌套 ZIP")
            files[path] = content
    # Allow the common "bundle-folder/manifest.json" packaging form only when
    # every file has the same top-level folder; never search arbitrary subtrees.
    if "manifest.json" not in files:
        roots = {p.split("/")[0] for p in files}
        if len(roots) == 1:
            prefix = next(iter(roots)) + "/"
            if prefix + "manifest.json" in files:
                files = {p[len(prefix) :]: v for p, v in files.items()}
    if require_manifest and "manifest.json" not in files:
        _fail("VKS_MANIFEST_MISSING", "ZIP 根目录缺少 manifest.json")
    materials = [raw for path, raw in files.items() if path != "manifest.json"]
    if len(materials) > MAX_FILES or sum(map(len, materials)) > MAX_BUNDLE_BYTES:
        _fail("VKS_BUNDLE_TOO_LARGE", "材料超过 5000 文件或 100 MiB 配额（manifest 单独计量）")
    if len(files.get("manifest.json", b"")) > MAX_MANIFEST_BYTES:
        _fail("VKS_BUNDLE_TOO_LARGE", "manifest 超过 4 MiB 元数据配额")
    checkpoint(force=True)
    return files


def _decode(data: bytes, encoding: str | None = None) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig"), "utf-8-bom"
        except UnicodeError:
            _fail("VKS_ENCODING_INVALID", "文本编码不可解析")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16-bom"
        except UnicodeError:
            _fail("VKS_ENCODING_INVALID", "文本编码不可解析")
    allowed = {"utf-8", "utf8", "gb18030", "gbk", "gb2312", "big5", "ascii", "latin-1", "iso-8859-1"}
    selected = (encoding or "utf-8").lower()
    if selected not in allowed:
        _fail("VKS_ENCODING_UNSUPPORTED", "不支持声明的文本编码")
    try:
        text = data.decode(selected)
    except UnicodeError:
        _fail("VKS_ENCODING_INVALID", "文本与声明的编码不符")
    if "\x00" in text:
        _fail("VKS_MEDIA_TYPE_INVALID", "文本材料中包含二进制空字节")
    return text, selected


def _lines(text: str, locator: dict) -> str:
    lines = text.splitlines()
    start, end = locator.get("startLine"), locator.get("endLine")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(lines):
        _fail("VKS_LOCATOR_INVALID", "来源行范围无法定位")
    return "\n".join(lines[start - 1 : end])


class _DOM(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.pages: set[str] = set()
        self.actions: dict[str, set[str | None]] = {}
        self.external_or_script = False
        try:
            self.feed(text)
        except Exception:
            _fail("VKS_HTML_INVALID", "HTML 无法解析")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        inherited = self.stack[-1][1] if self.stack else None
        page = d.get("data-vks-page") or inherited
        if d.get("data-vks-page"):
            self.pages.add(d["data-vks-page"])
        if d.get("data-vks-action"):
            self.actions.setdefault(d["data-vks-action"], set()).add(page)
        if tag in ("script", "iframe", "object", "embed") or any(k.startswith("on") for k in d):
            self.external_or_script = True
        if any(
            (d.get(k) or "").startswith(("http:", "https:", "//", "javascript:", "data:"))
            for k in ("src", "href", "action")
        ):
            self.external_or_script = True
        if tag not in (
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        ):
            self.stack.append((tag, page))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _headings(text: str) -> list[dict]:
    from markdown_it import MarkdownIt

    out, parents = [], []
    tokens = MarkdownIt("commonmark").parse(text)
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        level, title = int(token.tag[1:]), tokens[index + 1].content
        while parents and parents[-1][0] >= level:
            parents.pop()
        parents.append((level, title))
        out.append({"level": level, "title": title, "line": token.map[0] + 1,
                    "path": [p[1] for p in parents]})
    return out


def _entity(
    key: str, kind: str, name: str, content: str, path: str, locator: dict, metadata: dict | None = None
) -> dict:
    return {
        "stable_key": key,
        "type": kind,
        "display_name": name,
        "content": content,
        "artifact_path": path,
        "locator": locator,
        "metadata": metadata or {},
    }


def _document_key(path: str) -> str:
    return "document." + sha256(path.encode()).hexdigest()[:24]


def _parse_markdown(path: str, text: str, explicit: list[dict]) -> list[dict]:
    key = _document_key(path)
    headings = _headings(text)
    title = headings[0]["title"] if headings else PurePosixPath(path).stem
    entities = [
        _entity(
            key,
            "document",
            title,
            text,
            path,
            {"kind": "file", "path": path},
            {"source_kind": "imported", "manual_lock": True, "section_count": 0},
        )
    ]
    lines = text.splitlines()
    explicit_by_line = {}
    for section in explicit:
        if section["startLine"] in explicit_by_line:
            _fail("VKS_LOCATOR_INVALID", "多个章节声明使用同一开始行")
        body = _lines(text, section)
        matched = next((h for h in headings if h["line"] == section["startLine"]), None)
        if not matched or matched["path"] != section["headingPath"]:
            _fail("VKS_LOCATOR_INVALID", "章节标题路径与原文不一致")
        explicit_by_line[section["startLine"]] = section
    counts: dict[str, int] = {}
    if not headings:
        headings = [{"line": 1, "title": title, "path": [title], "level": 1}]
    for i, h in enumerate(headings):
        declared = explicit_by_line.get(h["line"])
        heading_token = sha256(canonical_json(h["path"])).hexdigest()[:16]
        counts[heading_token] = counts.get(heading_token, 0) + 1
        section_key = declared["sectionKey"] if declared else f"{key}.section.{heading_token}.{counts[heading_token]}"
        # A section includes its immediate text, stopping at the next heading.
        # Explicit sidecars may include a larger range; their exact locator wins.
        end = (
            declared["endLine"] if declared else (headings[i + 1]["line"] - 1 if i + 1 < len(headings) else len(lines))
        )
        locator = {
            "kind": "document_section",
            "sectionKey": section_key,
            "startLine": h["line"],
            "endLine": max(h["line"], end),
        }
        body = _lines(text, locator) if lines else ""
        entities.append(
            _entity(
                section_key,
                "section",
                h["title"],
                body,
                path,
                locator,
                {
                    "document_key": key,
                    "heading_path": h["path"],
                    "ordinal": i,
                    "identity_origin": "explicit" if declared else "heading_path",
                    "semantic_content_sha256": sha256(body.encode()).hexdigest(),
                    "manual_lock": True,
                },
            )
        )
    entities[0]["metadata"]["section_count"] = len(entities) - 1
    return entities


def _parse_openapi(path: str, text: str, source: dict, issues: list[dict]) -> list[dict]:
    try:
        if text.lstrip().startswith("{"):
            document = _json(text.encode())
        else:
            import yaml

            # Refuse recursive/expansive aliases before constructing a YAML graph.
            tokens = list(yaml.scan(text))
            if any(isinstance(t, yaml.tokens.AliasToken) for t in tokens):
                _fail("VKS_OPENAPI_INVALID", "OpenAPI YAML 不接受别名引用")
            document = yaml.safe_load(text)
    except BundleError:
        raise
    except Exception:
        _fail("VKS_OPENAPI_INVALID", "OpenAPI 材料无法解析")
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        _fail("VKS_OPENAPI_INVALID", "OpenAPI 缺少 paths 对象")
    if not (str(document.get("openapi", "")).startswith("3.") or document.get("swagger") == "2.0"):
        _fail("VKS_OPENAPI_UNSUPPORTED", "当前支持 OpenAPI 3.x 或 Swagger 2.0")
    entities = []
    service = source.get("service_key", source["source_id"])
    for route, methods in document["paths"].items():
        if not isinstance(route, str) or not route.startswith("/") or not isinstance(methods, dict):
            _fail("VKS_OPENAPI_INVALID", "OpenAPI 路由格式无效")
        for method, operation in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
                continue
            if not isinstance(operation, dict):
                _fail("VKS_OPENAPI_INVALID", "OpenAPI 操作必须是对象")
            canonical = {"service": service, "method": method.upper(), "route": route, "conditions": {}}
            token = sha256(canonical_json(canonical)).hexdigest()[:28]
            key = f"api.{service}.{token}"
            display = f"{method.upper()} {route}"
            metadata = {
                "service_key": service,
                "method": method.upper(),
                "normalized_route": route,
                "condition_fingerprint": sha256(canonical_json({})).hexdigest(),
                "operation_id": operation.get("operationId"),
                "summary": operation.get("summary", ""),
                "resolution_state": "resolved",
                "evidence_kind": "static",
                "runtime_source_alignment": "unknown",
                "parser_version": ANALYZER_VERSION,
            }
            entities.append(
                _entity(
                    key,
                    "api",
                    display,
                    json.dumps(operation, ensure_ascii=False, indent=2),
                    path,
                    {"kind": "openapi_operation", "method": method.upper(), "path": route},
                    metadata,
                )
            )
    issues.append(
        {
            "code": "VKS_RUNTIME_UNVERIFIED",
            "severity": "info",
            "artifact_path": path,
            "message": "接口来自规范声明；未调用业务服务，运行行为与源码对齐尚未验证。",
        }
    )
    return entities


def _validate_relation(from_entity: dict, to_entity: dict, relation_type: str) -> None:
    a, b = from_entity["type"], to_entity["type"]
    legal = {
        "documents": lambda: a != "section" and b == "section",
        "renders": lambda: a in ("page", "symbol") and b in ("action", "page"),
        "handler_of": lambda: a == "symbol" and b in ("action", "api"),
        "may_call": lambda: a in ("action", "symbol", "api") and b in ("api", "symbol"),
        "observed_during": lambda: a in ("action", "api") and b in ("action", "api"),
        "implements": lambda: a in ("api", "symbol") and b in ("symbol", "api"),
        "maps_to_sql": lambda: a == "symbol" and b == "sql",
        "reads_table": lambda: a in ("sql", "symbol") and b == "table",
        "writes_table": lambda: a in ("sql", "symbol") and b == "table",
        "contains": lambda: (
            a in ("document", "page", "scenario", "symbol")
            and (
                a != b
                or a == "symbol"
                and from_entity["metadata"].get("kind") in ("class", "interface")
                and to_entity["metadata"].get("kind") in ("method", "constructor")
                and from_entity["stable_key"] != to_entity["stable_key"]
            )
        ),
        "depends_on": lambda: a != b or from_entity["stable_key"] != to_entity["stable_key"],
    }
    if relation_type not in legal or not legal[relation_type]():
        _fail("VKS_RELATION_INVALID", "关系端点类型不符合约束", relation_type=relation_type)


def parse_bundle(data: bytes, expected_project_key: str) -> ParsedBundle:
    """Verify a ZIP and return immutable inputs plus unreviewed knowledge facts."""
    files = _read_zip(data)
    manifest = _json(files["manifest.json"])
    _schema(manifest, "import_manifest.schema.json")
    if manifest["project_key"] != expected_project_key:
        _fail("VKS_PROJECT_MISMATCH", "导入包不属于当前项目")
    result = ParsedBundle(manifest=manifest)
    partial_allowed = not manifest.get("uiManifest") and not manifest.get("bindings") and len(manifest["artifacts"]) > 1
    failed_paths = set()

    def isolate_failure(item, error):
        if not partial_allowed or error.code not in {
            "VKS_ENCODING_INVALID",
            "VKS_ENCODING_UNSUPPORTED",
            "VKS_OPENAPI_INVALID",
        }:
            raise error
        failed_paths.add(item["path"])
        item.update(sensitivity="confidential", parse_status="failed", parse_error_code=error.code)
        result.issues.append(
            {
                "code": error.code,
                "severity": "warning",
                "artifact_path": item["path"],
                "message": "独立材料无法解析；保留受限原始字节与问题清单，其余材料继续导入。",
            }
        )

    sources: dict[str, dict] = {}
    for source in manifest["sources"]:
        if source["source_id"] in sources:
            _fail("VKS_DUPLICATE_KEY", "来源 source_id 重复")
        safe_path(source["root"], root=True)
        sources[source["source_id"]] = dict(source)
    artifacts: dict[str, dict] = {}
    texts: dict[str, str] = {}
    for record in manifest["artifacts"]:
        checkpoint()
        path = safe_path(record["path"])
        if path == "manifest.json" or path in artifacts:
            _fail("VKS_DUPLICATE_PATH", "制品路径重复或包含根清单自身")
        if record["source_id"] not in sources:
            _fail("VKS_REFERENCE_INVALID", "制品引用不存在的来源")
        source = sources[record["source_id"]]
        root = source["root"]
        if root != "." and not (path == root or path.startswith(root + "/")):
            _fail("VKS_SOURCE_SCOPE", "制品越出声明来源目录")
        if path not in files:
            _fail("VKS_ARTIFACT_MISSING", "清单声明的制品不存在")
        content = files[path]
        if len(content) != record["size_bytes"] or sha256(content).hexdigest() != record["sha256"]:
            _fail("VKS_HASH_MISMATCH", "制品实际字节与清单大小或摘要不符")
        parts = PurePosixPath(path).parts
        if any(p in (".git", ".svn", "node_modules", "__pycache__") for p in parts) or any(
            p.lower() in (".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519") for p in parts
        ):
            _fail("VKS_PRIVATE_MATERIAL", "包内含凭据文件或管理目录，需先排除")
        if record["kind"] in ("capture", "trace"):
            _fail("VKS_CAPTURE_UNVERIFIED", "当前未提供采集脱敏核验器，拒绝保存原始录制或 Trace 包")
        suffix = PurePosixPath(path).suffix.lower()
        if record["kind"] == "markdown" and suffix not in (".md", ".markdown"):
            _fail("VKS_MEDIA_TYPE_INVALID", "Markdown 制品扩展名不匹配")
        if record["kind"] in ("ui_manifest", "binding_manifest") and suffix != ".json":
            _fail("VKS_MEDIA_TYPE_INVALID", "侧车清单必须是 JSON 文件")
        if record["kind"] == "openapi" and suffix not in (".json", ".yaml", ".yml"):
            _fail("VKS_MEDIA_TYPE_INVALID", "OpenAPI 文件扩展名不匹配")
        item = dict(record, content=content)
        if record["kind"] in ("markdown", "openapi", "ui_manifest", "binding_manifest") or (
            record["kind"] in ("source", "prototype")
            and (
                record["media_type"].startswith(("text/", "application/json", "application/xml"))
                or suffix
                in (
                    ".java",
                    ".py",
                    ".js",
                    ".jsx",
                    ".ts",
                    ".tsx",
                    ".vue",
                    ".xml",
                    ".html",
                    ".htm",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".properties",
                    ".toml",
                    ".ini",
                    ".sh",
                    ".ps1",
                )
            )
        ):
            try:
                text, encoding = _decode(content, record.get("encoding"))
            except BundleError as exc:
                isolate_failure(item, exc)
                artifacts[path] = item
                continue
            texts[path] = text
            item["detected_encoding"] = encoding
            # Block common credential literals before any content reaches index.
            # Raw unrecognized material is not claimed to be comprehensively scanned.
            if re.search(
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b",
                text,
            ) or re.search(
                r"""(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*["']([^"'\r\n]{8,})["']""",
                text,
            ):
                _fail("VKS_SECRET_DETECTED", "材料中发现疑似秘密，请脱敏后导入")
        artifacts[path] = item
    extras = set(files) - set(artifacts) - {"manifest.json"}
    if extras:
        _fail("VKS_UNDECLARED_ARTIFACT", "包内存在清单未声明的文件", count=len(extras))
    for source_id, source in sources.items():
        members = [a for a in manifest["artifacts"] if a["source_id"] == source_id]
        if not members or source_digest(members) != source["content_sha256"]:
            _fail("VKS_SOURCE_HASH_MISMATCH", "来源文件清单摘要不一致")
        source["provider_revision"] = source.get("revision") or source["content_sha256"]
    if failed_paths and len(failed_paths) == len(artifacts):
        _fail("VKS_NO_PARSABLE_MATERIAL", "所有材料均无法解析，未提交任何知识结果")
    result.sources = list(sources.values())
    result.artifacts = list(artifacts.values())
    sidecars: dict[str, dict] = {}
    for field, schema_name, expected_kind in [
        ("uiManifest", "import_manifest.schema.json", "ui_manifest"),
        ("bindings", "binding_manifest.schema.json", "binding_manifest"),
    ]:
        reference = manifest.get(field)
        if reference is None:
            continue
        path = safe_path(reference["path"])
        artifact = artifacts.get(path)
        if not artifact or artifact["kind"] != expected_kind or artifact["sha256"] != reference["sha256"]:
            _fail("VKS_REFERENCE_INVALID", "侧车清单引用无效")
        sidecar = _json(artifact["content"])
        _schema(sidecar, schema_name, definition="uiManifestDocument" if field == "uiManifest" else None)
        if sidecar["projectKey"] != expected_project_key or sidecar["baselineKey"] != manifest["baseline_key"]:
            _fail("VKS_BASELINE_MISMATCH", "侧车清单与项目或基线不一致")
        sidecars[field] = sidecar
    ui = sidecars.get("uiManifest", {"pages": [], "actions": [], "documentSections": [], "codeEntities": []})
    entity_map: dict[str, dict] = {}

    def add(entity: dict) -> None:
        if entity["stable_key"] in entity_map:
            _fail("VKS_DUPLICATE_LOGICAL_KEY", "同一基线的逻辑 key 重复")
        entity_map[entity["stable_key"]] = entity
        result.entities.append(entity)

    def referenced_artifact(row: dict, allowed_kinds: tuple[str, ...] | None = None) -> tuple[dict, str]:
        path = safe_path(row["artifactPath"])
        artifact = artifacts.get(path)
        if not artifact or artifact["sha256"] != row["sha256"]:
            _fail("VKS_REFERENCE_INVALID", "实体来源文件或摘要不一致")
        if allowed_kinds and artifact["kind"] not in allowed_kinds:
            _fail("VKS_MEDIA_TYPE_INVALID", "实体来源材料类型不匹配")
        if path not in texts:
            _fail("VKS_MEDIA_TYPE_INVALID", "实体定位需要可解析文本")
        return artifact, texts[path]

    declared_keys = [x["logicalKey"] for field in ("pages", "actions", "codeEntities") for x in ui.get(field, [])] + [
        x["sectionKey"] for x in ui["documentSections"]
    ]
    if len(declared_keys) != len(set(declared_keys)):
        _fail("VKS_DUPLICATE_LOGICAL_KEY", "同一基线的逻辑 key 重复")
    explicit_by_path: dict[str, list[dict]] = {}
    for section in ui["documentSections"]:
        referenced_artifact(section, ("markdown",))
        explicit_by_path.setdefault(section["artifactPath"], []).append(section)
    for path, artifact in artifacts.items():
        checkpoint()
        if path in failed_paths:
            continue
        if artifact["kind"] == "markdown":
            for entity in _parse_markdown(path, texts[path], explicit_by_path.get(path, [])):
                add(entity)
        elif artifact["kind"] == "openapi":
            try:
                indexed = _parse_openapi(path, texts[path], sources[artifact["source_id"]], result.issues)
            except BundleError as exc:
                isolate_failure(artifact, exc)
                continue
            for entity in indexed:
                add(entity)
    if failed_paths and len(failed_paths) == len(artifacts):
        _fail("VKS_NO_PARSABLE_MATERIAL", "所有材料均无法解析，未提交任何知识结果")
    doms: dict[str, _DOM] = {}
    for page in ui["pages"]:
        artifact, text = referenced_artifact(page, ("prototype", "source"))
        dom = doms.setdefault(page["artifactPath"], _DOM(text))
        if page["logicalKey"] not in dom.pages:
            _fail("VKS_LOCATOR_INVALID", "页面 logicalKey 无法在 DOM 属性定位")
        add(
            _entity(
                page["logicalKey"],
                "page",
                page["name"],
                text,
                page["artifactPath"],
                {"kind": "dom_attribute", "pageKey": page["logicalKey"]},
                {
                    "preview_mode": "snapshot",
                    "state_key": page.get("stateKey"),
                    "source_kind": "manual",
                    "runtime_source_alignment": "unknown",
                    "script_content_blocked": dom.external_or_script,
                },
            )
        )
        if dom.external_or_script:
            result.issues.append(
                {
                    "code": "VKS_PREVIEW_DEGRADED",
                    "severity": "warning",
                    "artifact_path": page["artifactPath"],
                    "message": "原型含脚本或外联；讲解预览必须禁用脚本、表单、导航及网络。",
                }
            )
    for action in ui["actions"]:
        page = entity_map.get(action["pageKey"])
        if not page or page["type"] != "page":
            _fail("VKS_REFERENCE_INVALID", "动作所属页面不存在")
        dom = doms[page["artifact_path"]]
        if dom.actions.get(action["logicalKey"]) != {action["pageKey"]}:
            _fail("VKS_LOCATOR_INVALID", "动作无法唯一归属声明页面的 DOM 属性")
        add(
            _entity(
                action["logicalKey"],
                "action",
                action["label"],
                action["label"],
                page["artifact_path"],
                {"kind": "dom_attribute", "pageKey": action["pageKey"], "actionKey": action["logicalKey"]},
                {
                    "page_key": action["pageKey"],
                    "event": action["event"],
                    "evidence_origin": "manual",
                    "runtime_source_alignment": "unknown",
                },
            )
        )
    for code in ui.get("codeEntities", []):
        artifact, text = referenced_artifact(code)
        snippet = _lines(text, code)
        # External code locations are manual anchors, never analyzer-verified calls.
        # Require at least the leaf identifier to be present at the stated span.
        leaf = re.split(r"[.#:/()]", code["qualifiedName"].rstrip("()"))[-1]
        if leaf and leaf not in snippet:
            _fail("VKS_LOCATOR_INVALID", "代码限定名称不在声明行范围内")
        add(
            _entity(
                code["logicalKey"],
                code["entityType"],
                code["qualifiedName"],
                snippet,
                code["artifactPath"],
                {
                    "kind": "file_lines",
                    "startLine": code["startLine"],
                    "endLine": code["endLine"],
                    "qualifiedName": code["qualifiedName"],
                },
                {
                    "qualified_name": code["qualifiedName"],
                    "resolution_state": "external",
                    "evidence_kind": "manual",
                    "parser_version": ANALYZER_VERSION,
                    "service_key": sources[artifact["source_id"]].get("service_key", artifact["source_id"]),
                },
            )
        )
    binding = sidecars.get("bindings", {"evidence": [], "bindings": []})
    evidence_map = {}
    for record in binding["evidence"]:
        checkpoint()
        if record["evidenceKey"] in evidence_map:
            _fail("VKS_DUPLICATE_KEY", "证据逻辑 key 重复")
        artifact, text = referenced_artifact(record)
        locator = record["locator"]
        kind = record["evidenceKind"]
        if locator["kind"] == "network_request" or kind == "observed":
            # The first slice has no registered capture verifier. Rejecting is
            # safer than accepting a fabricated observation or causal claim.
            _fail("VKS_CAPTURE_UNVERIFIED", "本轮不接受未经受控 Runner 核验的运行证据")
        if locator["kind"] in ("file_lines", "document_section"):
            snippet = _lines(text, locator)
            if locator["kind"] == "document_section":
                section = entity_map.get(locator["sectionKey"])
                if (
                    not section
                    or section["type"] != "section"
                    or section["artifact_path"] != record["artifactPath"]
                    or any(section["locator"].get(k) != locator[k] for k in ("startLine", "endLine"))
                ):
                    _fail("VKS_LOCATOR_INVALID", "章节证据定位与已解析章节不一致")
        elif locator["kind"] == "dom_attribute":
            page = entity_map.get(locator["pageKey"])
            action = entity_map.get(locator["actionKey"])
            if (
                not page
                or not action
                or action["type"] != "action"
                or action["metadata"]["page_key"] != locator["pageKey"]
                or page["artifact_path"] != record["artifactPath"]
            ):
                _fail("VKS_LOCATOR_INVALID", "DOM 证据与页面动作不匹配")
            snippet = f'data-vks-page="{locator["pageKey"]}"; data-vks-action="{locator["actionKey"]}"'
        else:
            _fail("VKS_LOCATOR_INVALID", "不支持的证据定位方式")
        evidence = {
            "stable_key": record["evidenceKey"],
            "kind": kind,
            "artifact_path": record["artifactPath"],
            "locator": dict(locator),
            "sha256": artifact["sha256"],
            "claim": record.get("claim", ""),
            "content": snippet,
            "extractor_version": ANALYZER_VERSION,
        }
        evidence_map[record["evidenceKey"]] = evidence
        result.evidence.append(evidence)
    seen_relations = set()
    for relation in binding["bindings"]:
        source = entity_map.get(relation["fromLogicalKey"])
        target = entity_map.get(relation["toLogicalKey"])
        if not source or not target or not all(k in evidence_map for k in relation["evidenceRefs"]):
            _fail("VKS_REFERENCE_INVALID", "绑定端点或证据引用不存在")
        _validate_relation(source, target, relation["relationType"])
        identity = (source["stable_key"], target["stable_key"], relation["relationType"])
        if identity in seen_relations:
            _fail("VKS_DUPLICATE_RELATION", "同一关系重复声明")
        seen_relations.add(identity)
        if relation["relationType"] == "observed_during":
            _fail("VKS_CAPTURE_UNVERIFIED", "运行邻接关系需要已核验的采集证据")
        result.relations.append(
            {
                "from_key": source["stable_key"],
                "to_key": target["stable_key"],
                "type": relation["relationType"],
                "evidence_keys": relation["evidenceRefs"],
                "reason": relation.get("reason", ""),
                "causal_basis": "not_established",
            }
        )
    from .java_spring import analyze_sources

    try:
        analysis = analyze_sources([a for a in result.artifacts if a["path"] not in failed_paths], sources, texts)
    except ValueError:
        _fail("VKS_ANALYZER_LIMIT", "源码静态分析超过安全声明数量限制")
    for entity in analysis.entities:
        add(entity)
    for evidence in analysis.evidence:
        if evidence["stable_key"] in evidence_map:
            _fail("VKS_DUPLICATE_LOGICAL_KEY", "显式证据与解析器生成证据的逻辑 key 冲突")
        evidence_map[evidence["stable_key"]] = evidence
        result.evidence.append(evidence)
    for relation in analysis.relations:
        _validate_relation(entity_map[relation["from_key"]], entity_map[relation["to_key"]], relation["type"])
        result.relations.append(relation)
    result.issues.extend(analysis.issues)
    result.analysis_metrics = analysis.metrics
    # Keep independent source declarations. An exact contract/handler route
    # match produces an auditable candidate, not an automatic merge or approval.
    spring = [e for e in analysis.entities if e["type"] == "api" and e["metadata"].get("api_origin") == "spring"]
    specifications = [
        e
        for e in result.entities
        if e["type"] == "api" and e["artifact_path"] in artifacts and artifacts[e["artifact_path"]]["kind"] == "openapi"
    ]
    for specification in specifications:
        metadata = specification["metadata"]
        matches = [
            e
            for e in spring
            if all(
                e["metadata"].get(k) == metadata.get(k)
                for k in ("service_key", "method", "normalized_route", "condition_fingerprint")
            )
        ]
        specification["metadata"]["parallel_declaration_keys"] = [e["stable_key"] for e in matches]
        if len(matches) == 1 and matches[0]["metadata"]["resolution_state"] == "resolved":
            target = matches[0]
            path = specification["artifact_path"]
            evidence_key = (
                "java.spec."
                + sha256(canonical_json([path, specification["stable_key"], artifacts[path]["sha256"]])).hexdigest()[
                    :40
                ]
            )
            if evidence_key not in evidence_map:
                evidence = {
                    "stable_key": evidence_key,
                    "kind": "static",
                    "artifact_path": path,
                    "locator": {"kind": "file_lines", "startLine": 1, "endLine": len(texts[path].splitlines())},
                    "sha256": artifacts[path]["sha256"],
                    "claim": "固定 OpenAPI 中存在相同服务、HTTP 方法及路径声明",
                    "content": specification["content"],
                    "extractor_version": analysis.metrics["parser_version"],
                }
                evidence_map[evidence_key] = evidence
                result.evidence.append(evidence)
            handler = target["metadata"]["handler_key"]
            mapping_evidence = next(
                r["evidence_keys"]
                for r in analysis.relations
                if r["type"] == "handler_of" and r["to_key"] == target["stable_key"]
            )
            result.relations.append(
                {
                    "from_key": specification["stable_key"],
                    "to_key": handler,
                    "type": "implements",
                    "evidence_keys": [evidence_key, *mapping_evidence],
                    "reason": "规范和 Spring 声明的服务、方法、路径及条件精确匹配；仍需核对来源版本、网关与业务含义",
                    "causal_basis": "static_potential",
                }
            )
        elif matches:
            result.issues.append(
                {
                    "code": "VKS_API_DECLARATION_AMBIGUOUS",
                    "severity": "warning",
                    "artifact_path": specification["artifact_path"],
                    "message": "规范匹配多个或歧义 Spring 处理器，保留全部来源且不自动选择实现。",
                }
            )
    from .document_links import add_candidates

    add_candidates(result, entity_map, artifacts, evidence_map, texts)
    for artifact in result.artifacts:
        checkpoint()
        if artifact["kind"] == "source" and artifact["path"] not in analysis.supported_paths:
            result.issues.append(
                {
                    "code": "VKS_SOURCE_ANCHORS_ONLY",
                    "severity": "info",
                    "artifact_path": artifact["path"],
                    "message": "此文件超出当前静态解析支持范围；仅保留原始来源与显式定位。",
                }
            )
    if manifest.get("allow_platform_llm"):
        result.issues.append(
            {
                "code": "VKS_MODEL_NOT_CONFIGURED",
                "severity": "info",
                "message": "本轮使用确定性解析，未配置或调用平台模型。",
            }
        )
    return result
