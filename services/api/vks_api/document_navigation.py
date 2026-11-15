"""Source-ordered document navigation projected only from authorized selections."""

import json
import posixpath
from collections import OrderedDict
from hashlib import sha256
from pathlib import PurePosixPath

from bs4 import BeautifulSoup, SoupStrainer
from sqlalchemy import select

from packages.analyzers.cancellation import cancellation_scope
from packages.analyzers.offline_prototype import HANDBOOK, _inflate, _scripts
from packages.analyzers.vks_ingest import BundleError, safe_path

from .exports import allow_artifact, read_original
from .knowledge import SourceSnapshot
from .models import Artifact, Entity, Revision
from .reading import artifact_policy, chunks


def _id(namespace, *parts):
    raw = json.dumps([namespace, *parts], ensure_ascii=False, separators=(",", ":")).encode()
    return "nav-" + sha256(raw).hexdigest()[:24]


def handbook_navigation(raw, *, prefix="handbook/"):
    """Read a supported handbook's declared TOC as data; execute nothing."""
    envelope = _scripts(raw, {HANDBOOK: "application/octet-stream", "fh-pack-meta": "application/json"})
    if HANDBOOK in envelope:
        raw = _inflate(envelope[HANDBOOK])
        meta = json.loads(envelope.get("fh-pack-meta", "{}"))
        if (
            meta.get("format") != "fh-gzip-html-1"
            or meta.get("bytes") != len(raw)
            or meta.get("sha256") != sha256(raw).hexdigest()
        ):
            raise ValueError("Invalid handbook envelope")
    source = _scripts(raw, {"source-data": "application/json"}).get("source-data")
    if not source:
        return {"order": [], "categories": []}
    documents = json.loads(source)
    if not isinstance(documents, dict) or len(documents) > 5000:
        raise ValueError("Invalid handbook document directory")
    paths = {}
    for key, document in documents.items():
        if isinstance(document, dict) and isinstance(document.get("path"), str):
            paths[key] = prefix + safe_path(document["path"])
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser", parse_only=SoupStrainer("nav"))
    categories, ordered, seen = [], [], set()
    for group in soup.select("nav.toc .toc-group[data-name]"):
        name = " ".join(str(group.get("data-name", "")).split())[:160]
        members = []
        for entry in group.select(".toc-doc[data-doc]"):
            path = paths.get(entry.get("data-doc"))
            if path and path not in seen:
                seen.add(path)
                ordered.append(path)
                members.append(path)
        if name and members:
            categories.append({"name": name, "paths": members, "flat": True})
    ordered.extend(path for path in paths.values() if path not in seen)
    return {"order": ordered, "categories": categories}


def _cached(service, artifact, loader):
    cache = service.__dict__.setdefault("_document_navigation_sources", OrderedDict())
    key = (artifact.id, artifact.sha256)
    if key not in cache:
        # This bounded, data-only parse runs while its owning read transaction
        # is open. Analyzer callbacks can open a fresh DB transaction and must
        # not re-enter that lock. Restore the outer job fence immediately after
        # parsing; export checks it again outside the transaction before model
        # requests and publication.
        with cancellation_scope(lambda: False):
            cache[key] = loader(read_original(service, artifact))
        while len(cache) > 8:
            cache.popitem(last=False)
    else:
        cache.move_to_end(key)
    return cache[key]


def _generic_navigation(raw, filename):
    from .material_parser import parse

    parsed = parse("documents", [(filename, raw)])
    return parsed.get("navigation") or {"order": [path for path, _ in parsed["documents"]], "categories": []}


