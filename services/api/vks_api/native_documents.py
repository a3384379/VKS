"""Exact, permission-closed source-reader locations. Source HTML is only data here."""

import base64
import json
import re
from collections import OrderedDict
from pathlib import PurePosixPath

from bs4 import BeautifulSoup, SoupStrainer
from markdown_it import MarkdownIt
from sqlalchemy import select

from packages.analyzers.cancellation import cancellation_scope
from packages.analyzers.offline_prototype import HANDBOOK, _inflate, _scripts
from packages.analyzers.vks_ingest import BundleError, safe_path

from .documents import DocumentService, parse_sections
from .material_parser import html_markdown
from .models import Artifact, Entity, Revision
from .service import DomainError, sha


def complete_selection(service, s, actor, pid, choices, documents):
    """A whole source reader must not bypass a partial selected chapter view."""
    docs = DocumentService(service)
    for entity_id in set(documents):
        entity = s.get(Entity, entity_id)
        row = s.get(Revision, choices.get(entity_id)) if choices.get(entity_id) else None
        if (
            not entity
            or entity.project_id != pid
            or entity.type != "document"
            or not row
            or row.project_id != pid
            or row.entity_id != entity_id
            or not service._visible_revision(s, actor, pid, row)
        ):
            return False
        try:
            chapters = docs._sections(s, entity, row)
        except DomainError:
            return False
        if not chapters or any(
            choices.get(chapter.entity_id) != chapter.id or not service._visible_revision(s, actor, pid, chapter)
            for chapter in chapters
        ):
            return False
    return True


def package_documents(s, pid, package):
    from .materials import ROOT
    from .reading import chunks

    output = []
    for batch in chunks(package.metadata_json[ROOT].get("members", [])):
        output.extend(
            s.scalars(
                select(Revision.entity_id)
                .join(Entity, Revision.entity_id == Entity.id)
                .where(
                    Revision.id.in_(batch),
                    Revision.project_id == pid,
                    Entity.project_id == pid,
                    Entity.type == "document",
                )
            )
        )
    return output


def catalog_locations(s, pid, choices, rows, packages):
    """Lightweight raw-package hints; source parsing is deferred to detail reads."""
    from .materials import roots

    available = {package["id"]: package for package in packages if package["kind"] == "handbook"}
    membership = {}
    for package in roots(s, pid, choices):
        if package.artifact_id not in available:
            continue
        for entity_id in package_documents(s, pid, package):
            membership.setdefault(entity_id, []).append(available[package.artifact_id])
    output = {}
    for entity, row in rows:
        if entity.type != "document":
            continue
        candidates = membership.get(entity.id, [])
        if not candidates and not row.metadata_json.get("material_package_id"):
            origin = s.get(Artifact, row.metadata_json.get("original_artifact_id") or row.artifact_id)
            candidates = [
                package
                for package in available.values()
                if origin
                and origin.project_id == pid
                and (art := s.get(Artifact, package["id"]))
                and art.snapshot_id == origin.snapshot_id
            ]
        if len(candidates) != 1:
            continue
        package = candidates[0]
        fmt = package.get("format", "")
        path = row.metadata_json.get("material_path") or s.get(Artifact, row.artifact_id).relative_path
        if fmt == "charge-handbook" or (
            fmt in {"html-document", "static-site"} and PurePosixPath(path).suffix.lower() in {".html", ".htm"}
        ):
            output[entity.id] = package["id"]
    return output


def index_modified(s, pid, row):
    """Compare normalized source-body facts without parsing the source package."""
    metadata = row.metadata_json
    fingerprint = metadata.get("source_body_sha256")
    if fingerprint is not None:
        return (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or sha(row.content) != fingerprint
        )
    artifact = s.get(Artifact, row.artifact_id)
    if not artifact or artifact.project_id != pid:
        return True
    original_id = metadata.get("original_artifact_id") or artifact.metadata_json.get("original_artifact_id")
    if original_id:
        original = s.get(Artifact, original_id)
        return (
            not original
            or original.project_id != pid
            or original.kind != "markdown"
            or sha(row.content) != original.sha256
        )
    if metadata.get("source_kind") == "platform_draft" or artifact.metadata_json.get("source_kind") == "platform_draft":
        return True
    # Older path-move imports could preserve authored text in a fresh Markdown
    # artifact while recording its prior draft only in the input provenance.
    for artifact_id in artifact.metadata_json.get("prototype_input_artifact_ids", []):
        source = s.get(Artifact, artifact_id)
        if not source or source.project_id != pid:
            return True
        if source.metadata_json.get("source_kind") == "platform_draft" or source.metadata_json.get(
            "original_artifact_id"
        ):
            return True
    return artifact.kind != "markdown" or sha(row.content) != artifact.sha256


