"""Portable Studio material closures and exact, revision-scoped bindings."""

import base64
import binascii
from collections import defaultdict
from pathlib import PurePosixPath

from sqlalchemy import select

from .materials import COMPOSITION, ROOT
from .models import Artifact, Relation, Revision, Selection
from .reading import artifact_policy
from .service import DomainError, sha


def unsupported(message):
    raise DomainError("VKS_INVALID_INPUT", message, 422)


def file_prefix(package_key):
    return f"documents/.vks-export/{package_key}/"


def document_layout(s, rows):
    owners = {}
    for entity, revision, _ in rows:
        meta = revision.metadata_json.get(ROOT, {})
        if meta.get("kind") != "documents":
            continue
        for rid in meta["members"]:
            member = s.get(Revision, rid)
            if member:
                owners[member.entity_id] = entity.stable_key
    return {
        entity.id: (
            owners[entity.id],
            revision.metadata_json.get("material_path")
            or art.relative_path.removeprefix(file_prefix(owners[entity.id])).removeprefix("documents/"),
        )
        for entity, revision, art in rows
        if entity.type == "document" and entity.id in owners and art
    }


def export_studio(service, s, rows, relations, capabilities, add_file, layout, key_by_revision):
    from .exports import allow_artifact, read_original

    roots = {art.id: entity.stable_key for entity, revision, art in rows if ROOT in revision.metadata_json and art}
    root_keys = {entity.id: entity.stable_key for entity, revision, _ in rows if ROOT in revision.metadata_json}
    by_id = {rev.id: (entity, rev, art) for entity, rev, art in rows}
    docs = defaultdict(list)
    for entity, rev, _ in rows:
        if entity.id in layout and rev.id in key_by_revision:
            owner, path = layout[entity.id]
            docs[owner].append({"key": entity.stable_key, "path": path})

    def material_file(artifact, path, key, *, base64_archive=False):
        if not artifact or not allow_artifact(artifact, capabilities, s):
            unsupported("当前导出权限无法包含 Studio 原包或附件的完整闭包")
        policy = artifact_policy(s, artifact)
        if policy["requires_trace_read"]:
            unsupported("需要运行证据权限的 Studio 资料暂不支持可回导格式")
        snapshots.add(artifact.snapshot_id)
        raw = read_original(service, artifact)
        return add_file(
            path,
            base64.b64encode(raw) if base64_archive else raw,
            "fixture",
            key,
            "application/octet-stream" if base64_archive else artifact.media_type,
            "confidential" if policy["requires_source_read"] else artifact.sensitivity,
        )

    packages, bindings, snapshots = [], [], set()
    primary = None
    for entity, rev, art in rows:
        if composition := rev.metadata_json.get(COMPOSITION):
            primary = root_keys.get(composition.get("primary_package_id"))
        if not (meta := rev.metadata_json.get(ROOT)):
            continue
        for rid in meta.get("members", []):
            member = s.get(Revision, rid)
            if not member or not allow_artifact(s.get(Artifact, member.artifact_id), capabilities, s):
                unsupported("当前导出权限无法包含 Studio 资料包的完整闭包")
        key = entity.stable_key
        directory = "export/studio/" + key
        inputs = []
        for aid in art.metadata_json.get("prototype_input_artifact_ids", []):
            if aid in meta.get("attachment_artifact_ids", []):
                continue
            origin = s.get(Artifact, aid)
            if not origin or origin.project_id != rev.project_id:
                unsupported("Studio 资料包缺少原始输入")
            name = PurePosixPath(origin.relative_path).name
            archive = name.lower().endswith(".zip")
            path = f"{directory}/inputs/{len(inputs)}/{name}" + (".b64" if archive else "")
            inputs.append(
                {
                    **material_file(origin, path, key, base64_archive=archive),
                    "name": name,
                    "encoding": "base64" if archive else "raw",
                    "originalSha256": origin.sha256,
                }
            )
        attachments = []
        for aid in meta.get("attachment_artifact_ids", []):
            attachment = s.get(Artifact, aid)
            if not attachment or attachment.project_id != rev.project_id:
                unsupported("Studio 文档包缺少附件")
            path = attachment.relative_path.removeprefix(file_prefix(key)).removeprefix("documents/")
            attachments.append(material_file(attachment, file_prefix(key) + path, key))
        packages.append(
            {
                "key": key,
                "name": rev.name,
                "kind": meta["kind"],
                "format": meta["format"],
                "entry": meta.get("import_entry", meta.get("entry", "")),
                "runtime": material_file(art, directory + "/runtime.html", key),
                "inputs": inputs,
                "attachments": attachments,
                "documents": docs[key],
            }
        )

    edges = defaultdict(list)
    for row in relations:
        edges[row["from_revision_id"]].append(row)
    for action, revision, _ in rows:
        anchor = revision.metadata_json.get("interactive_anchor")
        if not anchor:
            continue
        linked = [row for row in edges[revision.id] if row["relation_type"] == "has_business_point"]
        if not linked:
            continue
        if len(linked) != 1 or anchor["package_id"] not in roots:
            unsupported("Studio 关联缺少可回导的原型包或唯一业务点")
        (relation,) = linked
        frozen_edge = s.get(Relation, relation["id"])
        entity, point, artifact = by_id[relation["to_revision_id"]]
        policy = artifact_policy(s, artifact)
        if policy["requires_trace_read"]:
            unsupported("依赖运行证据的 Studio 关联暂不支持可回导格式")
        inputs = [ref["revision_id"] for ref in artifact.metadata_json.get("input_revision_provenance", [])]
        citations = {
            row["to_revision_id"]
            for row in edges[point.id]
            if row["relation_type"] == "supported_by_section" and not row["excluded"]
        }
        order = point.metadata_json.get("citation_revision_ids", [])
        citations = [rid for rid in order if rid in citations] + sorted(citations - set(order))
        if not citations or any(rid not in key_by_revision for rid in [*citations, *inputs]):
            unsupported("Studio 关联的准确章节或历史生成依据不在当前导出快照中")
        bindings.append(
            {
                "action": action.stable_key,
                "point": entity.stable_key,
                "package": roots[anchor["package_id"]],
                "target": anchor["target"],
                "title": point.name,
                "summary": point.content,
                **{field: point.metadata_json.get(field, "") for field in ("applicability", "outcome", "exceptions")},
                "sections": [key_by_revision[rid] for rid in citations],
                "inputs": [key_by_revision[rid] for rid in inputs],
                "excluded": relation["excluded"],
                "validity": relation["validity_status"],
                "manualLock": frozen_edge.manual_lock if frozen_edge else not bool(point.metadata_json.get("model_id")),
                "sourceRestricted": bool(policy["requires_source_read"]),
                "model": point.metadata_json.get("model_id", "")
                if point.metadata_json.get("origin") == "model"
                else "",
            }
        )
    return {"primaryPackage": primary, "packages": packages, "bindings": bindings}, snapshots


