"""Inert DOM candidates, grounded in original attributes/lines, never execution."""

from collections import Counter
from hashlib import sha256
from pathlib import PurePosixPath
import re
from bs4 import BeautifulSoup, Tag

KEY = re.compile(r"[a-z][a-z0-9._-]{0,95}\Z")
SAFE_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
OMITTED = {"script", "style", "iframe", "object", "embed", "template", "noscript", "head"}


def visible(tag):
    for node in [tag, *tag.parents]:
        if not isinstance(node, Tag):
            continue
        if node.name in OMITTED or any(k in node.attrs for k in ("hidden", "inert")):
            return False
        if str(node.get("aria-hidden", "")).lower() == "true" or re.search(
            r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)", str(node.get("style", "")), re.I
        ):
            return False
    return True


def discover(artifacts, texts):
    pages, actions, issues = [], [], []

    def gap(path, message):
        issues.append(dict(code="VKS_UI_DISCOVERY_GAP", severity="warning", artifact_path=path, message=message))

    for path, artifact in artifacts.items():
        if PurePosixPath(path).suffix.lower() not in (".html", ".htm") or path not in texts:
            continue
        text = texts[path]
        if len(text.encode()) > 2 * 1024 * 1024:
            gap(path, "HTML 超出静态候选发现范围；保留原始材料，需显式清单或人工定位。")
            continue
        dom = BeautifulSoup(text, "html.parser")
        tags = dom.find_all(True, limit=20001)
        if len(tags) > 20000:
            gap(path, "DOM 节点超过候选发现上限；未生成不完整动作集合。")
            continue
        anchors = [t for t in tags if t.has_attr("data-vks-page") and visible(t)]
        scopes = {}
        for tag in anchors:
            key = tag.get("data-vks-page", "")
            if not isinstance(key, str) or not KEY.fullmatch(key):
                gap(path, "页面锚点 key 无效，未替换或模糊合并。")
                continue
            scopes[id(tag)] = key
            pages.append(
                dict(
                    logicalKey=key,
                    name=(tag.get("aria-label") or tag.get_text(" ", strip=True) or key)[:160],
                    artifactPath=path,
                    sha256=artifact["sha256"],
                    _line=tag.sourceline or 1,
                    _discovered=True,
                    _discovery_basis="explicit_dom_attribute",
                )
            )
        fallback = None
        if not anchors:
            fallback = "ui.page.discovered." + sha256((artifact["source_id"] + ":" + path).encode()).hexdigest()[:32]
            title = dom.title or dom.find("h1")
            pages.append(
                dict(
                    logicalKey=fallback,
                    name=(title.get_text(" ", strip=True) if title else PurePosixPath(path).stem)[:160] or path[:160],
                    artifactPath=path,
                    sha256=artifact["sha256"],
                    _line=1,
                    _discovered=True,
                    _discovery_basis="source_path",
                )
            )
        ids = Counter(t.get("id") for t in tags if isinstance(t.get("id"), str))
        for tag in tags:
            if not visible(tag) or not (
                tag.has_attr("data-vks-action") or tag.name in {"button", "a", "input", "select", "textarea"}
            ):
                continue
            page_key = next((scopes[id(p)] for p in [tag, *tag.parents] if id(p) in scopes), fallback)
            if not page_key:
                continue
            key, selector, basis = tag.get("data-vks-action"), None, "explicit_dom_attribute"
            if key is not None:
                if not isinstance(key, str) or not KEY.fullmatch(key):
                    gap(path, "动作锚点 key 无效，未替换或模糊合并。")
                    continue
            else:
                dom_id = tag.get("id", "")
                if not isinstance(dom_id, str) or not SAFE_ID.fullmatch(dom_id) or ids[dom_id] != 1:
                    gap(path, "交互元素缺少唯一稳定锚点；文本仅作说明，需人工补充动作身份。")
                    continue
                selector, basis = "#" + dom_id, "unique_dom_id"
                key = "ui.action.discovered." + sha256((page_key + ":" + dom_id).encode()).hexdigest()[:32]
            label = str(
                tag.get("aria-label") or tag.get_text(" ", strip=True) or tag.get("name") or tag.get("id") or key
            )[:160]
            actions.append(
                dict(
                    logicalKey=key,
                    pageKey=page_key,
                    label=label,
                    event="click",
                    _line=tag.sourceline or 1,
                    _discovered=True,
                    _selector=selector,
                    _discovery_basis=basis,
                    _artifact_path=path,
                )
            )
    counts = Counter(row["logicalKey"] for row in pages + actions)
    conflicts = {key for key, count in counts.items() if count > 1}
    if conflicts:
        for path in sorted(
            {p["artifactPath"] for p in pages if p["logicalKey"] in conflicts}
            | {a["_artifact_path"] for a in actions if a["logicalKey"] in conflicts}
        ):
            gap(path, "发现重复的显式逻辑 key；冲突候选被隔离，需来源前缀或显式清单。")
    pages = [p for p in pages if p["logicalKey"] not in conflicts]
    valid_pages = {p["logicalKey"] for p in pages}
    actions = [a for a in actions if a["logicalKey"] not in conflicts and a["pageKey"] in valid_pages]
    return dict(pages=pages, actions=actions, documentSections=[], codeEntities=[]), issues