def _plain(value):
    rendered = MarkdownIt("commonmark").renderInline(value)
    return " ".join(BeautifulSoup(rendered, "html.parser").get_text().split())


def _structural_selector(node):
    parts = []
    while node and node.name not in {"html", "[document]"}:
        parts.insert(0, f"{node.name}:nth-of-type({len(node.find_previous_siblings(node.name)) + 1})")
        node = node.parent
    return " > ".join(parts)


def _headings(raw, *, article_id=""):
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.find(id=article_id) if article_id else soup
    if not root:
        return []
    for node in root(["script", "style", "noscript", "iframe", "object", "embed", "template"]):
        node.decompose()
    out = []
    ids = {}
    for el in root.find_all(id=True):
        ids[el["id"]] = ids.get(el["id"], 0) + 1
    for index, node in enumerate(root.find_all(re.compile(r"^h[1-6]$"))):
        anchor = node.get("id", "")
        # Duplicate IDs are not accurate locations; retain an exact structural selector.
        if anchor and ids.get(anchor) != 1:
            anchor = ""
        out.append(
            {
                "anchor": anchor,
                "selector": ("[id=" + json.dumps(anchor, ensure_ascii=False) + "]")
                if anchor
                else _structural_selector(node),
                "heading": " ".join(node.get_text().split()),
                "heading_level": int(node.name[1:]),
                "heading_ordinal": index,
            }
        )
    return out


def _sources(raw, fmt, default_path):
    """Parse immutable source bytes; never call or infer source script functions."""
    if fmt == "charge-handbook":
        envelope = _scripts(raw, {HANDBOOK: "application/octet-stream", "fh-pack-meta": "application/json"})
        if HANDBOOK in envelope:
            raw = _inflate(envelope[HANDBOOK])
            metadata = json.loads(envelope.get("fh-pack-meta", "{}"))
            if metadata.get("bytes") != len(raw) or metadata.get("sha256") != sha(raw):
                raise ValueError("Invalid handbook envelope")
        scripts = _scripts(raw, {"source-data": "application/json", "fh-book-pages": "application/json"})
        sources = json.loads(scripts.get("source-data", "{}"))
        pages = json.loads(scripts.get("fh-book-pages", "{}"))
        soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser", parse_only=SoupStrainer("article"))
        output = {}
        for article_id, source in sources.items():
            path = "handbook/" + safe_path(source["path"])
            content = base64.b64decode(source["base64"], validate=True)
            if sha(content) != source["sha256"] or path in output:
                raise ValueError("Invalid handbook source")
            page = pages.get(article_id, {})
            # Lazy handbook articles are populated from an exact HTML payload.
            rendered = page.get("html") if isinstance(page, dict) else None
            articles = soup.find_all(id=article_id)
            if len(articles) != 1:
                continue
            output[path] = {
                "content": content.decode("utf-8-sig"),
                "document_anchor": article_id,
                "rendered": rendered if isinstance(rendered, str) else str(articles[0]),
            }
        return output
    scripts = _scripts(raw, {"vks-site": "application/json"})
    if "vks-site" in scripts:
        files = json.loads(scripts["vks-site"])
        output = {}
        for path, item in files.items():
            if PurePosixPath(path).suffix.lower() not in {".html", ".htm"}:
                continue
            data = base64.b64decode(item["data"], validate=True)
            output[safe_path(path)] = {
                "raw": data,
                "document_anchor": "",
            }
        return output
    return {
        default_path: {
            "raw": raw,
            "document_anchor": "",
        }
    }