def original_input(reference, raw):
    if reference["encoding"] == "base64":
        try:
            raw = base64.b64decode(raw, validate=True)
        except binascii.Error:
            raise DomainError("VKS_INVALID_INPUT", "Studio 原始输入编码不合法", 422) from None
    if sha(raw) != reference["originalSha256"]:
        raise DomainError("VKS_INVALID_INPUT", "Studio 原始输入摘要不一致", 422)
    return raw


def describe_studio(bundle):
    """Recompute runtime metadata from the original uploads during preflight."""
    from .material_parser import parse
    from .material_site import site_html
    from .service import canonical

    files = {art["path"]: art["content"] for art in bundle.artifacts}
    for package in bundle.studio_manifest["packages"]:
        inputs = [(ref["name"], original_input(ref, files[ref["path"]])) for ref in package["inputs"]]
        if not inputs:
            # Adopted packages may use their original single HTML directly.
            if package["format"] != "markdown":
                inputs = [("index.html", files[package["runtime"]["path"]])]
            else:
                bundle.studio_descriptors[package["key"]] = {"targets": [], "warnings": [], "legacy": True}
                continue
        parsed = parse(package["kind"], inputs, package["entry"])
        runtime = parsed["original"]
        if parsed["format"] == "static-site" or parsed["format"] == "html-document" and len(parsed["files"]) > 1:
            runtime = site_html(parsed["entry"], parsed["files"])
        if parsed["format"] != package["format"] or sha(runtime) != package["runtime"]["sha256"]:
            raise DomainError("VKS_INVALID_INPUT", "Studio 原包与运行副本摘要不一致", 422)
        bundle.studio_descriptors[package["key"]] = {
            "entry": parsed["entry"],
            "import_entry": package["entry"],
            "targets": parsed["targets"],
            "inventory": parsed.get("inventory", {}),
            "warnings": parsed["warnings"],
            "document_navigation": parsed.get("navigation", {"order": [], "categories": []}),
            "source_body_sha256_by_path": {path: sha(markdown) for path, markdown in parsed["documents"]},
            "input_fingerprint": sha(
                canonical(
                    {
                        "files": [(name, sha(raw)) for name, raw in inputs],
                        "entry": parsed["entry"],
                        "format": parsed["format"],
                    }
                )
            ),
            "upload_fingerprint": sha(
                canonical(
                    {
                        "kind": package["kind"],
                        "entry": package["entry"],
                        "files": [(name, sha(raw)) for name, raw in inputs],
                    }
                )
            ),
            "single_input_sha256": sha(inputs[0][1])
            if len(inputs) == 1 and inputs[0][0].lower().endswith((".html", ".htm", ".md", ".markdown", ".txt"))
            else None,
            **({"document_template": parsed["template"]} if parsed.get("template") else {}),
        }


