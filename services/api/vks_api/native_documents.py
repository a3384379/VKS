"""Exact, permission-closed source-reader locations. Source HTML is only data here."""

import base64
import json
import re
import threading
import weakref
from collections import OrderedDict
from functools import lru_cache
from html.parser import HTMLParser
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
    from .reading import ReadingService

    documents = set(documents)
    full_access = service._can(s, actor, pid, "source:read") and service._can(s, actor, pid, "trace:read")
    if full_access and s.info.get("vks_read_only"):
        projected = _complete_mapped_selection(s, pid, choices, documents)
        if projected is not None:
            return projected
    retained = s.info.setdefault("vks_prefetched", {})
    reader = ReadingService(service)

    def preload(ids):
        missing = [rid for rid in set(ids) if rid and (Revision, rid) not in retained]
        retained.update({(Revision, row.id): row for row in reader.revision_rows(s, missing)})

    preload(choices.get(entity_id) for entity_id in documents)
    section_ids = []
    for entity_id in documents:
        row = retained.get((Revision, choices.get(entity_id)))
        ids = row.metadata_json.get("section_revision_ids", []) if row else []
        if isinstance(ids, list):
            section_ids.extend(rid for rid in ids if isinstance(rid, str))
    preload(section_ids)

    def visible(row):
        entity = s.get(Entity, row.entity_id)
        art = s.get(Artifact, row.artifact_id) if row.artifact_id else None
        return bool(
            row.project_id == pid
            and entity
            and entity.project_id == pid
            and (not row.artifact_id or (art and art.project_id == pid))
            and (full_access or service._visible_revision(s, actor, pid, row))
        )

    docs = DocumentService(service)
    for entity_id in documents:
        entity = s.get(Entity, entity_id)
        row = s.get(Revision, choices.get(entity_id)) if choices.get(entity_id) else None
        if (
            not entity
            or entity.project_id != pid
            or entity.type != "document"
            or not row
            or row.project_id != pid
            or row.entity_id != entity_id
            or not visible(row)
        ):
            return False
        try:
            chapters = docs._sections(s, entity, row)
        except DomainError:
            return False
        if not chapters or any(
            choices.get(chapter.entity_id) != chapter.id or not visible(chapter) for chapter in chapters
        ):
            return False
    return True


def _complete_mapped_selection(s, pid, choices, document_ids):
    """Validate mapped ownership without loading names, bodies or ORM instances.

    None delegates legacy documents to the authoritative frozen/AST resolver.
    The caller has already checked both source and trace capability for this
    read snapshot; restricted readers continue through revision policy checks.
    """
    from .reading import chunks

    documents = {}
    ids = [choices.get(eid) for eid in document_ids]
    if any(rid is None for rid in ids):
        return False
    for batch in chunks(ids):
        query = (
            select(
                Revision.id,
                Revision.entity_id,
                Revision.project_id,
                Revision.artifact_id,
                Revision.metadata_json,
                Entity.project_id,
                Entity.type,
                Entity.stable_key,
                Artifact.project_id,
            )
            .join(Entity, Entity.id == Revision.entity_id)
            .outerjoin(Artifact, Artifact.id == Revision.artifact_id)
            .where(Revision.id.in_(batch))
        )
        documents.update({row[0]: row for row in s.execute(query)})
    members = set()
    for eid in document_ids:
        row = documents.get(choices[eid])
        if not row:
            return False
        _rid, entity_id, rpid, artifact_id, metadata, epid, kind, _key, apid = row
        if entity_id != eid or rpid != pid or epid != pid or kind != "document" or (artifact_id and apid != pid):
            return False
        if "section_revision_ids" not in metadata:
            return None
        mapping = metadata["section_revision_ids"]
        if (
            not isinstance(mapping, list)
            or not mapping
            or any(not isinstance(rid, str) for rid in mapping)
            or len(set(mapping)) != len(mapping)
            or metadata.get("section_count") != len(mapping)
        ):
            return False
        members.update(mapping)
    chapters = {}
    for batch in chunks(members):
        query = (
            select(
                Revision.id,
                Revision.entity_id,
                Revision.project_id,
                Revision.artifact_id,
                Revision.metadata_json["document_key"],
                Entity.project_id,
                Entity.type,
                Artifact.project_id,
            )
            .join(Entity, Entity.id == Revision.entity_id)
            .outerjoin(Artifact, Artifact.id == Revision.artifact_id)
            .where(Revision.id.in_(batch))
        )
        chapters.update({row[0]: row for row in s.execute(query)})
    for eid in document_ids:
        document = documents[choices[eid]]
        identities = set()
        for rid in document[4]["section_revision_ids"]:
            row = chapters.get(rid)
            if not row:
                return False
            _rid, entity_id, rpid, artifact_id, document_key, epid, kind, apid = row
            if (
                rpid != pid
                or epid != pid
                or kind != "section"
                or artifact_id != document[3]
                or (artifact_id and apid != pid)
                or document_key != document[7]
                or choices.get(entity_id) != rid
                or entity_id in identities
            ):
                return False
            identities.add(entity_id)
    return True


