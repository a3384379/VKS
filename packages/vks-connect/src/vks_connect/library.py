"""Read the inert data envelope of a local VKS read-only library."""

import base64
import gzip
import io
import json
import math
import os
import re
import stat
import tempfile
import threading
import unicodedata
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from .library_vectors import LibraryVectors
from .local_embeddings import DIMENSIONS, MODEL, PROFILE
from .sidebar import IMAGE_MAX_BYTES, RASTER_TYPES

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_DATA_BYTES = 256 * 1024 * 1024
PAYLOAD = re.compile(
    rb'<script\s+id="vks-payload"\s+type="application/octet-stream"\s+'
    rb'data-encoding="gzip-base64"\s*>\s*([A-Za-z0-9+/=]+)\s*</script>'
)
SHA256 = re.compile(r"[a-f0-9]{64}")
IDENTITY = re.compile(r"[a-zA-Z0-9_.:-]{1,96}")
ASCII_TERMS = re.compile(r"[a-z0-9_]+")
CJK_TERMS = re.compile(r"[\u3400-\u9fff]+")


class LibraryError(ValueError):
    def __init__(self, message, code="VKS_LIBRARY_INVALID"):
        super().__init__(message)
        self.code = code


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def terms(value):
    yield from ASCII_TERMS.findall(value)
    for run in CJK_TERMS.findall(value):
        if len(run) == 1:
            yield run
        else:
            yield from (run[i : i + 2] for i in range(len(run) - 1))


def string(row, key, maximum):
    value = row[key]
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(key)
    return value


def identity(row, key="id"):
    value = string(row, key, 96)
    if not IDENTITY.fullmatch(value):
        raise ValueError(key)
    return value


def items(row, key, maximum):
    value = row[key]
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(key)
    return value


@dataclass
class Section:
    title: str
    document_title: str
    content: str
    images: dict
    indexed_title: str
    indexed_text: str
    counts: Counter