def prepare_packages(service, s, actor, analysis, studio, artifacts):
    from .knowledge import _require_import_revision
    from .materials import MaterialService, MaterialTrash
    from .prototype_edit import _identity, _new_entity

    pid = analysis.project_id
    materials = MaterialService(service)
    packages, docs = {}, {}
    for pack in studio["packages"]:
        entity = _identity(s, pid, "page", pack["key"])
        if entity:
            previous = s.scalar(select(Revision).where(Revision.entity_id == entity.id).limit(1))
            if previous and previous.metadata_json.get(ROOT, {}).get("kind") != pack["kind"]:
                raise DomainError("VKS_STATE_CONFLICT", "Studio 资料包逻辑身份已由其他来源使用", 409)
            if any(s.get(MaterialTrash, (pid, kind, entity.id)) for kind in ("package", "purged_package")):
                raise DomainError("VKS_STATE_CONFLICT", "Studio 资料包已移除，请先在资料管理中恢复", 409)
            _require_import_revision(service, s, actor, pid, previous)
        else:
            entity = _new_entity(s, pid, "page", pack["key"])
        packages[pack["key"]] = entity
        runtime = artifacts[pack["runtime"]["path"]]
        refs = []
        for index, ref in enumerate(pack["inputs"]):
            stored = artifacts[ref["path"]]
            if ref["encoding"] == "base64":
                raw = (service.data_dir / "objects" / stored.object_key).read_bytes()
                raw = original_input(ref, raw)
                original = materials._artifact(
                    s,
                    pid,
                    stored.snapshot_id,
                    f"inputs/{index}/{ref['name']}",
                    raw,
                    metadata={"prototype_input_artifact_ids": [stored.id]},
                    media_type="application/zip",
                )
                refs.append(original.id)
            else:
                refs.append(stored.id)
        refs.extend(artifacts[ref["path"]].id for ref in pack["attachments"])
        runtime.metadata_json = {**runtime.metadata_json, "prototype_input_artifact_ids": refs}
        for doc in pack["documents"]:
            docs[doc["key"]] = (entity.id, pack["key"], doc["path"])
    return packages, docs


