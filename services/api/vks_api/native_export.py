"""Permission-closed original document readers for a portable, read-only export.

All source inspection here is parsing. Source JavaScript only ever runs in the
recipient's opaque sandbox, and only the known handbook reader has an executable
compatibility adapter. Other executable documents keep the exact standard view.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections import OrderedDict
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from packages.analyzers.offline_prototype import HANDBOOK, _inflate, _scripts
from packages.analyzers.vks_ingest import BundleError, safe_path

from .exports import allow_artifact, read_original
from .models import Artifact, Entity, Revision
from .native_documents import describe, index_modified, package_documents
from .service import sha


def _active_html(raw):
    soup = BeautifulSoup(raw, "html.parser")
    return bool(
        soup.find("script", src=True)
        or any(
            node.get("type", "").lower() not in {"application/json", "application/ld+json", "application/octet-stream"}
            for node in soup.find_all("script")
        )
        or any(
            str(key).lower().startswith("on") or str(key).lower() == "contenteditable"
            for node in soup.find_all(True)
            for key in node.attrs
        )
        or soup.find("input", attrs={"type": re.compile("^file$", re.IGNORECASE)})
        or soup.find(["iframe", "object", "embed"])
        or soup.find("meta", attrs={"http-equiv": re.compile("^refresh$", re.IGNORECASE)})
        or any(
            urlsplit(str(node.get(attribute, ""))).scheme.lower() in {"javascript", "vbscript"}
            for node in soup.find_all(True)
            for attribute in ("href", "src", "action")
        )
    )


def _local_path(base, reference):
    value = urlsplit(urljoin("https://vks-source.invalid/" + base, reference))
    if value.scheme != "https" or value.netloc != "vks-source.invalid":
        return None
    return safe_path(unquote(value.path.lstrip("/")))


def _static_pages(raw, fmt, default_path):
    envelope = _scripts(raw, {"vks-site": "application/json"})
    if "vks-site" in envelope:
        items = json.loads(envelope.get("vks-site", "{}"))
        files = {
            safe_path(path): (base64.b64decode(item["data"], validate=True), item["type"])
            for path, item in items.items()
        }
    else:
        files = {default_path: (raw, "text/html")}
    html_files = {path: body for path, (body, _media) in files.items() if path.lower().endswith((".html", ".htm"))}
    if not html_files or any(_active_html(body) for body in html_files.values()):
        return None

    def resource(base, reference, seen=()):
        if reference.startswith(("data:", "#")):
            return reference
        path = _local_path(base, reference)
        if path not in files or path in seen:
            raise ValueError("Missing or recursive source resource")
        body, media = files[path]
        if path.lower().endswith(".css"):
            css = body.decode("utf-8-sig")
            if re.search(r"@import\b", css, re.IGNORECASE):
                # Import order/cycles need a proper stylesheet resolver.
                raise ValueError("Unsupported stylesheet import")
            css = re.sub(
                r"url\(\s*(['\"]?)([^)'\"\s]+)\1\s*\)",
                lambda match: 'url("' + resource(path, match[2], (*seen, path)) + '")',
                css,
            )
            body = css.encode()
        return "data:" + media + ";base64," + base64.b64encode(body).decode()

    output = {}
    for path, body in html_files.items():
        soup = BeautifulSoup(body.decode("utf-8-sig"), "html.parser")
        for node in soup.find_all("base"):
            node.decompose()
        for node in soup.find_all(True):
            if node.get("srcset"):
                # Keep resource rewriting exact; do not silently lose images.
                raise ValueError("Source srcset requires standard reader")
            for attribute in ("src", "poster", "href"):
                reference = node.get(attribute)
                if not isinstance(reference, str) or not reference or reference.startswith("#"):
                    continue
                if node.name == "a" and attribute == "href":
                    destination = _local_path(path, reference)
                    if destination in html_files:
                        node["data-vks-native-path"] = destination
                        node["data-vks-native-anchor"] = unquote(urlsplit(reference).fragment)
                        node[attribute] = "#"
                    elif urlsplit(reference).scheme in {"https", "http", "mailto"}:
                        node["target"] = "_blank"
                        node["rel"] = "noopener noreferrer"
                    continue
                if node.name == "link" and attribute == "href" and "stylesheet" not in node.get("rel", []):
                    continue
                node[attribute] = resource(path, reference)
            if node.get("style"):
                node["style"] = re.sub(
                    r"url\(\s*(['\"]?)([^)'\"\s]+)\1\s*\)",
                    lambda match, path=path: 'url("' + resource(path, match[2]) + '")',
                    node["style"],
                )
        for style in soup.find_all("style"):
            css = style.get_text()
            if re.search(r"@import\b", css, re.IGNORECASE):
                raise ValueError("Unsupported stylesheet import")
            style.string = re.sub(
                r"url\(\s*(['\"]?)([^)'\"\s]+)\1\s*\)",
                lambda match, path=path: 'url("' + resource(path, match[2]) + '")',
                css,
            )
        output[path] = "<!doctype html>" + str(soup)
    return output


def _handbook_page(raw):
    packed = _scripts(raw, {HANDBOOK: "application/octet-stream", "fh-pack-meta": "application/json"})
    if HANDBOOK in packed:
        raw = _inflate(packed[HANDBOOK])
        metadata = json.loads(packed.get("fh-pack-meta", "{}"))
        if metadata.get("bytes") != len(raw) or metadata.get("sha256") != sha(raw):
            raise ValueError("Invalid source handbook envelope")
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    if not soup.find("script", id="source-data") or not soup.find("article"):
        raise ValueError("Unknown handbook framework")
    codec = soup.find("script", id="fh-pack-codec")
    if codec and "fh-pack-template" in codec.get_text():
        code = codec.get_text()
        template_init = re.search(
            r"const\s+template\s*=\s*JSON\.parse\(document\.getElementById\(['\"]fh-pack-template['\"]\)\.textContent\);",
            code,
        )
        if (
            not template_init
            or "window.fhBookPages" not in code[: template_init.start()]
            or not code.rstrip().endswith("})();")
        ):
            raise ValueError("Unknown handbook reader/export initialization")
        # This known IIFE first installs lazy reading, then the source updater's
        # portable exporter. End after the reader; its removed template must not
        # be dereferenced, and no source export capability is needed here.
        codec.string = code[: template_init.start()] + "return;" + code[template_init.end() :]
    # The source UI's network, vector editing and document updater are replaced
    # by the recipient's outer, read-only assistant. Retain reader/search/diagram
    # scripts and original article markup; no source content is rewritten.
    for node in soup.select(
        "#fh-ai-root,#fh-ai-runtime,#fh-ai-manifest,#fh-ai-corpus,#fh-ai-index,#fh-pack-template,#handbook-template-integration,#template-document-update,#vks-template-data"
    ):
        node.decompose()
    for node in soup.select('[id^="fh-update-"],input[type="file"],[contenteditable]'):
        if node.name in {"input", "button", "select", "textarea"}:
            node["disabled"] = ""
        node["hidden"] = ""
        node.attrs.pop("contenteditable", None)
    # Derived reader only: preserve original imported bytes and SVG content.
    stable = soup.new_tag("style")
    stable["data-vks-diagram-viewport"] = "stable"
    stable.string = "#diagram-dialog #diagram-viewport{overflow:scroll!important;scrollbar-gutter:stable!important}"
    (soup.head or soup).append(stable)
    return {"__handbook__": "<!doctype html>" + str(soup)}


def _render_pages(service, artifact, raw, fmt, path):
    """Retain bounded immutable transforms; callers still authorize every time."""
    cache, guard = service.__dict__.setdefault("_native_export_pages", (OrderedDict(), threading.RLock()))
    key = (artifact.id, artifact.sha256, fmt, path if fmt == "html-document" else "")
    with guard:
        if key not in cache:
            cache[key] = (
                _handbook_page(raw)
                if fmt in {"charge-handbook", "standalone-handbook"}
                else _static_pages(raw, fmt, path)
            )
        cache.move_to_end(key)
        while len(cache) > 2:
            cache.popitem(last=False)
        pages = cache[key]
        return dict(pages) if pages else pages


def project_originals(service, s, actor, pid, choices, links, rows, documents, caps, *, warn_missing=True):
    """Return whole compatible originals only after the complete current closure."""
    from .materials import ROOT, roots

    available = links._packages(s, actor, pid, choices, kind="handbook")
    installed_roots = roots(s, pid, choices)
    selected_roots = {root.artifact_id: root for root in installed_roots}
    package_membership = {}
    package_entities = {}
    for root in installed_roots:
        if root.metadata_json[ROOT].get("kind") != "documents":
            continue
        entities = package_documents(s, pid, root)
        package_entities[root.id] = entities
        for eid in entities:
            package_membership.setdefault(eid, []).append(root)
    document_rows = {entity.id: row for entity, row in rows.values() if entity.type == "document"}
    document_payloads = {document["id"]: document for document in documents}
    bundles, warnings, covered = [], [], set()
    for package in available:
        fmt = package.get("format")
        if fmt not in {"charge-handbook", "standalone-handbook", "html-document", "static-site"}:
            continue
        artifact = s.get(Artifact, package["id"])
        root = selected_roots.get(package["id"])
        if not artifact or not allow_artifact(artifact, caps, s):
            continue
        members = root.metadata_json[ROOT].get("members", []) if root else []
        if any(
            not (member := s.get(Revision, rid))
            or not allow_artifact(s.get(Artifact, member.artifact_id) if member.artifact_id else None, caps, s)
            for rid in members
        ):
            continue
        entities = package_entities[root.id] if root else list(document_rows)
        included = [row for eid in entities if (row := document_rows.get(eid)) and row.id in document_payloads]
        if not included or len(included) != len(entities):
            continue
        if any(index_modified(s, pid, row) for row in included):
            for row in included:
                document_payloads[row.id]["native_fallback"] = "source_modified"
            warnings.append(f"“{package['name']}”的项目正文已修改，原件与当前版本不同；本次保留准确项目正文。")
            continue
        native = {}
        for row in included:
            chapters = links.documents._sections(s, s.get(Entity, row.entity_id), row)
            location = describe(
                service, s, actor, pid, choices, row, chapters, available, package_membership=package_membership
            )
            if not location or location["package_id"] != package["id"] or not location["matches_index"]:
                break
            native[row.id] = location
        if len(native) != len(included):
            for row in included:
                document_payloads[row.id]["native_fallback"] = "source_location_unresolved"
            warnings.append(f"“{package['name']}”的准确原文位置无法完整验证，本次使用项目阅读器。")
            continue
        raw = read_original(service, artifact)
        try:
            path = next(iter(native.values()))["path"]
            pages = _render_pages(service, artifact, raw, fmt, path)
        except (ValueError, KeyError, TypeError, UnicodeError, BundleError):
            pages = None
        if not pages:
            for row in included:
                document_payloads[row.id]["native_fallback"] = "unsupported_source"
            warnings.append(f"“{package['name']}”包含未适配的脚本、更新能力或外部资源，本次使用只读项目阅读器。")
            continue
        bundles.append(
            {
                "id": artifact.id,
                "name": package["name"],
                "format": fmt,
                "source_sha256": artifact.sha256,
                "pages": pages,
                "document_ids": sorted(native),
            }
        )
        for rid, location in native.items():
            location = {key: location[key] for key in ("package_id", "path", "document_anchor", "sections")}
            location["page"] = "__handbook__" if fmt in {"charge-handbook", "standalone-handbook"} else location["path"]
            document_payloads[rid]["native"] = location
            covered.add(rid)
    if warn_missing and len(covered) != len(documents):
        warnings.append("部分文档没有完整、兼容且可导出的原 HTML；这些文档使用准确项目正文阅读。")
    revisions = {
        rid
        for bundle in bundles
        if (root := selected_roots.get(bundle["id"]))
        for rid in root.metadata_json[ROOT].get("members", [])
    }
    revisions.update(root.id for bundle in bundles if (root := selected_roots.get(bundle["id"])))
    artifacts = {bundle["id"] for bundle in bundles}
    return bundles, warnings, revisions, artifacts