def _source_cache(service, artifact, fmt, path):
    cache = service.__dict__.setdefault("_native_document_sources", OrderedDict())
    key = (artifact.id, artifact.sha256, fmt, path if fmt == "html-document" else "")
    if key not in cache:
        raw = (service.data_dir / "objects" / artifact.object_key).read_bytes()
        if len(raw) != artifact.size_bytes or sha(raw) != artifact.sha256:
            raise DomainError("VKS_INTEGRITY_ERROR", "原始资料完整性校验失败", 409)
        # Analyzer checkpoints can open a second DB transaction. This data-only
        # parse is inside its owning read transaction; restore the outer fence.
        with cancellation_scope(lambda: False):
            cache[key] = _sources(raw, fmt, path)
        while len(cache) > 4:
            cache.popitem(last=False)
    else:
        cache.move_to_end(key)
    return cache[key]


def describe(service, s, actor, pid, choices, row, chapters, packages):
    """Return a verifiable source version/location only for an authorized package."""
    from .materials import ROOT, roots

    candidates = []
    for package in roots(s, pid, choices):
        if package.metadata_json[ROOT].get("kind") != "documents":
            continue
        if row.entity_id in package_documents(s, pid, package):
            candidates.append(package)
    selected = candidates[0] if len(candidates) == 1 else None
    available = {package["id"]: package for package in packages if package["kind"] == "handbook"}
    if selected:
        descriptor = available.get(selected.artifact_id)
        if not descriptor:
            return None
        fmt = selected.metadata_json[ROOT].get("format", "")
    elif candidates:
        return None
    else:
        origin = s.get(Artifact, row.metadata_json.get("original_artifact_id") or row.artifact_id)
        if not origin or origin.project_id != pid:
            return None
        matches = [
            package
            for package in available.values()
            if (art := s.get(Artifact, package["id"])) and art.snapshot_id == origin.snapshot_id
        ]
        if len(matches) != 1:
            return None
        descriptor, fmt = matches[0], "charge-handbook"
    if fmt not in {"charge-handbook", "html-document", "static-site"}:
        return None
    artifact = s.get(Artifact, descriptor["id"])
    origin = s.get(Artifact, row.artifact_id)
    if not artifact or artifact.project_id != pid or not origin or origin.project_id != pid:
        return None
    path = row.metadata_json.get("material_path") or origin.relative_path
    try:
        source = _source_cache(service, artifact, fmt, path).get(path)
    except (ValueError, KeyError, TypeError, UnicodeError, BundleError):
        # Unsupported/ambiguous source structures keep the standard reader.
        return None
    if not source:
        return None
    if "content" not in source:
        source["content"] = html_markdown(source["raw"], path)
    if "headings" not in source:
        source["headings"] = _headings(
            source["rendered"] if "rendered" in source else source["raw"].decode("utf-8-sig")
        )
    source_sections = parse_sections(source["content"], PurePosixPath(path).stem)
    source_headings = [section for section in source_sections if section["headingEndLine"] > 0]
    rendered = source["headings"]
    exact_headings = len(source_headings) == len(rendered) and all(
        _plain(section["title"]) == heading["heading"]
        for section, heading in zip(source_headings, rendered, strict=False)
    )
    positions = {}
    if exact_headings:
        positions = {section["startLine"]: heading for section, heading in zip(source_headings, rendered, strict=True)}
    matches_index = row.content == source["content"]
    locations = []
    for chapter in chapters:
        # An edited/reordered chapter is linked only when its complete source
        # section remains unique. Never map a same-named edited section by guess.
        matches = [section for section in source_sections if section["content"] == chapter.content]
        source_section = matches[0] if len(matches) == 1 else None
        if matches_index:
            start = chapter.locator.get("startLine") or chapter.locator.get("line_start")
            source_section = next((section for section in source_sections if section["startLine"] == start), None)
        location = positions.get(source_section["startLine"]) if source_section else None
        if location:
            locations.append({"revision_id": chapter.id, **location})
    source_revision = row.id if matches_index else None
    if not source_revision:
        matches = list(
            s.scalars(
                select(Revision).where(
                    Revision.project_id == pid,
                    Revision.entity_id == row.entity_id,
                    Revision.content == source["content"],
                )
            )
        )
        matches = [candidate.id for candidate in matches if service._visible_revision(s, actor, pid, candidate)]
        source_revision = matches[0] if len(matches) == 1 else None
    return {
        "package_id": artifact.id,
        "package_key": descriptor.get("package_key", ""),
        "revision_id": descriptor.get("revision_id", ""),
        "sha256": artifact.sha256,
        "format": fmt,
        "path": path,
        "document_anchor": source["document_anchor"],
        "sections": locations,
        "matches_index": matches_index,
        "source_document_revision_id": source_revision,
    }