def install_studio(service, s, actor, analysis, bundle, artifacts, entities, packages):
    from .documents import DocumentService
    from .knowledge import _require_import_relation, _require_import_revision
    from .materials import MaterialService
    from .prototype_edit import (
        PointText,
        PrototypeEditor,
        _edges,
        _evidence,
        _identity,
        _new_entity,
        _repoint,
        _select,
        _write_relation,
    )

    studio = bundle.studio_manifest
    pid = analysis.project_id
    doc_service = DocumentService(service)
    members = defaultdict(list)
    for section in bundle.entities:
        if section["type"] == "section" and section["stable_key"] in entities:
            members[section["metadata"]["document_key"]].append(entities[section["stable_key"]].id)
    runtimes = {}
    for pack in studio["packages"]:
        key = pack["key"]
        entity = packages[key]
        runtime = artifacts[pack["runtime"]["path"]]
        runtimes[key] = runtime
        docs = [rec["key"] for rec in pack["documents"]]
        sections = [rid for key_ in docs for rid in members[key_]]
        descriptor = dict(bundle.studio_descriptors[key])
        descriptor.pop("source_body_sha256_by_path", None)
        root = doc_service._revision(
            s,
            entity,
            runtime,
            pack["name"],
            "",
            {"kind": "file", "relative_path": runtime.relative_path},
            {
                ROOT: {
                    **descriptor,
                    "kind": pack["kind"],
                    "format": pack["format"],
                    "members": [entities[key_].id for key_ in docs] + sections,
                    "document_count": len(docs),
                    "section_count": len(sections),
                    "attachment_artifact_ids": [artifacts[ref["path"]].id for ref in pack["attachments"]],
                }
            },
        )
        _select(s, analysis, root)
    s.flush()
    primary = packages.get(studio["primaryPackage"])
    if packages:
        MaterialService(service)._composition(s, analysis, primary.id if primary else None)

    editor = PrototypeEditor(service)

    def identity(kind, key):
        entity = _identity(s, pid, kind, key)
        selected = s.get(Selection, (analysis.id, entity.id)) if entity else None
        previous = (
            s.get(Revision, selected.entity_revision_id)
            if selected
            else (s.scalar(select(Revision).where(Revision.entity_id == entity.id).limit(1)) if entity else None)
        )
        _require_import_revision(service, s, actor, pid, previous)
        return entity or _new_entity(s, pid, kind, key), previous if selected else None, previous

    for binding in studio["bindings"]:
        runtime = runtimes[binding["package"]]
        action_entity, old_action, historical = identity("action", binding["action"])
        if historical:
            meta = historical.metadata_json.get("interactive_anchor")
            original = s.get(Artifact, historical.artifact_id)
            from .knowledge import SourceSnapshot

            source = s.get(SourceSnapshot, original.snapshot_id) if original else None
            package_key = binding["package"]
            if (
                not meta
                or not original
                or not source
                or (
                    source.source_key not in (package_key, "material." + packages[package_key].id)
                    and package_key != "material." + original.id
                )
            ):
                raise DomainError("VKS_STATE_CONFLICT", "Studio 控件逻辑身份已由其他原型使用", 409)
        action = doc_service._revision(
            s,
            action_entity,
            runtime,
            binding["target"]["label"],
            "",
            {"kind": "file", "relative_path": runtime.relative_path},
            {
                "interactive_anchor": {
                    "package_id": runtime.id,
                    "sha256": runtime.sha256,
                    "adapter_version": "charge-dom-1",
                    "target": binding["target"],
                }
            },
        )
        _select(s, analysis, action)
        existing = None
        if old_action:
            edges = _edges(s, analysis, old_action.id)
            for edge in edges:
                _require_import_relation(service, s, actor, pid, edge)
            _repoint(edges, old_action, action, stale=False)
            existing = next((edge for edge in edges if edge.relation_type == "has_business_point"), None)

        point_entity, old_point, historical_point = identity("business_point", binding["point"])
        if existing and s.get(Revision, existing.to_revision_id).entity_id != point_entity.id:
            raise DomainError("VKS_STATE_CONFLICT", "Studio 控件已关联不同的业务点", 409)
        if historical_point and not s.scalar(
            select(Relation.id)
            .join(Revision, Revision.id == Relation.from_revision_id)
            .where(
                Relation.project_id == pid,
                Relation.relation_type == "has_business_point",
                Relation.to_revision_id == historical_point.id,
                Revision.entity_id == action_entity.id,
            )
            .limit(1)
        ):
            raise DomainError("VKS_STATE_CONFLICT", "Studio 业务点逻辑身份已由其他控件使用", 409)
        refs = [entities[key] for key in binding["sections"]]
        from .interactive import InteractiveBinding

        form = InteractiveBinding(
            analysis_set_id=analysis.id,
            package_id=runtime.id,
            target=binding["target"],
            point=PointText(**{k: binding[k] for k in ("title", "summary", "applicability", "outcome", "exceptions")}),
            section_revision_ids=[row.id for row in refs],
        )
        inputs = [entities[key] for key in binding["inputs"]]
        generation = (
            {
                "origin": "model",
                "model_id": binding["model"],
                "confidence": 0,
                "input_revision_ids": [row.id for row in inputs],
            }
            if binding["model"]
            else None
        )
        point = editor._point_revision(
            s,
            pid,
            action,
            point_entity,
            old_point,
            form,
            refs,
            input_revisions=inputs,
            generation=generation,
            source_restricted=binding["sourceRestricted"],
        )
        _select(s, analysis, point)
        old_edges = _edges(s, analysis, old_point.id) if old_point else []
        for edge in old_edges:
            _require_import_relation(service, s, actor, pid, edge)
        if old_point:
            _repoint(old_edges, old_point, point, stale=False)
        editor._citations(s, analysis, point, old_edges, refs)
        excluded = binding["excluded"] or bool(existing and existing.excluded)
        locked = binding["manualLock"] or bool(existing and existing.manual_lock)
        artifact = s.get(Artifact, point.artifact_id)
        eid = _evidence(s, analysis, point, artifact)
        edge = _write_relation(s, analysis, action, point, "has_business_point", eid, existing)
        edge.excluded = excluded
        edge.validity_status = binding["validity"]
        edge.manual_lock = locked
        if generation:
            from .models import Evidence

            s.get(Evidence, eid).evidence_kind = "inferred"
