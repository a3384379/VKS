"""Data-only readers for installable prototype and documentation packages."""

import json
from collections import Counter
from html import escape
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from packages.analyzers.offline_prototype import PROTOTYPE, _inflate, _scripts, offline_bundle
from packages.analyzers.vks_ingest import BundleError, safe_path

from .service import DomainError

LIMIT = 80 * 1024 * 1024
MAX_FILES = 5000


def fail(message):
    raise DomainError("VKS_INVALID_ARCHIVE", message, 422)


def text(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("资料需要使用 UTF-8 编码")


def selector(el, id_counts=None):
    document = el
    while document.parent:
        document = document.parent
    if id_counts is None:
        id_counts = Counter(node["id"] for node in document.select("[id]"))

    def escaped(value):
        out = []
        for i, char in enumerate(value):
            code = ord(char)
            if code == 0:
                out.append("\ufffd")
            elif (
                code < 32 or code == 127 or char.isascii() and char.isdigit() and (i == 0 or i == 1 and value[0] == "-")
            ):
                out.append("\\" + format(code, "x") + " ")
            elif char == "-" and len(value) == 1:
                out.append("\\-")
            elif code >= 128 or char in "-_" or char.isascii() and char.isalnum():
                out.append(char)
            else:
                out.append("\\" + char)
        return "".join(out)

    parts = []
    while el and el.name not in ("html", "[document]"):
        if el.get("id") and id_counts[el["id"]] == 1:
            parts.insert(0, "#" + escaped(el["id"]))
            break
        peers = el.find_previous_siblings(el.name)
        parts.insert(0, f"{el.name}:nth-of-type({len(peers) + 1})")
        el = el.parent
    return " > ".join(parts)


def inventory(html, route="/", frame_path=None):
    return inventory_report(html, route, frame_path)["targets"]


def inventory_report(html, route="/", frame_path=None):
    """Describe only source-declared controls, including omitted inventory counts."""
    soup = BeautifulSoup(html, "html.parser")
    id_counts = Counter(node["id"] for node in soup.select("[id]"))
    title = soup.title.get_text().strip() if soup.title else ""
    heading = soup.find(["h1", "h2"])
    title = " ".join((title or (heading.get_text().strip() if heading else "") or route).split())[:160]
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
    operations, discovered = {}, 1

    def label_text(node):
        return "".join(
            str(t)
            for t in node.find_all(string=True)
            if not t.find_parent(["input", "select", "textarea", "script", "style"])
        )

    labels = {node.get("for"): label_text(node) for node in soup.select("label[for]")}
    for el in soup.select('button,a,[role="button"],[role="tab"],[onclick],input,select,textarea'):
        if el.name == "input" and el.get("type", "text").lower() == "hidden":
            continue
        discovered += 1
        label = " ".join(
            (
                el.get("aria-label")
                or el.get("title")
                or labels.get(el.get("id"))
                or (label_text(el.find_parent("label")) if el.find_parent("label") else "")
                or el.get("placeholder")
                or (el.get_text().strip() if el.name not in {"select", "textarea", "input"} else "")
                or (el.get("value") if el.get("type", "").lower() in {"submit", "button", "reset"} else "")
                or el.get("name")
                or (el.name.upper() if el.name in {"input", "select", "textarea"} else "")
            ).split()
        )[:160]
        if not label:
            continue
        if len(rows) >= 1501:
            continue
        dialog = el.find_parent(attrs={"role": "dialog"})
        row_selector = selector(el, id_counts)
        rows.append(
            {
                "route": route,
                "frame_path": frame_path or [],
                "selector": row_selector,
                "label": label,
                "state": selector(dialog, id_counts) if dialog else "",
                "open_steps": [],
            }
        )
        table = el.find_parent("table")
        if table and el.find_parent("tr"):
            # Group only same action in one exact table/state/page. Each row
            # keeps its own selector and saved binding; only inference is reused.
            operations[row_selector] = [
                selector(table, id_counts),
                el.name,
                el.get("type", ""),
                label,
                selector(dialog, id_counts) if dialog else "",
            ]
    dynamic = any(
        node.get("src") or node.get_text(strip=True)
        for node in soup.find_all("script")
        if node.get("type", "").lower() not in {"application/json", "application/octet-stream", "application/ld+json"}
    )
    return {
        "targets": rows,
        "discovered": discovered,
        "truncated": discovered - len(rows),
        "row_operations": operations,
        "dynamic": dynamic,
    }


def html_markdown(raw, name):
    soup = BeautifulSoup(text(raw), "html.parser")
    for node in soup(["script", "style", "noscript", "iframe", "object", "embed", "template"]):
        node.decompose()
    root = soup.body or soup

    def language(node):
        code = node.find("code") or node
        return next(
            (value.removeprefix("language-") for value in code.get("class", []) if value.startswith("language-")), ""
        )

    # Parse as data. Preserve structure without executing source HTML/scripts.
    return (
        MarkdownConverter(
            heading_style="ATX",
            bullets="-",
            code_language_callback=language,
            keep_inline_images_in=["td", "th", "h1", "h2", "h3", "h4", "h5", "h6"],
            table_infer_header=True,
            bs4_options="html.parser",
        )
        .convert_soup(root)
        .strip()
        or f"# {name}"
    )


def zip_files(raw, budget=None):
    # One budget covers the complete material, including all uploaded ZIPs.
    # Charge metadata before decompression so a later archive cannot briefly
    # allocate another full package before an aggregate limit is noticed.
    budget = budget if budget is not None else {"bytes": 0, "entries": 0}
    try:
        with ZipFile(BytesIO(raw)) as archive:
            rows, seen = [], set()
            if budget["entries"] + len(archive.infolist()) > MAX_FILES:
                fail("压缩包文件数量超过 5000")
            for item in archive.infolist():
                budget["entries"] += 1
                if item.is_dir():
                    continue
                path = safe_path(item.filename)
                if path.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    fail("压缩包有重复路径或符号链接")
                seen.add(path.casefold())
                budget["bytes"] += item.file_size
                if (
                    budget["bytes"] > LIMIT
                    or item.file_size > LIMIT
                    or item.file_size > max(1, item.compress_size) * 1000
                ):
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
    budget = {"bytes": 0, "entries": 0}
    for name, raw in files:
        try:
            name = safe_path(name)
        except BundleError:
            fail("资料文件名或路径不合法")
        if name.lower().endswith(".zip"):
            expanded.extend(zip_files(raw, budget))
        else:
            budget["bytes"] += len(raw)
            budget["entries"] += 1
            if budget["bytes"] > LIMIT or budget["entries"] > MAX_FILES:
                fail("资料展开总大小或文件数量超过限制")
            expanded.append((name, raw))
    names = [name.casefold() for name, _ in expanded]
    if len(names) != len(set(names)):
        fail("资料含重复文件路径")
    documents, targets, warnings = [], [], []
    authored, embedded_files = None, []
    original = b""
    fmt = "markdown"
    if kind == "prototype":
        inventory_stats = {"discovered": 0, "truncated": 0, "row_operations": {}, "dynamic": False, "pages": 0}

        def collect(source, route, frame_path):
            report = inventory_report(source, route, frame_path)
            retained = report["targets"][: max(0, 10000 - len(targets))]
            targets.extend(retained)
            inventory_stats["discovered"] += report["discovered"]
            inventory_stats["truncated"] += report["discovered"] - len(retained)
            inventory_stats["dynamic"] = inventory_stats["dynamic"] or report["dynamic"]
            inventory_stats["pages"] += 1
            for item in retained:
                if item["selector"] in report["row_operations"]:
                    inventory_stats["row_operations"][json.dumps([route, frame_path, item["selector"]])] = report[
                        "row_operations"
                    ][item["selector"]]

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
                        collect(page["html"], "/" + path.lstrip("/"), ["actualPage"])
        except (BundleError, ValueError, KeyError):
            fail("离线原型结构或摘要校验失败")
        if not targets:
            collect(
                text(original),
                "/" + entry if fmt == "static-site" else "/",
                ["actualPage"] if fmt == "static-site" else [],
            )
        if fmt == "static-site":
            for path, raw in candidates:
                if path != entry:
                    collect(text(raw), "/" + path, ["actualPage"])
        return {
            "format": fmt,
            "entry": entry,
            "original": original,
            "documents": [],
            "targets": targets[:10000],
            "files": expanded if fmt == "static-site" else [],
            "warnings": warnings,
            "inventory": inventory_stats,
        }
    document_files = [
        name
        for name, _ in expanded
        if PurePosixPath(name).suffix.lower() in {".md", ".markdown", ".txt", ".html", ".htm"}
    ]
    html_files = [(name, raw) for name, raw in expanded if PurePosixPath(name).suffix.lower() in {".html", ".htm"}]
    for name, raw in expanded:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in {".md", ".markdown", ".txt"}:
            documents.append((name, text(raw)))
        elif suffix in {".html", ".htm"}:
            try:
                from .template_ingest import read_authoring

                source = read_authoring(raw)
                if source is not None:
                    if len(expanded) != 1 or original:
                        fail("本地知识手册请作为单个 HTML 独立导入")
                    if not source["documents"]:
                        fail("本地知识手册目前为空，请先在模板中导入 Markdown 再保存")
                    documents.extend(source["documents"])
                    embedded_files.extend(source["files"])
                    authored = source
                    fmt = (
                        "standalone-handbook"
                        if source["template"]["template_version"] == "2.0.0"
                        else "handbook-authoring"
                    )
                    original, entry = raw, name
                    continue
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
                    if len(document_files) == 1:
                        original, fmt, entry = raw, "html-document", name
            except BundleError:
                fail("离线手册结构或摘要校验失败")
        else:
            warnings.append(f"{name} 作为附件保留，不参与正文检索")
    if not documents:
        fail("没有可索引的文档；当前支持 Markdown、文本及 HTML 手册")
    if len(documents) > 5000 or len({p for p, _ in documents}) != len(documents):
        fail("文档路径重复或数量超过限制")
    if fmt not in {"charge-handbook", "handbook-authoring", "standalone-handbook"} and html_files:
        chosen = (
            next(((name, raw) for name, raw in html_files if name == entry), None)
            if entry
            else next(
                ((name, raw) for name, raw in html_files if PurePosixPath(name).name.lower() == "index.html"),
                html_files[0],
            )
        )
        if not chosen:
            fail("指定的文档 HTML 入口不存在")
        entry, original = chosen
        # Markdown/text members keep their standard reader. Only real HTML
        # documents receive native locations inside this source site.
        fmt = "static-site" if len(html_files) > 1 else "html-document"
    if not original:
        original = (
            "<!doctype html><meta charset=utf-8><title>文档包</title><h1>文档包</h1>"
            + "".join(f"<article><h2>{escape(p)}</h2><pre>{escape(v)}</pre></article>" for p, v in documents)
        ).encode()
    navigation = {"order": [path for path, _ in documents], "categories": []}
    if fmt in {"charge-handbook", "standalone-handbook"}:
        from .document_navigation import handbook_navigation

        navigation = handbook_navigation(original, prefix="" if fmt == "standalone-handbook" else "handbook/")
    return {
        "format": fmt,
        "entry": entry,
        "original": original,
        "documents": documents,
        "targets": [],
        "files": expanded + embedded_files,
        "warnings": warnings,
        "navigation": navigation,
        **({"template": authored["template"], "document_names": authored["document_names"]} if authored else {}),
    }