def _source_plan(service, s, package, metadata):
    plan = metadata.get("document_navigation")
    if isinstance(plan, dict):
        if metadata.get("format") in {"charge-handbook", "standalone-handbook"}:
            return {**plan, "categories": [{**category, "flat": True} for category in plan.get("categories", [])]}
        return plan
    art = s.get(Artifact, package.artifact_id)
    if metadata.get("format") == "charge-handbook":
        return _cached(service, art, handbook_navigation)
    if metadata.get("format") == "standalone-handbook":
        return _cached(service, art, lambda raw: handbook_navigation(raw, prefix=""))
    inputs = []
    for aid in art.metadata_json.get("prototype_input_artifact_ids", []):
        source = s.get(Artifact, aid)
        if source and source.project_id == art.project_id and source.relative_path.startswith("inputs/"):
            inputs.append(source)
    inputs.sort(key=lambda source: int(source.relative_path.split("/")[1]))
    order, categories = [], []
    for source in inputs:
        name = PurePosixPath(source.relative_path).name
        current = _cached(service, source, lambda raw, name=name: _generic_navigation(raw, name))
        order.extend(current.get("order", []))
        categories.extend(current.get("categories", []))
    if not order:
        # Legacy packages that predate copied uploads retain exact member order.
        for rid in metadata.get("members", []):
            row = s.get(Revision, rid)
            if row and (entity := s.get(Entity, row.entity_id)) and entity.type == "document":
                origin = s.get(Artifact, row.artifact_id)
                order.append(row.metadata_json.get("material_path") or origin.relative_path)
    return {"order": order, "categories": categories}


def _root_visible(service, s, actor, pid, package, capabilities):
    from .materials import ROOT, readable

    if not readable(service, s, actor, pid, package):
        return False
    if capabilities is None:
        return True
    art = s.get(Artifact, package.artifact_id)
    if not allow_artifact(art, capabilities, s):
        return False
    artifacts = set()
    for rid in package.metadata_json[ROOT].get("members", []):
        row = s.get(Revision, rid)
        if not row or row.project_id != pid:
            return False
        if row.artifact_id:
            artifacts.add(row.artifact_id)
    return all(allow_artifact(s.get(Artifact, aid), capabilities, s) for aid in artifacts)


def _path(row, artifact):
    return row.metadata_json.get("material_path") or artifact.relative_path


def _tree(namespace, paths, documents, *, prefix=""):
    children, folders = [], {}
    for path in paths:
        document = documents[path]
        relative = posixpath.relpath(path, prefix) if prefix else path
        parent = children
        accumulated = prefix
        for part in PurePosixPath(relative).parts[:-1]:
            accumulated = posixpath.join(accumulated, part)
            if accumulated not in folders:
                folder = {
                    "kind": "folder",
                    "id": _id(namespace, "folder", accumulated),
                    "name": part,
                    "path": accumulated,
                    "children": [],
                }
                folders[accumulated] = folder
                parent.append(folder)
            parent = folders[accumulated]["children"]
        parent.append({"kind": "document", **document})
    return children


def _categories(namespace, documents, plan):
    ordered, seen = [], set()
    for path in [*plan.get("order", []), *documents]:
        if path in documents and path not in seen:
            seen.add(path)
            ordered.append(path)
    categories, assigned, names = [], set(), {}
    for category in plan.get("categories", []):
        members = [path for path in category.get("paths", []) if path in documents and path not in assigned]
        if not members:
            continue
        assigned.update(members)
        name = str(category.get("name") or "文档")[:160]
        names[name] = names.get(name, 0) + 1
        prefix = posixpath.commonpath([posixpath.dirname(path) for path in members])
        categories.append(
            {
                "id": _id(namespace, "category", name, names[name]),
                "name": name,
                "path": prefix,
                "children": [{"kind": "document", **documents[path]} for path in members]
                if category.get("flat")
                else _tree(namespace, members, documents, prefix=prefix),
            }
        )
    generic = OrderedDict()
    for path in ordered:
        if path in assigned:
            continue
        parts = PurePosixPath(path).parts
        key = parts[0] if len(parts) > 1 else ""
        generic.setdefault(key, []).append(path)
    for prefix, members in generic.items():
        categories.append(
            {
                "id": _id(namespace, "directory", prefix),
                "name": prefix or "文档",
                "path": prefix,
                "children": _tree(namespace, members, documents, prefix=prefix),
            }
        )
    return categories