def package_documents(s, pid, package):
    from .materials import ROOT
    from .reading import chunks

    members = package.metadata_json[ROOT].get("members", [])
    cached = None
    key = None
    if s.info.get("vks_read_only"):
        transaction = s.get_transaction()
        state = s.info.get("vks_package_document_members")
        if not state or state[0] is not transaction:
            state = (transaction, {})
            s.info["vks_package_document_members"] = state
        cached = state[1]
        key = (pid, package.id, tuple(members))
        if key in cached:
            return list(cached[key])
    output = []
    for batch in chunks(members):
        # Anchor on exact revision IDs. Combining project_id and this IN clause
        # makes SQLite choose a repeated full-project index scan. Ownership and
        # kind are still checked before any identity is returned to the caller.
        rows = s.execute(
            select(Revision.entity_id, Revision.project_id, Entity.project_id, Entity.type)
            .join(Entity, Revision.entity_id == Entity.id)
            .where(Revision.id.in_(batch))
        )
        output.extend(
            eid
            for eid, revision_project, entity_project, kind in rows
            if revision_project == pid and entity_project == pid and kind == "document"
        )
    if cached is not None:
        cached[key] = tuple(output)
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


@lru_cache(maxsize=8192)
def _plain(value):
    rendered = MarkdownIt("commonmark").renderInline(value)
    return " ".join(BeautifulSoup(rendered, "html.parser").get_text().split())


def _structural_selector(node):
    parts = []
    while node and node.name not in {"html", "[document]"}:
        parts.insert(0, f"{node.name}:nth-of-type({len(node.find_previous_siblings(node.name)) + 1})")
        node = node.parent
    return " > ".join(parts)