class Library:
    def __init__(self, path, digest, payload):
        if payload["schema_version"] != 1:
            raise ValueError("schema_version")
        self.path = path
        self.ref = digest
        project, snapshot = payload["project"], payload["snapshot"]
        self.project_id = identity(project)
        self.project_name = string(project, "name", 2000)
        self.snapshot_id = identity(snapshot)
        self.version = string(snapshot, "name", 160)
        self.manifest = string(snapshot, "sha256", 64)
        if not self.project_name or not self.version or not SHA256.fullmatch(self.manifest):
            raise ValueError("snapshot")
        self.sections = {}
        for document in items(payload, "documents", 10000):
            title = string(document, "title", 2000)
            images = document.get("images", {})
            if not isinstance(images, dict):
                raise TypeError("images")
            for section in items(document, "sections", 60000):
                sid = identity(section)
                if sid in self.sections or len(self.sections) >= 60000:
                    raise ValueError("sections")
                heading = string(section, "title", 2000)
                content = string(section, "content", 2_000_000)
                indexed_title, indexed_text = normalized(title + " " + heading), normalized(content)
                self.sections[sid] = Section(
                    heading,
                    title,
                    content,
                    images,
                    indexed_title,
                    indexed_text,
                    Counter(terms(indexed_title + " " + indexed_text)),
                )
        self.bindings = {}
        self.section_bindings = defaultdict(list)
        for binding in items(payload, "bindings", 60000):
            bid = identity(binding)
            references = items(binding, "section_ids", 1000)
            if bid in self.bindings or not all(isinstance(sid, str) and sid in self.sections for sid in references):
                raise ValueError("bindings")
            self.bindings[bid] = {
                "id": bid,
                "title": string(binding, "title", 2000),
                "summary": string(binding, "summary", 2_000_000),
                "section_ids": references,
            }
            for sid in references:
                self.section_bindings[sid].append(bid)
        self.vectors = LibraryVectors.read(payload.get("rag"), self.sections)
        plan = payload.get("chatgpt_plan")
        eligible = plan.get("section_ids") if isinstance(plan, dict) else None
        self.plan_sections = frozenset(
            sid for sid in eligible if isinstance(sid, str) and sid in self.sections
        ) if isinstance(eligible, list) else frozenset()
        local = payload.get("local_rag")
        self.local_vectors = None
        if isinstance(local, dict) and (local.get("profile"), local.get("model"), local.get("dimensions")) == (
            PROFILE, MODEL, DIMENSIONS
        ):
            self.local_vectors = LibraryVectors.read(local, self.sections)

    def context(self):
        return {
            "library_ref": self.ref,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "manifest_sha256": self.manifest,
            "file_name": self.path.name,
            "file_url": self.url(),
            "section_count": len(self.sections),
            "binding_count": len(self.bindings),
            "vector_index": self.vectors.info() if self.vectors else None,
            "local_vector_index": self.local_vectors.info() if self.local_vectors else None,
        }

    def section(self, section_id):
        if section_id not in self.sections:
            raise LibraryError("成品库未包含该章节。", "VKS_LIBRARY_SECTION_MISSING")
        return self.sections[section_id]

    def url(self, section_id=None):
        if section_id is None:
            return self.path.as_uri()
        self.section(section_id)
        return self.path.as_uri() + "#" + urlencode({"section": section_id})

    def reference(self, sid):
        section = self.sections[sid]
        return {
            "section_id": sid,
            "title": section.title,
            "document_title": section.document_title,
            "file_url": self.url(sid),
        }

    def search(self, query, limit, vector=None, *, index=None, eligible=None):
        query = normalized(query).strip()
        if not query:
            return []
        query_terms = set(terms(query))
        scored = []
        for sid, section in self.sections.items():
            if eligible is not None and sid not in eligible:
                continue
            score = sum(
                (1 + math.log(section.counts[term])) / (1 + len(section.indexed_text) / 2400)
                + (4 if term in section.indexed_title else 0)
                for term in query_terms
                if section.counts[term]
            )
            if query in section.indexed_title:
                score += 20
            if query in section.indexed_text:
                score += 4
            if score:
                scored.append((-score, sid))
        candidates = max(30, limit * 2)
        lexical = [sid for _, sid in sorted(scored)[:candidates]]
        index = index if index is not None else self.vectors
        semantic = index.rank(vector, candidates, eligible=eligible) if vector is not None and index else []
        ranking = lexical
        if semantic:
            fusion = defaultdict(float)
            for matches in (lexical, semantic):
                for rank, sid in enumerate(matches, 1):
                    fusion[sid] += 1 / (60 + rank)
            ranking = sorted(fusion, key=lambda sid: (-fusion[sid], sid))
        results = []
        for sid in ranking[:limit]:
            section = self.sections[sid]
            # Search text is normalized, but excerpts always use original characters.
            paragraph = next(
                (part for part in section.content.splitlines() if query_terms.intersection(terms(normalized(part)))),
                section.content,
            )
            results.append(
                {
                    **self.reference(sid),
                    "excerpt": paragraph[:480],
                    "binding_count": len(self.section_bindings.get(sid, ())),
                    "match": "hybrid"
                    if sid in semantic and sid in lexical
                    else "semantic"
                    if sid in semantic
                    else "keyword",
                }
            )
        return results

    def read(self, section_id, offset, max_chars):
        content = self.section(section_id).content
        if offset > len(content):
            raise LibraryError("章节续读位置超出范围。", "VKS_LIBRARY_INVALID_INPUT")
        end = min(offset + max_chars, len(content))
        return {
            **self.reference(section_id),
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "content": content[offset:end],
            "binding_count": len(self.section_bindings.get(section_id, ())),
        }

    def associations(self, section_id, offset, limit):
        self.section(section_id)
        ids = self.section_bindings.get(section_id, ())
        end = min(offset + limit, len(ids))
        return {
            "offset": offset,
            "next_offset": end if end < len(ids) else None,
            "bindings": [
                {
                    "id": bid,
                    "title": self.bindings[bid]["title"],
                    "summary": self.bindings[bid]["summary"][:2400],
                    "sections": [self.reference(sid) for sid in self.bindings[bid]["section_ids"][:12]],
                }
                for bid in ids[offset:end]
            ],
        }

    def image(self, section_id, path):
        value = self.section(section_id).images.get(path)
        if isinstance(value, str) and len(value) <= 4 * ((IMAGE_MAX_BYTES + 2) // 3) + 64:
            prefix, _, data = value.partition(",")
            media = prefix.removeprefix("data:").removesuffix(";base64")
            if prefix == f"data:{media};base64" and media in RASTER_TYPES:
                try:
                    raw = base64.b64decode(data, validate=True)
                    if 0 < len(raw) <= IMAGE_MAX_BYTES:
                        return media, data
                except ValueError:
                    pass
        raise LibraryError("成品库未包含可预览的图片。", "VKS_LIBRARY_IMAGE_MISSING")


def file_signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_envelope(path):
    try:
        path = path.expanduser().resolve(strict=True)
        if path.suffix.lower() not in {".html", ".htm"} or not path.is_file():
            raise ValueError("path")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError("size")
            raw = stream.read(MAX_FILE_BYTES + 1)
            signature = file_signature(os.fstat(stream.fileno()))
        if signature != file_signature(before) or len(raw) > MAX_FILE_BYTES:
            raise ValueError("changed")
        span, payload = decode_envelope(raw)
        return path, raw, span, payload, signature
    except OSError as error:
        if isinstance(error, gzip.BadGzipFile):
            raise LibraryError("成品库内容损坏，请重新选择。") from None
        raise LibraryError("无法读取成品库文件，请检查本机路径或重新选择。", "VKS_LIBRARY_UNAVAILABLE") from None
    except (ValueError, TypeError, KeyError, EOFError, RecursionError, zlib.error):
        raise LibraryError("请选择有效的 VKS 只读成品库 HTML。") from None


def decode_envelope(raw):
    matches = PAYLOAD.finditer(raw)
    first = next(matches, None)
    if first is None or next(matches, None) is not None:
        raise ValueError("envelope")
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(first[1], validate=True))) as data:
        decoded = data.read(MAX_DATA_BYTES + 1)
    if len(decoded) > MAX_DATA_BYTES:
        raise ValueError("size")
    return first.span(1), json.loads(decoded)


