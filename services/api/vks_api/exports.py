"""Release-pinned, permission-filtered exports and re-importable data bundles.

Export files are data. Re-import preserves source hashes and logical anchors,
while runtime causality is established from evidence on the new server.
"""

from __future__ import annotations

import json
import re
import zipfile
from hashlib import sha256
from html import escape
from io import BytesIO

from packages.analyzers.vks_ingest import _DOM, _document_key, parse_bundle, source_digest

from .models import Artifact, Entity, Evidence, Project, Release, Relation, Revision

SOURCE_TYPES = {"symbol", "sql", "table"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return sha256(raw).hexdigest()


def allow_artifact(art, capabilities, session=None):
    if art is None:
        return True
    from .reading import artifact_policy

    meta = artifact_policy(session, art) if session is not None else art.metadata_json or {}
    return (
        ((art.kind != "source" and not meta.get("requires_source_read")) or capabilities.get("allowed_source"))
        and (art.sensitivity != "confidential" or capabilities.get("allowed_confidential"))
        and (
            (art.kind not in ("trace", "capture") and not meta.get("requires_trace_read"))
            or capabilities.get("allowed_trace")
        )
    )


def visible_release(s, release, capabilities):
    from .reading import relation_is_visible

    rows = []
    for choice in release.manifest_json["selections"]:
        rev = s.get(Revision, choice["entity_revision_id"])
        entity = s.get(Entity, rev.entity_id)
        art = s.get(Artifact, rev.artifact_id) if rev.artifact_id else None
        if allow_artifact(art, capabilities, s) and (
            entity.type not in SOURCE_TYPES or capabilities.get("allowed_source")
        ):
            rows.append((entity, rev, art))
    ids = {r.id for _, r, _ in rows}
    relations = []
    evidence = {}
    for relation in release.manifest_json["relations"]:
        from .models import relation_payload

        relation = relation_payload(relation)
        if relation["from_revision_id"] not in ids or relation["to_revision_id"] not in ids:
            continue
        allowed = []
        for eid in relation["evidence_ids"]:
            ev = s.get(Evidence, eid)
            if ev and allow_artifact(s.get(Artifact, ev.artifact_id), capabilities, s):
                allowed.append(eid)
        if not relation_is_visible(
            relation,
            ids,
            set(allowed),
            s=s,
            source_allowed=bool(capabilities.get("allowed_source")),
            trace_allowed=bool(capabilities.get("allowed_trace")),
        ):
            continue
        for eid in allowed:
            evidence[eid] = s.get(Evidence, eid)
        relations.append(dict(relation, evidence_ids=allowed, reason=relation.get("reason", "")))
    return rows, relations, evidence


def metadata(service, release, capabilities, relations):
    return {
        "schema_version": "1.0",
        "release": service.release_dto(release),
        "relations": relations,
        "omission_policy": [
            "Permissions are applied before materials, references, and counts are produced.",
            "Sensitive originals, raw capture, binary attachments, and inaccessible references are omitted.",
            "Derived text preserves source content and does not claim the original content hash.",
            "Re-imported knowledge is directly usable; imported observations retain their original evidence origin.",
        ],
        "source_included": bool(capabilities.get("allowed_source")),
        "export_kind": "knowledge_bundle",
    }


def read_original(service, art):
    raw = (service.data_dir / "objects" / art.object_key).read_bytes()
    if digest(raw) != art.sha256:
        from .service import DomainError

        raise DomainError("VKS_INVALID_EVIDENCE", "导出来源对象摘要不一致", 409)
    return raw


def safe_original(service, art):
    # The Markdown export includes supported text formats with original bytes.
    if art.kind not in ("markdown", "source", "prototype", "openapi"):
        return None
    raw = read_original(service, art)
    try:
        text = raw.decode(art.metadata_json.get("encoding", "utf-8").replace("utf-8-bom", "utf-8-sig"))
    except (UnicodeError, LookupError):
        return None
    return raw


def markdown_zip(service, release, capabilities, rows, relations, evidence):
    files = {}
    records = []
    originals = set()
    for entity, rev, art in rows:
        if entity.type == "document":
            if art and art.id not in originals:
                raw = safe_original(service, art)
                if raw is not None:
                    path = f"originals/{art.snapshot_id}/{art.relative_path}"
                    files[path] = raw
                    records.append({"path": path, "sha256": digest(raw), "kind": "original"})
                    originals.add(art.id)
            path = f"knowledge/{entity.id}.md"
        elif entity.type in SOURCE_TYPES and capabilities.get("allowed_source"):
            path = f"source-excerpts/{entity.id}.txt"
        elif entity.type == "scenario":
            path = f"scenarios/{entity.id}.json"
        elif entity.type == "journey":
            path = f"journeys/{entity.id}.json"
        else:
            continue
        raw = rev.content.encode()
        files[path] = raw
        records.append({"path": path, "sha256": digest(raw), "kind": "derived", "original_content": True})
    doc = metadata(service, release, capabilities, relations)
    doc["files"] = records
    doc["evidence"] = [
        {"id": ev.id, "evidence_kind": ev.evidence_kind, "excerpt": ev.excerpt, "claim": ev.claim}
        for ev in evidence.values()
    ]
    files["knowledge-manifest.json"] = canonical(doc)
    return zip_files(files)


def zip_files(files):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            archive.writestr(name, raw)
    return stream.getvalue()


def export_locator(evidence):
    loc = evidence.locator
    kind = loc.get("kind")
    if kind in ("file_lines", "document_section", "dom_attribute"):
        fields = {
            "file_lines": ("kind", "startLine", "endLine", "qualifiedName"),
            "document_section": ("kind", "sectionKey", "startLine", "endLine"),
            "dom_attribute": ("kind", "pageKey", "actionKey"),
        }[kind]
        return {key: loc[key] for key in fields if key in loc}
    if isinstance(loc.get("line_start"), int) and isinstance(loc.get("line_end"), int):
        return {"kind": "file_lines", "startLine": loc["line_start"], "endLine": loc["line_end"]}
    # Legacy normalized evidence stores DOM anchors in its validated excerpt.
    # Re-import verifies these attributes against the original bytes again.
    match = re.fullmatch(
        r'data-vks-page="([a-z][a-z0-9._-]*)"; data-vks-action="([a-z][a-z0-9._-]*)"', evidence.excerpt
    )
    if match:
        return {"kind": "dom_attribute", "pageKey": match[1], "actionKey": match[2]}
    return None


def reimport_bundle(service, s, release, capabilities, rows, relations, evidence):
    from .knowledge import SourceSnapshot, document_origin
    from .reading import artifact_policy
    from .prototype_export import add_prototypes, portable_points

    # Destination identities cannot reproduce all model input permissions.
    # Omit derived wording rather than mislabel its input scope as evidence.
    transferable = []
    for relation in relations:
        stored = s.get(Relation, relation["id"])
        origin = (stored.metadata_json or {}) if stored else {}
        if (
            origin.get("generation_kind") == "model"
            or origin.get("model_provenance_artifact_ids")
            or relation.get("reason", "").startswith("AI 语义候选：")
        ):
            continue
        transferable.append(relation)
    relations = transferable
    project = s.get(Project, release.project_id)
    baseline = "export-" + release.id.replace("-", "")
    files, records, art_rows, paths = {}, [], {}, {}
    reconstructed = {
        art.id
        for entity, rev, art in rows
        if art
        and (
            rev.metadata_json.get("prototype_manifest")
            or rev.metadata_json.get("prototype_action")
            or entity.type == "business_point"
        )
    }
    candidate_artifacts = {art.id: art for _, _, art in rows if art and art.id not in reconstructed}
    selected_artifacts = set(candidate_artifacts)
    document_sources = {
        art.id: document_origin(s, rev) for entity, rev, art in rows if entity.type == "document" and art
    }
    document_paths = {}
    for entity, rev, art in rows:
        if entity.type == "document" and art:
            original = s.get(Artifact, rev.metadata_json.get("original_artifact_id") or art.id)
            if original and _document_key(original.relative_path) == entity.stable_key:
                document_paths[art.id] = original.relative_path
    for ev in evidence.values():
        if ev.artifact_id not in reconstructed:
            candidate_artifacts[ev.artifact_id] = s.get(Artifact, ev.artifact_id)
    for art in candidate_artifacts.values():
        policy = artifact_policy(s, art)
        if not allow_artifact(art, capabilities, s) or policy.get("requires_trace_read"):
            continue
        raw = safe_original(service, art)
        if raw is None:
            continue
        preferred = document_paths.get(art.id, art.relative_path)
        path = (
            "materials/" + preferred
            if art.id not in document_paths and preferred.casefold().startswith("export/")
            else preferred
        )
        if path.casefold() in {x.casefold() for x in files}:
            path = "snapshot-" + art.snapshot_id.replace("-", "") + "/" + path
        files[path] = raw
        paths[art.id] = path
        snapshot = s.get(SourceSnapshot, art.snapshot_id)
        source_key = document_sources.get(art.id) or (
            snapshot.source_key if snapshot else "source-" + art.snapshot_id.replace("-", "")
        )
        record = {
            "source_id": source_key,
            # Evidence-only revisions retain their exact bytes, but absence from
            # the frozen selection must never turn them back into live knowledge.
            "kind": art.kind if art.id in selected_artifacts else "fixture",
            "path": path,
            "sha256": digest(raw),
            "size_bytes": len(raw),
            "media_type": art.media_type,
            "sensitivity": "confidential" if policy.get("requires_source_read") else art.sensitivity,
            "encoding": art.metadata_json.get("encoding", "utf-8").replace("utf-8-bom", "utf-8-sig"),
        }
        records.append(record)
        art_rows[art.id] = art

    def add_file(path, raw, kind, source, media, sensitivity):
        if path in files:
            if files[path] != raw:
                raise ValueError("Exported prototype object conflicts")
            # Deduplicated material preserves its most restrictive classification.
            if sensitivity == "confidential":
                next(row for row in records if row["path"] == path)["sensitivity"] = sensitivity
        else:
            files[path] = raw
            records.append(
                {
                    "source_id": source,
                    "kind": kind,
                    "path": path,
                    "sha256": digest(raw),
                    "size_bytes": len(raw),
                    "media_type": media,
                    "sensitivity": sensitivity,
                }
            )
        return {"path": path, "sha256": digest(raw)}

    identity = {"schemaVersion": "1.0", "projectKey": project.key, "baselineKey": baseline}
    prototypes, raster_refs = add_prototypes(service, s, rows, capabilities, add_file, identity)
    page_refs = {}
    for entity, revision, artifact in rows:
        if (
            entity.type != "page"
            or revision.metadata_json.get("prototype_manifest")
            or not artifact
            or not allow_artifact(artifact, capabilities)
            or artifact.metadata_json.get("requires_trace_read")
        ):
            continue
        if (
            artifact.id in paths
            and not revision.metadata_json.get("hotspots")
            and entity.stable_key in _DOM(files[paths[artifact.id]].decode("utf-8-sig", errors="replace")).pages
        ):
            # Original explicit pages already carry their verified DOM anchors.
            page_refs[entity.stable_key] = {"artifactPath": paths[artifact.id], "sha256": artifact.sha256}
            continue
        # Authored hotspots are an annotation layer, not bytes in the original
        # source. Export an explicitly derived passive page with its declared
        # anchors; never pretend those anchors existed in the source snapshot.
        buttons = []
        for action, action_revision, _ in rows:
            if (
                action.type == "action"
                and action_revision.metadata_json.get("page_key") == entity.stable_key
                and action_revision.metadata_json.get("status") != "removed"
            ):
                buttons.append(
                    '<button type="button" data-vks-action="'
                    + escape(action.stable_key, quote=True)
                    + '">'
                    + escape(action_revision.name)
                    + "</button>"
                )
        markup = (
            '<!doctype html><html><body><section data-vks-page="'
            + escape(entity.stable_key, quote=True)
            + '"><h1>'
            + escape(revision.name)
            + "</h1><p>导出的人工热点声明。原始画面另存或已按权限省略；此派生页面不表示真实业务运行。</p>"
            + "".join(buttons)
            + "</section></body></html>"
        ).encode()
        path = "export/pages/" + entity.id + ".html"
        files[path] = markup
        records.append(
            {
                "source_id": "export-metadata",
                "kind": "prototype",
                "path": path,
                "sha256": digest(markup),
                "size_bytes": len(markup),
                "media_type": "text/html",
                "sensitivity": "confidential"
                if artifact.metadata_json.get("requires_source_read")
                else artifact.sensitivity,
            }
        )
        page_refs[entity.stable_key] = {"artifactPath": path, "sha256": digest(markup)}
        art_rows[artifact.id] = artifact
    ui = {
        "schemaVersion": "1.0",
        "projectKey": project.key,
        "baselineKey": baseline,
        "pages": [],
        "actions": [],
        "documentSections": [],
        "codeEntities": [],
    }
    key_by_revision = {}
    selected_entities = []
    source_by_path = {row["path"]: row["source_id"] for row in records}
    for entity, rev, art in rows:
        if rev.metadata_json.get("prototype_manifest") or rev.metadata_json.get("prototype_action"):
            ref = raster_refs.get(entity.stable_key)
            if ref:
                key_by_revision[rev.id] = entity.stable_key
                selected_entities.append(
                    {
                        "artifactPath": ref["path"],
                        "sha256": ref["sha256"],
                        "logicalKey": entity.stable_key,
                        "entityType": entity.type,
                        "sourceId": source_by_path[ref["path"]],
                    }
                )
            continue
        if entity.type == "action":
            if rev.metadata_json.get("status") == "removed":
                continue
            base = page_refs.get(rev.metadata_json.get("page_key"))
            if not base:
                continue
            path = base["artifactPath"]
        elif entity.type == "page":
            base = page_refs.get(entity.stable_key)
            if not base:
                continue
            path = base["artifactPath"]
        else:
            if not art or art.id not in paths:
                continue
            path = paths[art.id]
            base = {"artifactPath": path, "sha256": art.sha256}
        key_by_revision[rev.id] = _document_key(path) if entity.type == "document" else entity.stable_key
        if entity.type in {"page", "action", "api", "symbol", "sql", "table", "document", "section", "rule"}:
            selected_entities.append(
                dict(base, logicalKey=key_by_revision[rev.id], entityType=entity.type, sourceId=source_by_path[path])
            )
        if entity.type == "page":
            ui["pages"].append(dict(base, logicalKey=entity.stable_key, name=rev.name))
        elif entity.type == "action":
            ui["actions"].append(
                {
                    "logicalKey": entity.stable_key,
                    "pageKey": rev.metadata_json["page_key"],
                    "label": rev.name,
                    "event": rev.metadata_json.get("event", "click"),
                }
            )
        elif entity.type == "section":
            ui["documentSections"].append(
                dict(
                    base,
                    sectionKey=entity.stable_key,
                    headingPath=rev.metadata_json["heading_path"],
                    startLine=rev.locator["startLine"],
                    endLine=rev.locator["endLine"],
                )
            )
        elif (
            entity.type in ("api", "symbol", "sql", "table", "rule")
            and rev.metadata_json.get("resolution_state") == "external"
        ):
            ui["codeEntities"].append(
                dict(
                    base,
                    logicalKey=entity.stable_key,
                    entityType=entity.type,
                    qualifiedName=rev.metadata_json.get("qualified_name", rev.name),
                    startLine=rev.locator["startLine"],
                    endLine=rev.locator["endLine"],
                )
            )
    page_keys = {r["logicalKey"] for r in ui["pages"]}
    ui["actions"] = [r for r in ui["actions"] if r["pageKey"] in page_keys]

    def add_sidecar(path, kind, value, *, sensitivity="internal"):
        raw = canonical(value)
        files[path] = raw
        records.append(
            {
                "source_id": "export-metadata",
                "kind": kind,
                "path": path,
                "sha256": digest(raw),
                "size_bytes": len(raw),
                "media_type": "application/json",
                "sensitivity": sensitivity,
            }
        )
        return {"path": path, "sha256": digest(raw)}

    point_rows, point_bindings, point_policy, selected_points = portable_points(
        s,
        rows,
        relations,
        {
            rev.id: key_by_revision[rev.id]
            for entity, rev, _ in rows
            if entity.type == "section" and rev.id in key_by_revision
        },
        {
            rev.id: key_by_revision[rev.id]
            for entity, rev, _ in rows
            if entity.type == "action" and rev.metadata_json.get("prototype_action") and rev.id in key_by_revision
        },
        capabilities,
    )
    point_ref = None
    if point_rows:
        point_ref = add_sidecar(
            "export/business-points.json",
            "fixture",
            {**identity, "points": point_rows, "bindings": point_bindings},
            sensitivity=point_policy,
        )
        for rid, (entity, _, _) in selected_points.items():
            key_by_revision[rid] = entity.stable_key
            selected_entities.append(
                {
                    "artifactPath": point_ref["path"],
                    "sha256": point_ref["sha256"],
                    "logicalKey": entity.stable_key,
                    "entityType": "business_point",
                    "sourceId": "export-metadata",
                }
            )

    for entity, revision, _ in rows:
        if entity.type == "journey":
            add_sidecar(
                "export/journeys/" + entity.id + ".json",
                "fixture",
                {
                    "kind": "passive_journey_definition",
                    "schema_version": "2.0",
                    "definition": json.loads(revision.content),
                    "reimport_policy": "Preserved as data; rebind exact destination revisions before creating a candidate journey.",
                },
            )
        if entity.type == "scenario":
            add_sidecar(
                "export/scenarios/" + entity.id + ".json",
                "fixture",
                {
                    "kind": "scenario_definition",
                    "definition": {
                        k: value for k, value in json.loads(revision.content).items() if k != "approval_ref"
                    },
                    "reimport_policy": "Preserved as data; create a new scenario and revalidate observations before use.",
                },
            )
    ui_ref = add_sidecar("export/ui-manifest.json", "ui_manifest", ui)
    selection_ref = add_sidecar(
        "export/selection.json",
        "fixture",
        {
            "kind": "fixed_entity_selection",
            "schemaVersion": "1.0",
            "projectKey": project.key,
            "baselineKey": baseline,
            "entities": selected_entities,
        },
        sensitivity="confidential"
        if any(artifact_policy(s, art).get("requires_source_read") for art in art_rows.values())
        else "internal",
    )
    # The declaration documents the fixed release and exclusions, without
    # embedding hidden IDs or restricted material in an importable side channel.
    provenance = metadata(service, release, capabilities, [])
    provenance.pop("relations")
    provenance["knowledge_policy"] = (
        "Imported relations are directly usable; evidence and runtime causality remain separate."
    )
    provenance["exclusions"] = [
        {
            "kind": "model_derived_relationships",
            "policy": "Omitted because exact all-input provenance and destination permissions cannot be preserved; regenerate and rebind before proposing.",
        }
    ]
    included_snapshots = {a.snapshot_id for a in art_rows.values()}
    provenance["sources"] = [
        source
        for source in release.manifest_json.get("sources", [])
        if source.get("source_snapshot_id") in included_snapshots
    ]
    provenance["files"] = [
        {
            "path": x["path"],
            "sha256": x["sha256"],
            "original": not x["path"].startswith("export/"),
            "indexable": x["kind"] != "fixture",
        }
        for x in records
        if x["kind"] not in ("ui_manifest", "binding_manifest")
    ]
    provenance["evidence"] = [
        {
            "id": ev.id,
            "stable_key": ev.stable_key,
            "original_artifact_id": ev.artifact_id,
            "artifact_path": paths[ev.artifact_id],
            "original_relative_path": art_rows[ev.artifact_id].relative_path,
            "source_snapshot_id": art_rows[ev.artifact_id].snapshot_id,
            "artifact_sha256": art_rows[ev.artifact_id].sha256,
            "content_sha256": ev.content_sha256,
            "locator": ev.locator,
            "evidence_kind": ev.evidence_kind,
            "claim": ev.claim,
            "reimport_policy": "Archived evidence data; does not select historical knowledge or transfer runtime causality.",
        }
        for ev in evidence.values()
        if ev.artifact_id in paths
    ]
    add_sidecar(
        "export/provenance.json",
        "fixture",
        provenance,
        sensitivity="confidential"
        if any(artifact_policy(s, art).get("requires_source_read") for art in art_rows.values())
        else "internal",
    )
    source_metadata = {
        x.get("source_id", "source-" + x["source_snapshot_id"].replace("-", "")): x
        for x in release.manifest_json.get("sources", [])
        if x.get("source_snapshot_id")
    }

    def manifest():
        sources = []
        for source_id in sorted({x["source_id"] for x in records}):
            old = source_metadata.get(source_id, {})
            source = {
                "source_id": source_id,
                "role": "prototype"
                if any(ref["path"] == f"export/prototypes/{source_id}.json" for ref in prototypes)
                else old.get("role", "documentation"),
                "provider": "local",
                "root": ".",
                "content_sha256": source_digest([r for r in records if r["source_id"] == source_id]),
            }
            if old.get("service_key"):
                source["service_key"] = old["service_key"]
            sources.append(source)
        return {
            "schema_version": "1.0",
            "project_key": project.key,
            "baseline_key": baseline,
            "import_key": "release-" + release.id,
            "sources": sources,
            "artifacts": records,
            "uiManifest": ui_ref,
            "selectionManifest": selection_ref,
            **({"prototypeManifests": prototypes} if prototypes else {}),
            **({"businessPointsManifest": point_ref} if point_ref else {}),
            "analysis_origin": "manual",
            # A default-deny artifact can never become an opt-in on export.
            "allow_platform_llm": bool(art_rows)
            and all(a.metadata_json.get("allow_platform_llm", False) for a in art_rows.values()),
        }

    def pack(m):
        return zip_files(dict(files, **{"manifest.json": canonical(m)}))

    m = manifest()
    parsed = parse_bundle(pack(m), project.key)
    known = {e["stable_key"] for e in parsed.entities}
    regenerated = {(r["from_key"], r["to_key"], r["type"]) for r in parsed.relations}
    binding_evidence, binding_relations = {}, []
    for relation in relations:
        src, dst = key_by_revision.get(relation["from_revision_id"]), key_by_revision.get(relation["to_revision_id"])
        if (
            src not in known
            or dst not in known
            or relation["relation_type"] == "observed_during"
            or (src, dst, relation["relation_type"]) in regenerated
        ):
            continue
        refs = []
        for eid in relation["evidence_ids"]:
            ev = evidence[eid]
            if ev.artifact_id not in selected_artifacts:
                # Its exact locator/hash is retained in provenance. Rebinding an
                # old document_section to today's section would falsify evidence.
                continue
            loc = export_locator(ev)
            if ev.locator.get("kind") == "ui_hotspot":
                loc = {"kind": "dom_attribute", "pageKey": ev.locator["pageKey"], "actionKey": ev.locator["actionKey"]}
            dom_ref = page_refs.get(loc.get("pageKey")) if loc and loc.get("kind") == "dom_attribute" else None
            if (ev.artifact_id not in paths and not dom_ref) or ev.evidence_kind == "observed" or not loc:
                continue
            if loc.get("kind") == "document_section" and loc.get("sectionKey") not in known:
                continue
            if loc.get("kind") == "dom_attribute" and any(loc.get(k) not in known for k in ("pageKey", "actionKey")):
                continue
            key = "export.ev." + ev.id.replace("-", "")
            binding_evidence[key] = {
                "evidenceKey": key,
                "evidenceKind": "manual"
                if dom_ref and dom_ref["artifactPath"].startswith("export/")
                else ev.evidence_kind,
                "artifactPath": dom_ref["artifactPath"] if dom_ref else paths[ev.artifact_id],
                "sha256": dom_ref["sha256"] if dom_ref else art_rows[ev.artifact_id].sha256,
                "locator": loc,
                "claim": ev.claim[:2000],
            }
            refs.append(key)
        if refs:
            binding_relations.append(
                {
                    "fromLogicalKey": src,
                    "toLogicalKey": dst,
                    "relationType": relation["relation_type"],
                    "evidenceRefs": refs[:20],
                    "reason": relation.get("reason", "")[:2000],
                }
            )
    if binding_relations:
        ref = add_sidecar(
            "export/bindings.json",
            "binding_manifest",
            {
                "schemaVersion": "1.0",
                "projectKey": project.key,
                "baselineKey": baseline,
                "producer": {"kind": "manual", "name": "VKS release export", "version": "1.0"},
                "evidence": list(binding_evidence.values()),
                "bindings": binding_relations,
            },
        )
        m = manifest()
        m["bindings"] = ref
    raw = pack(m)
    parse_bundle(raw, project.key)
    return raw


def pdf_report(service, release, rows, relations):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=48,
        bottomMargin=45,
        title="VKS 知识发布报告",
        author="VKS",
        subject="Fixed release, permission-filtered knowledge snapshot",
    )
    body = ParagraphStyle(
        "body",
        fontName="STSong-Light",
        fontSize=10,
        leading=16,
        spaceAfter=6,
        wordWrap="CJK",
        alignment=TA_LEFT,
        textColor=colors.HexColor("#24324a"),
    )
    title = ParagraphStyle(
        "title", parent=body, fontSize=22, leading=30, spaceAfter=14, textColor=colors.HexColor("#123a65")
    )
    heading = ParagraphStyle("heading", parent=body, fontSize=14, leading=22, spaceBefore=15, spaceAfter=8)
    note = ParagraphStyle("note", parent=body, fontSize=8, leading=12, textColor=colors.HexColor("#66758a"))
    story = [
        Paragraph("知境 VKS / 知识发布报告", title),
        Paragraph(f"发布版本：{escape(release.release_no)}", body),
        Paragraph(f"发布清单 SHA-256：{release.manifest_sha256}", note),
        Paragraph(
            "本报告基于固定发布版本，已按导出权限筛选。原始字节与衍生知识分别保存；关联存在不代表运行因果证据。",
            body,
        ),
        Spacer(1, 12),
    ]
    for entity, rev, _ in rows:
        if entity.type not in ("document", "scenario", "journey"):
            continue
        story.append(Paragraph(escape(rev.name), heading))
        story.append(Paragraph(f"类型：{entity.type} / 修订：{rev.id}", note))
        for line in rev.content.splitlines():
            story.append(Paragraph(escape(line).replace("\t", "    ") or "<br/>", body))
    story.append(Paragraph("已授权关系", heading))
    names = {rev.id: rev.name for _, rev, _ in rows}
    for relation in relations:
        label = (
            f"{names[relation['from_revision_id']]} → {relation['relation_type']} → {names[relation['to_revision_id']]}"
        )
        story.append(
            KeepTogether(
                [
                    Paragraph(escape(label), body),
                    Paragraph(
                        escape(f"有效性：{relation['validity_status']}；因果：{relation['causal_basis']}"),
                        note,
                    ),
                ]
            )
        )
    if not rows:
        story.append(Paragraph("当前权限下没有可导出的知识正文。", body))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#66758a"))
        canvas.drawString(48, 25, "VKS / 知识报告")
        canvas.drawRightString(A4[0] - 48, 25, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()


def perform_export(service, s, job):
    from .jobs import current_actor
    from .service import DomainError

    actor = current_actor(service, job.created_by, job.project_id, session=s, job=job)
    service.require(s, actor, job.project_id, "knowledge:export")
    capabilities = dict(job.input_json)
    # Authorization is checked again when a queued export actually runs.
    capabilities["allowed_source"] = bool(capabilities.get("allowed_source")) and service._can(
        s, actor, job.project_id, "source:read"
    )
    capabilities["allowed_confidential"] = service._can(s, actor, job.project_id, "source:read")
    capabilities["allowed_trace"] = service._can(s, actor, job.project_id, "trace:read")
    release = s.get(Release, job.input_json["release_id"])
    if not release or release.project_id != job.project_id:
        raise DomainError("VKS_NOT_FOUND", "未找到发布版", 404)
    rows, relations, evidence = visible_release(s, release, capabilities)
    format = job.input_json["format"]
    if format == "markdown_bundle":
        raw, suffix = markdown_zip(service, release, capabilities, rows, relations, evidence), ".zip"
    elif format == "vksbundle":
        raw, suffix = reimport_bundle(service, s, release, capabilities, rows, relations, evidence), ".vksbundle.zip"
    elif format == "pdf":
        raw, suffix = pdf_report(service, release, rows, relations), ".pdf"
    else:
        raise DomainError("VKS_INVALID_INPUT", "不支持的导出格式")
    # Store capabilities actually used, allowing download to recheck grants.
    job.input_json = dict(
        job.input_json,
        **{k: capabilities[k] for k in ("allowed_source", "allowed_confidential", "allowed_trace")},
        _export_revision_ids=sorted(revision.id for _, revision, _ in rows),
        _export_relation_ids=sorted(row["id"] for row in relations),
        _export_evidence_ids=sorted(evidence),
    )
    return {
        "result_refs": [{"kind": "export", "id": job.id}],
        "download_url": f"/api/v1/projects/{job.project_id}/exports/{job.id}/download",
        "object_key": service._put_object(raw),
        "filename": f"vks-{release.release_no}{suffix}",
        "format": format,
    }