class _AnchoredHeadings(HTMLParser):
    """Read the usual unique-id outline without materializing large SVG trees.

    Structural/ambiguous headings still use the full parser below. This fast
    path stores immutable source facts only and grants no package visibility.
    """

    inert = frozenset({"script", "style", "noscript", "iframe", "object", "embed", "template"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = {}
        self.headings = []
        self.active = None
        self.skipping = []
        self.ambiguous = False

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            if tag in self.inert and tag != "embed":
                self.skipping.append(tag)
            return
        if tag in self.inert:
            if tag != "embed":
                self.skipping.append(tag)
            return
        anchor = dict(attrs).get("id", "")
        if anchor:
            self.ids[anchor] = self.ids.get(anchor, 0) + 1
        if re.fullmatch(r"h[1-6]", tag):
            if self.active:
                self.ambiguous = True
            self.active = {"tag": tag, "anchor": anchor, "text": []}
            self.headings.append(self.active)

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.skipping:
                self.skipping = self.skipping[: self.skipping.index(tag)]
            return
        if re.fullmatch(r"h[1-6]", tag):
            if not self.active or self.active["tag"] != tag:
                self.ambiguous = True
            self.active = None

    def handle_data(self, value):
        if self.active and not self.skipping:
            self.active["text"].append(value)

    def result(self):
        if (
            self.active
            or self.ambiguous
            or any(not node["anchor"] or self.ids.get(node["anchor"]) != 1 for node in self.headings)
        ):
            return None
        return [
            {
                "anchor": node["anchor"],
                "selector": "[id=" + json.dumps(node["anchor"], ensure_ascii=False) + "]",
                "heading": " ".join("".join(node["text"]).split()),
                "heading_level": int(node["tag"][1:]),
                "heading_ordinal": index,
            }
            for index, node in enumerate(self.headings)
        ]


def _headings(raw, *, article_id=""):
    if not article_id:
        outline = _AnchoredHeadings()
        outline.feed(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
        outline.close()
        result = outline.result()
        if result is not None:
            return result
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
    # Retain only immutable source facts. Permission/selection checks always run
    # before this cache. Concurrent readers share one parse and retain a local
    # reference even when another request evicts the entry.
    cache, guard, locks = service.__dict__.setdefault(
        "_native_document_source_state", (OrderedDict(), threading.RLock(), weakref.WeakValueDictionary())
    )
    key = (artifact.id, artifact.sha256, fmt, path if fmt == "html-document" else "")
    with guard:
        lock = locks.get(key)
        if lock is None:
            lock = threading.RLock()
            locks[key] = lock
    with lock:
        with guard:
            sources = cache.get(key)
        if sources is None:
            raw = (service.data_dir / "objects" / artifact.object_key).read_bytes()
            if len(raw) != artifact.size_bytes or sha(raw) != artifact.sha256:
                raise DomainError("VKS_INTEGRITY_ERROR", "原始资料完整性校验失败", 409)
            # Source parsing never opens an independent task DB checkpoint.
            with cancellation_scope(lambda: False):
                sources = _sources(raw, fmt, path)
        source = sources.get(path)
        if source and "positions" not in source:
            if "content" not in source:
                source["content"] = html_markdown(source["raw"], path)
            headings = _headings(source["rendered"] if "rendered" in source else source["raw"])
            sections = parse_sections(source["content"], PurePosixPath(path).stem)
            titles = [section for section in sections if section["headingEndLine"] > 0]
            exact = len(titles) == len(headings) and all(
                _plain(section["title"]) == heading["heading"]
                for section, heading in zip(titles, headings, strict=False)
            )
            source["sections"] = sections
            source["positions"] = (
                {section["startLine"]: heading for section, heading in zip(titles, headings, strict=True)}
                if exact
                else {}
            )
            source["by_line"] = {section["startLine"]: section for section in sections}
            by_content = {}
            for section in sections:
                by_content.setdefault(section["content"], []).append(section)
            source["by_content"] = by_content
        with guard:
            cache[key] = sources
            cache.move_to_end(key)
            while len(cache) > 4:
                cache.popitem(last=False)
        return sources


def describe(service, s, actor, pid, choices, row, chapters, packages, *, package_membership=None):
    """Return a verifiable source version/location only for an authorized package."""
    from .materials import ROOT, roots

    if package_membership is not None:
        # Immutable facts supplied only by the current export projection, after
        # its authorized package closure. Never retain grants across requests.
        candidates = package_membership.get(row.entity_id, [])
    else:
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
    positions = source["positions"]
    matches_index = row.content == source["content"]
    locations = []
    for chapter in chapters:
        # An edited/reordered chapter is linked only when its complete source
        # section remains unique. Never map a same-named edited section by guess.
        matches = source["by_content"].get(chapter.content, [])
        source_section = matches[0] if len(matches) == 1 else None
        if matches_index:
            start = chapter.locator.get("startLine") or chapter.locator.get("line_start")
            source_section = source["by_line"].get(start)
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
