"""Add a reproducible offline vector index to a saved library HTML."""

import argparse
import base64
import gzip
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "services/api"), str(ROOT / "packages/vks-connect/src")]

from vks_api.documents import parse_sections
from vks_connect.library import MAX_DATA_BYTES, MAX_FILE_BYTES, Library, read_envelope
from vks_connect.local_embeddings import MODEL, LocalEncoder, build_local_index

MAX_SUPPLEMENT_BYTES = 2 * 1024 * 1024


def add_supplement(payload, path):
    if path.suffix.lower() != ".md":
        raise ValueError("补充资料须为 Markdown 文件。")
    with path.open("rb") as stream:
        data = stream.read(MAX_SUPPLEMENT_BYTES + 1)
    if len(data) > MAX_SUPPLEMENT_BYTES:
        raise ValueError("补充 Markdown 超过大小限制。")
    markdown = data.decode("utf-8")
    if not markdown.strip():
        raise ValueError("补充 Markdown 没有正文。")
    digest = hashlib.sha256(data).hexdigest()
    doc_id = uuid5(NAMESPACE_URL, f"vks:offline-supplement:{payload['snapshot']['id']}:{digest}")
    if any(doc["id"] == str(doc_id) for doc in payload["documents"]):
        return
    title = "补充需求 · " + path.stem
    package_id = str(uuid5(NAMESPACE_URL, f"vks:offline-supplements:{payload['snapshot']['id']}"))
    original_path = "supplement/" + path.name
    sections = []
    for row in parse_sections(markdown, title):
        section_id = str(uuid5(doc_id, str(row["startLine"])))
        sections.append({
            "id": section_id, "revision_id": section_id, "title": row["title"], "name": row["title"],
            "heading_level": len(row["heading_path"]), "start_line": row["startLine"], "end_line": row["endLine"],
            "content": row["content"],
        })
    payload["documents"].append({
        "id": str(doc_id), "title": title, "path": original_path, "namespace": package_id, "package_id": package_id,
        "images": {}, "markdown": markdown, "sections": sections, "partial": False, "source_sha256": digest,
    })
    # Keep older libraries without a curated directory on their existing fallback.
    packs = payload.get("navigation", {}).get("packages")
    if packs:
        package = next((pack for pack in packs if pack["id"] == package_id), None)
        if package is None:
            package = {"id": package_id, "name": "补充需求", "categories": [
                {"id": package_id + "-docs", "name": "需求原文", "path": "supplement", "children": []}
            ]}
            packs.append(package)
        package["categories"][0]["children"].append({
            "id": str(doc_id), "entity_id": str(doc_id), "name": title, "kind": "document", "path": original_path
        })


def show_progress(done, total):
    if done % 1024 == 32 or done == total:
        print(f"Indexed {done}/{total} passages", flush=True)


def export_library(source, output, model_dir, supplements):
    if output.suffix.lower() not in {".html", ".htm"} or output.exists():
        raise ValueError("请选择尚不存在的成品库 HTML 输出文件。")
    if not output.parent.is_dir():
        raise ValueError("成品库输出目录不存在。")
    path, raw, (start, end), payload, _ = read_envelope(source)
    digest = hashlib.sha256(raw).hexdigest()
    for supplement in supplements:
        add_supplement(payload, supplement)
    # An index from an earlier offline export is always rebuilt from the same plaintext.
    payload.pop("local_rag", None)
    library = Library(path, digest, payload)
    payload["local_rag"] = build_local_index(payload["documents"], LocalEncoder(model_dir), progress=show_progress)
    payload["local_rag"]["source_sha256"] = digest
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_DATA_BYTES:
        raise ValueError("离线成品库数据超出读取大小限制。")
    packed = base64.b64encode(gzip.compress(data, compresslevel=6, mtime=0))
    if len(raw) - end + start + len(packed) > MAX_FILE_BYTES:
        raise ValueError("离线成品库文件超出读取大小限制。")
    stream = os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
    try:
        with stream:
            stream.write(memoryview(raw)[:start])
            stream.write(packed)
            stream.write(memoryview(raw)[end:])
    except OSError:
        output.unlink(missing_ok=True)
        raise
    return {
        "output": str(output), "source_sha256": digest, "sections": len(library.sections),
        "bindings": len(library.bindings), "chunks": len(payload["local_rag"]["chunks"]), "model": MODEL,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, action="append", default=[])
    parser.add_argument("--model-dir", type=Path, default=ROOT / "output/vks-connect/local-model")
    args = parser.parse_args()
    try:
        result = export_library(args.source, args.output, args.model_dir, args.supplement)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
