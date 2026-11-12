"""Data-only readers for installable prototype and documentation packages."""

import json
from html import escape
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup

from packages.analyzers.offline_prototype import PROTOTYPE, _inflate, _scripts, offline_bundle
from packages.analyzers.vks_ingest import BundleError, safe_path

from .service import DomainError

LIMIT = 80 * 1024 * 1024


def fail(message):
    raise DomainError("VKS_INVALID_ARCHIVE", message, 422)


def text(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("资料需要使用 UTF-8 编码")


def selector(el):
    if el.get("id"):
        # CSS.escape-compatible attribute selector avoids assuming ASCII IDs.
        return "[id=" + json.dumps(el["id"], ensure_ascii=False) + "]"
    for key in ("data-vks-id", "data-testid", "data-key"):
        if el.get(key):
            return "[" + key + "=" + json.dumps(el[key], ensure_ascii=False) + "]"
    parts = []
    while el and el.name not in ("html", "[document]"):
        peers = el.find_previous_siblings(el.name)
        parts.insert(0, f"{el.name}:nth-of-type({len(peers) + 1})")
        el = el.parent
    return " > ".join(parts)


def inventory(html, route="/", frame_path=None):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.find(["h1", "h2"])
    title = (title or (heading.get_text(" ", strip=True) if heading else "") or route)[:160]
    rows = [
        {
            "route": route,
            "frame_path": frame_path or [],
            "selector": "body",
            "label": title,
            "state": "",
            "open_steps": [],
        }
    ]
    for el in soup.select('button,a,[role="button"],[onclick],input[type="submit"]')[:1500]:
        label = " ".join(
            (el.get("aria-label") or el.get("title") or el.get_text(" ", strip=True) or el.get("value") or "").split()
        )[:160]
        if not label:
            continue
        dialog = el.find_parent(attrs={"role": "dialog"})
        rows.append(
            {
                "route": route,
                "frame_path": frame_path or [],
                "selector": selector(el),
                "label": label,
                "state": selector(dialog) if dialog else "",
                "open_steps": [],
            }
        )
    return rows


def html_markdown(raw, name):
    soup = BeautifulSoup(text(raw), "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    root = soup.body or soup
    lines = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "tr"]):
        value = el.get_text(" ", strip=True)
        if value:
            lines.append(("#" * int(el.name[1]) + " " if el.name.startswith("h") else "") + value)
    return "\n\n".join(lines) or root.get_text("\n", strip=True) or f"# {name}"


def zip_files(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            rows, seen, size = [], set(), 0
            if len(archive.infolist()) > 5000:
                fail("压缩包文件数量超过 5000")
            for item in archive.infolist():
                if item.is_dir():
                    continue
                path = safe_path(item.filename)
                if path.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    fail("压缩包有重复路径或符号链接")
                seen.add(path.casefold())
                size += item.file_size
                if size > LIMIT or item.file_size > LIMIT or item.file_size > max(1, item.compress_size) * 1000:
                    fail("压缩包展开大小或压缩比超过限制")
                if any(
                    p in {".git", "node_modules", ".env", ".env.local", "id_rsa"} for p in PurePosixPath(path).parts
                ):
                    fail("请排除环境凭据和依赖目录")
                rows.append((path, archive.read(item)))
            return rows
    except (BadZipFile, OSError, RuntimeError, BundleError):
        fail("压缩包无法读取或路径不合法")


def parse(kind, files, entry=""):
    """Return renderable bytes, normalized documents and a non-executed locator inventory."""
    if sum(len(raw) for _, raw in files) > LIMIT:
        fail("资料总大小超过 80 MiB")
    expanded = []
    for name, raw in files:
        expanded.extend(zip_files(raw) if name.lower().endswith(".zip") else [(name, raw)])
    names = [name.casefold() for name, _ in expanded]
    if len(names) != len(set(names)):
        fail("资料含重复文件路径")
    documents, targets, warnings = [], [], []
    original = b""
    fmt = "markdown"
    if kind == "prototype":
        candidates = [(name, raw) for name, raw in expanded if name.lower().endswith((".html", ".htm"))]
        if not candidates:
            fail("原型包需要 HTML 入口")
        chosen = (
            next(((n, r) for n, r in candidates if n == entry), None)
            if entry
            else next(((n, r) for n, r in candidates if PurePosixPath(n).name.lower() == "index.html"), candidates[0])
        )
        if not chosen:
            fail("指定的原型入口不存在")
        entry, original = chosen
        if len(expanded) > 1:
            # Resource paths are assembled by the credential-free runtime, never served from API origin.
            fmt = "static-site"
        else:
            fmt = "html"
        try:
            scripts = _scripts(original, {PROTOTYPE: "application/octet-stream"})
            if PROTOTYPE in scripts:
                packed = json.loads(_inflate(scripts[PROTOTYPE]))
                offline_bundle(original, "validation", "validation")
                fmt = "charge-prototype"
                for path, page in packed.get("pages", {}).items():
                    if isinstance(page, dict) and isinstance(page.get("html"), str):
                        targets.extend(inventory(page["html"], "/" + path.lstrip("/"), ["actualPage"]))
        except (BundleError, ValueError, KeyError):
            fail("离线原型结构或摘要校验失败")
        targets = targets or inventory(
            text(original), "/" + entry if fmt == "static-site" else "/", ["actualPage"] if fmt == "static-site" else []
        )
        if fmt == "static-site":
            for path, raw in candidates:
                if path != entry:
                    targets.extend(inventory(text(raw), "/" + path, ["actualPage"]))
        return {
            "format": fmt,
            "entry": entry,
            "original": original,
            "documents": [],
            "targets": targets[:10000],
            "files": expanded if fmt == "static-site" else [],
            "warnings": warnings,
        }
    for name, raw in expanded:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in {".md", ".markdown", ".txt"}:
            documents.append((name, text(raw)))
        elif suffix in {".html", ".htm"}:
            try:
                adapted = offline_bundle(raw, "validation", "validation")
                if adapted and adapted[1].get("format") == "charge-handbook":
                    with ZipFile(BytesIO(adapted[0])) as archive:
                        documents.extend(
                            (p, text(archive.read(p))) for p in archive.namelist() if p.endswith((".md", ".markdown"))
                        )
                    if original:
                        fail("一个文档包只能有一份独立 HTML 阅读入口；请拆为多个包")
                    original, fmt = raw, "charge-handbook"
                else:
                    documents.append((name, html_markdown(raw, name)))
                    if len(expanded) == 1:
                        original, fmt = raw, "html-document"
            except BundleError:
                fail("离线手册结构或摘要校验失败")
        else:
            warnings.append(f"{name} 作为附件保留，不参与正文检索")
    if not documents:
        fail("没有可索引的文档；当前支持 Markdown、文本及 HTML 手册")
    if len(documents) > 5000 or len({p for p, _ in documents}) != len(documents):
        fail("文档路径重复或数量超过限制")
    if not original:
        original = (
            "<!doctype html><meta charset=utf-8><title>文档包</title><h1>文档包</h1>"
            + "".join(f"<article><h2>{escape(p)}</h2><pre>{escape(v)}</pre></article>" for p, v in documents)
        ).encode()
    return {
        "format": fmt,
        "entry": entry,
        "original": original,
        "documents": documents,
        "targets": [],
        "files": expanded,
        "warnings": warnings,
    }