def read_library(path):
    path, raw, _, payload, signature = read_envelope(path)
    try:
        return Library(path, sha256(raw).hexdigest(), payload), signature
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise LibraryError("请选择有效的 VKS 只读成品库 HTML。") from None


class LibraryStore:
    def __init__(self, preference_path):
        self.preference_path = preference_path
        self.lock = threading.Lock()
        self.cached = None

    def select(self, path):
        with self.lock:
            library, signature = read_library(Path(path))
            self.preference_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.preference_path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump({"path": str(library.path)}, stream, ensure_ascii=False)
                temporary.replace(self.preference_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self.cached = (library, signature)
            return library

    def current(self, expected_ref=None):
        with self.lock:
            try:
                with self.preference_path.open(encoding="utf-8") as stream:
                    picked = json.loads(stream.read(32768))
                path = Path(picked["path"])
                signature = file_signature(path.stat())
            except FileNotFoundError:
                raise LibraryError("请选择本机 VKS 成品库。", "VKS_LIBRARY_UNAVAILABLE") from None
            except (ValueError, TypeError, KeyError, OSError):
                raise LibraryError("成品库路径不可用，请重新选择。", "VKS_LIBRARY_UNAVAILABLE") from None
            if self.cached is None or self.cached[0].path != path or self.cached[1] != signature:
                self.cached = read_library(path)
            library, _ = self.cached
            if expected_ref is not None and library.ref != expected_ref:
                raise LibraryError("成品库已切换，请重新获取版本并搜索。", "VKS_LIBRARY_CHANGED")
            return library