def build_navigation(service, s, actor, pid, choices, revision_ids, *, capabilities=None, dependencies=None):
    """Return selected visible leaves, ordered by exact imported package sources."""
    from .materials import ROOT, roots

    visible = {}
    for rid in revision_ids:
        row = s.get(Revision, rid)
        entity = s.get(Entity, row.entity_id) if row else None
        if (
            not row
            or not entity
            or entity.project_id != pid
            or entity.type != "document"
            or choices.get(entity.id) != rid
        ):
            continue
        if not service._visible_revision(s, actor, pid, row):
            continue
        art = s.get(Artifact, row.artifact_id)
        if not art or art.project_id != pid or capabilities is not None and not allow_artifact(art, capabilities, s):
            continue
        visible[entity.id] = (entity, row, art)
    if not visible:
        return {"packages": []}
    packages = [package for package in roots(s, pid, choices) if package.metadata_json[ROOT]["kind"] == "documents"]
    packages.sort(key=lambda package: (s.get(Entity, package.entity_id).created_at, package.entity_id))
    membership = {}
    for package in packages:
        for batch in chunks(package.metadata_json[ROOT].get("members", [])):
            for member in s.scalars(select(Revision).where(Revision.id.in_(batch), Revision.entity_id.in_(visible))):
                membership[member.entity_id] = package.entity_id
    roots_by_id = {package.entity_id: package for package in packages}
    groups = OrderedDict()
    for entity, row, art in visible.values():
        package_id = row.metadata_json.get("material_package_id") or membership.get(entity.id)
        namespace = package_id or row.metadata_json.get("logical_source_key") or art.snapshot_id
        groups.setdefault(namespace, []).append((entity, row, art))
    ordered_groups = [package.entity_id for package in packages if package.entity_id in groups]
    ordered_groups.extend(key for key in groups if key not in ordered_groups)
    output = []
    for namespace in ordered_groups:
        rows = groups[namespace]
        rows.sort(key=lambda item: (item[0].created_at, item[0].id))
        documents = {
            _path(row, art): {"id": row.id, "entity_id": entity.id, "name": row.name, "path": _path(row, art)}
            for entity, row, art in rows
        }
        package = roots_by_id.get(namespace)
        plan, name = {"order": [], "categories": []}, "项目文档"
        if package and _root_visible(service, s, actor, pid, package, capabilities):
            name = package.name
            if dependencies is not None:
                dependencies.setdefault("revision_ids", set()).update(
                    [package.id, *package.metadata_json[ROOT].get("members", [])]
                )
                dependencies.setdefault("artifact_ids", set()).add(package.artifact_id)
            try:
                plan = _source_plan(service, s, package, package.metadata_json[ROOT])
            except (OSError, ValueError, TypeError, KeyError, BundleError):
                pass
        elif not package:
            plan["order"] = [_path(row, art) for _, row, art in rows]
            source_ids = {art.snapshot_id for _, _, art in rows}
            if len(source_ids) == 1:
                snapshot = s.get(SourceSnapshot, next(iter(source_ids)))
                if snapshot and snapshot.project_id == pid and snapshot.source_key == "charge-offline-handbook":
                    original = s.scalar(
                        select(Artifact).where(
                            Artifact.project_id == pid,
                            Artifact.snapshot_id == snapshot.id,
                            Artifact.relative_path == "offline/original.html",
                        )
                    )
                    policy = artifact_policy(s, original) if original else {}
                    allowed = bool(original) and not policy.get("offline_archive_invalid")
                    allowed = allowed and (
                        not policy.get("requires_source_read") or service._can(s, actor, pid, "source:read")
                    )
                    allowed = allowed and (
                        not policy.get("requires_trace_read") or service._can(s, actor, pid, "trace:read")
                    )
                    if allowed and (capabilities is None or allow_artifact(original, capabilities, s)):
                        try:
                            plan = _cached(service, original, handbook_navigation)
                            if dependencies is not None:
                                dependencies.setdefault("artifact_ids", set()).add(original.id)
                        except (OSError, ValueError, TypeError, KeyError, BundleError):
                            pass
        categories = _categories(namespace, documents, plan)
        if categories:
            output.append({"id": namespace, "name": name, "categories": categories})
    return {"packages": output}
