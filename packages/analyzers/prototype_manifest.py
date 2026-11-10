"""Validate declared raster states and project them into the shared revision model."""

from io import BytesIO
from itertools import combinations
from pathlib import PurePosixPath

from PIL import Image

from .cancellation import checkpoint
from .vks_ingest import BundleError, _entity, _fail, safe_path


def _unique_rows(rows, field):
    by_key = {row[field]: row for row in rows}
    if len(rows) != len(by_key):
        _fail("VKS_DUPLICATE_LOGICAL_KEY", "原型清单身份重复", field=field)
    return by_key


def _intersects(first, second):
    x, y, w, h = first
    a, b, c, d = second
    return min(x + w, a + c) - max(x, a) > 1e-9 and min(y + h, b + d) - max(y, b) > 1e-9


def _check_image(state, artifacts, source_key):
    surface = state["surface"]
    path = safe_path(surface["artifactPath"])
    art = artifacts.get(path)
    if (
        not art
        or art["source_id"] != source_key
        or art["kind"] != "screenshot"
        or art["media_type"] != "image/png"
        or PurePosixPath(path).suffix.lower() != ".png"
        or art["sha256"] != surface["sha256"]
    ):
        _fail("VKS_REFERENCE_INVALID", "状态原图须匹配同一原型来源中的 PNG 截图及摘要")
    viewport = state["viewport"]
    width = round(viewport["width"] * viewport["devicePixelRatio"])
    height = round(viewport["height"] * viewport["devicePixelRatio"])
    if width * height > 24_000_000:
        _fail("VKS_ARTIFACT_TOO_LARGE", "状态原图超过 2400 万像素")
    try:
        with Image.open(BytesIO(art["content"]), formats=["PNG"]) as image:
            if image.size != (width, height) or getattr(image, "is_animated", False):
                _fail("VKS_LOCATOR_INVALID", "状态原图尺寸与声明视口不符或包含动画")
            image.verify()
    except BundleError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        _fail("VKS_MEDIA_TYPE_INVALID", "状态原图不是有效的 PNG")


def entities(manifest, manifest_path, artifacts, sources):
    source = manifest["sourceKey"]
    if artifacts[manifest_path]["source_id"] != source or sources[source]["role"] != "prototype":
        _fail("VKS_REFERENCE_INVALID", "原型清单须属于声明的原型来源")
    for page in manifest["pages"]:
        states = _unique_rows(page["states"], "stateKey")
        actions = _unique_rows(page["actions"], "actionKey")
        page_key = page["pageKey"]
        if page["defaultStateKey"] not in states:
            _fail("VKS_REFERENCE_INVALID", "默认状态不属于声明页面")
        used_actions = set()
        for state in states.values():
            checkpoint()
            if "surface" in state:
                _check_image(state, artifacts, source)
            anchors = _unique_rows(state["anchors"], "anchorKey").values()
            if len({row["actionKey"] for row in anchors}) != len(anchors):
                _fail("VKS_DUPLICATE_LOGICAL_KEY", "同一状态内的动作热区重复")
            for anchor in anchors:
                action = anchor["actionKey"]
                if action not in actions or anchor.get("targetStateKey", state["stateKey"]) not in states:
                    _fail("VKS_REFERENCE_INVALID", "热区引用的动作或目标状态不属于声明页面")
                used_actions.add(action)
                x, y, w, h = anchor["rect"]
                if not w or not h or x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
                    _fail("VKS_LOCATOR_INVALID", "热区须在归一化画布范围内")
            if any(_intersects(a["rect"], b["rect"]) for a, b in combinations(anchors, 2)):
                _fail("VKS_LOCATOR_CONFLICT", "同一状态内的热区重叠")
        if used_actions != actions.keys():
            _fail("VKS_REFERENCE_INVALID", "每个声明动作至少需要一个所属状态热区")
        yield _entity(
            page_key,
            "page",
            page["name"],
            "",
            manifest_path,
            {"kind": "file", "relative_path": manifest_path},
            {
                "source_kind": "manual",
                "runtime_source_alignment": "unknown",
                "prototype_manifest": {
                    "schemaVersion": manifest["schemaVersion"],
                    "defaultStateKey": page["defaultStateKey"],
                    "routeAliases": page.get("routeAliases", []),
                    "states": list(states.values()),
                },
            },
        )
        for action in actions.values():
            yield _entity(
                action["actionKey"],
                "action",
                action["label"],
                "",
                manifest_path,
                {"kind": "file", "relative_path": manifest_path},
                {
                    "page_key": page_key,
                    "event": "click",
                    "prototype_action": True,
                    "evidence_origin": "manual",
                    "runtime_source_alignment": "unknown",
                },
            )
