"""Build a one-page raster prototype bundle from synthetic material."""

import sys
from argparse import ArgumentParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from packages.analyzers.vks_ingest import canonical_json, parse_bundle, source_digest

PAGE_KEY = "charge.items"
SOURCE_KEY = "synthetic-prototype"
WIDTH, HEIGHT = 1200, 740
ACTIONS = {
    "charge.add": ("新增收费项", "Create"),
    "charge.sync": ("同步月卡套餐", "Sync"),
    "charge.edit": ("编辑收费项", "Edit"),
    "charge.penalty": ("违约金设置", "Late fee"),
    "charge.export": ("导出", "Export"),
}
SECTIONS = [
    ("charge.add", "charge.guide.create", "新增校验", "合成规则：新增项需要填写唯一编码。"),
    ("charge.sync", "charge.guide.sync", "停车月卡", "合成规则：同步前先选择演示停车场。"),
    ("charge.penalty", "charge.guide.penalty", "违约金口径", "合成规则：费率只用于本样例中的新账单。"),
]


def anchor(key, x, y, target=None):
    value = {"anchorKey": key, "actionKey": key, "rect": [x, y, 0.12, 0.07]}
    if target:
        value["targetStateKey"] = target
    return value


def raster(state_key, anchors, accent):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f0f4fa")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 62), fill="#162841")
    draw.text((24, 22), "VKS SYNTHETIC PROTOTYPE / CHARGE ITEMS", fill="#ffffff", font_size=18)
    draw.rectangle((0, 62, 188, HEIGHT), fill="#dce7f5")
    for i, text in enumerate(("Charge settings", "Charge items", "Billing records")):
        draw.text((22, 105 + i * 48), text, fill="#162841", font_size=17)
    draw.rounded_rectangle((216, 94, 1166, 676), radius=12, fill="#ffffff")
    draw.text((249, 124), "Charge items / " + state_key, fill="#162841", font_size=28)
    for i in range(5):
        y = 273 + i * 65
        draw.rectangle((249, y, 1134, y + 51), fill="#eff4fa" if i % 2 == 0 else "#f8fafc")
        draw.text(
            (274, y + 16), f"DEMO-{i + 1:03d}                 Synthetic charge item", fill="#162841", font_size=15
        )
    if state_key != "list":
        draw.rounded_rectangle((680, 202, 1166, 657), radius=12, fill="#e2ecfb", outline=accent, width=3)
        draw.text((716, 228), "Saved prototype state / " + state_key, fill="#162841", font_size=18)
    for control in anchors:
        x, y, w, h = control["rect"]
        left, top = round(x * WIDTH), round(y * HEIGHT)
        draw.rounded_rectangle((left, top, round((x + w) * WIDTH), round((y + h) * HEIGHT)), radius=5, fill=accent)
        draw.text((left + 10, top + 12), ACTIONS[control["actionKey"]][1], fill="#ffffff", font_size=15)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def build_bundle(project="prototype-demo", baseline="prototype-v1", *, accent="#2853a3", restricted=False, edit=None):
    files, records = {}, []
    identity = {"schemaVersion": "1.0", "projectKey": project, "baselineKey": baseline}

    def add(path, raw, kind, media, source=SOURCE_KEY, sensitivity="internal"):
        files[path] = raw
        digest = sha256(raw).hexdigest()
        records.append(
            {
                "path": path,
                "sha256": digest,
                "size_bytes": len(raw),
                "kind": kind,
                "media_type": media,
                "source_id": source,
                "sensitivity": sensitivity,
            }
        )
        return {"path": path, "sha256": digest}

    def state(key, label, anchors, captured=True):
        result = {
            "stateKey": key,
            "name": label,
            "origin": "prototype_export",
            "viewport": {"width": WIDTH, "height": HEIGHT, "devicePixelRatio": 1},
            "anchors": anchors,
        }
        if captured:
            ref = add(
                f"views/{key}.png",
                raster(key, anchors, accent),
                "screenshot",
                "image/png",
                sensitivity="confidential" if restricted and key == "edit-drawer" else "internal",
            )
            result["surface"] = {"kind": "raster", "artifactPath": ref["path"], "sha256": ref["sha256"]}
        return result

    page = {
        "pageKey": PAGE_KEY,
        "name": "收费项管理（合成）",
        "routeAliases": ["/demo/charge-items"],
        "defaultStateKey": "list",
        "states": [
            state(
                "list",
                "列表",
                [
                    anchor("charge.add", 0.34, 0.24, "create-drawer"),
                    anchor("charge.sync", 0.48, 0.24, "sync"),
                    anchor("charge.export", 0.62, 0.24),
                    anchor("charge.edit", 0.77, 0.37, "edit-drawer"),
                ],
            ),
            state("create-drawer", "新增抽屉", []),
            state("edit-drawer", "编辑抽屉", [anchor("charge.penalty", 0.60, 0.43)]),
            state("sync", "月卡同步", [], captured=False),
        ],
        "actions": [{"actionKey": key, "label": names[0]} for key, names in ACTIONS.items()],
    }
    if edit:
        edit(page)
    prototype = add(
        "prototype.json",
        canonical_json(dict(identity, sourceKey=SOURCE_KEY, pages=[page])),
        "fixture",
        "application/json",
    )
    markdown = (
        "# 收费项导览手册（合成）\n\n" + "\n\n".join(f"## {title}\n\n{body}" for _, _, title, body in SECTIONS) + "\n"
    )
    doc = add("manual.md", markdown.encode(), "markdown", "text/markdown", source="synthetic-manual")
    locations = []
    for i, (_, key, _, _) in enumerate(SECTIONS):
        locations.append({"kind": "document_section", "sectionKey": key, "startLine": 3 + i * 4, "endLine": 5 + i * 4})
    ui = add(
        "sections.json",
        canonical_json(
            dict(
                identity,
                pages=[],
                actions=[],
                documentSections=[
                    {
                        "sectionKey": locator["sectionKey"],
                        "artifactPath": doc["path"],
                        "sha256": doc["sha256"],
                        "headingPath": ["收费项导览手册（合成）", title],
                        "startLine": locator["startLine"],
                        "endLine": locator["endLine"],
                    }
                    for locator, (_, _, title, _) in zip(locations, SECTIONS, strict=True)
                ],
            )
        ),
        "ui_manifest",
        "application/json",
        source="synthetic-manual",
    )
    bindings = add(
        "bindings.json",
        canonical_json(
            dict(
                identity,
                evidence=[
                    {
                        "evidenceKey": "evidence." + locator["sectionKey"],
                        "evidenceKind": "manual",
                        "artifactPath": doc["path"],
                        "sha256": doc["sha256"],
                        "locator": locator,
                    }
                    for locator in locations
                ],
                bindings=[
                    {
                        "fromLogicalKey": action,
                        "toLogicalKey": key,
                        "relationType": "documents",
                        "evidenceRefs": ["evidence." + key],
                    }
                    for action, key, _, _ in SECTIONS
                ],
            )
        ),
        "binding_manifest",
        "application/json",
        source="synthetic-manual",
    )
    manifest = {
        "schema_version": "1.0",
        "project_key": project,
        "baseline_key": baseline,
        "import_key": baseline,
        "sources": [
            {
                "source_id": key,
                "role": role,
                "provider": "local",
                "root": ".",
                "content_sha256": source_digest([row for row in records if row["source_id"] == key]),
            }
            for key, role in [(SOURCE_KEY, "prototype"), ("synthetic-manual", "documentation")]
        ],
        "artifacts": records,
        "prototypeManifest": prototype,
        "uiManifest": ui,
        "bindings": bindings,
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", canonical_json(manifest))
        for path, raw in files.items():
            archive.writestr(path, raw)
    return output.getvalue()


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project", default="prototype-demo")
    parser.add_argument("--baseline", default="prototype-v1")
    args = parser.parse_args()
    raw = build_bundle(args.project, args.baseline)
    parse_bundle(raw, args.project)
    args.output.write_bytes(raw)
