"""Create an isolated VKS 3.0 pilot from offline source files and local viewport captures."""

import argparse
import json
import re
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from PIL import Image

PAGE_ROUTES = {
    "items": "/chargeItemMgnt",
    "standards": "/chargeStandard",
    "rooms": "/roomChargeSetting",
    "billing": "/billChargeout",
}
SOURCES = {
    "add": ("收费配置到批量出账流程", "4. 主流程", 35),
    "sync": ("manage 管理中心业务说明", "17.3 停车月卡配置", 1301),
    "penalty": ("账单违约金计算豁免与调整操作手册", "1. 先问客户想改什么，再找对应入口", 5),
}


def prepare(root, prototype, handbook, capture_dir, capture_manifest):
    from vks_api.journeys import JourneyService
    from vks_api.prototype import preview_fingerprint, prototype_image
    from vks_api.prototype_edit import AnchorEdit, BindingEdit, PrototypeEditor, SettingsEdit
    from vks_api.prototype_read import PrototypeReader
    from vks_api.service import Service
    from vks_api.knowledge import ImportService

    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    service = Service(root, database_url=f"sqlite:///{root / 'vks.db'}")
    try:
        _, owner = service.login(service.bootstrap_token)
        pid = service.create_project(owner, {"key": "charge-pilot", "name": "云收费离线原型试点"}, "create-project")[
            "id"
        ]
        aid = service.create_analysis(owner, pid, {"name": "云收费 3.0 试点"}, "create-draft")["id"]
        editor = PrototypeEditor(service)

        def upload(path, media):
            raw = path.read_bytes()
            pending = service.create_upload(
                owner,
                pid,
                {
                    "filename": path.name,
                    "size_bytes": len(raw),
                    "sha256": sha256(raw).hexdigest(),
                    "media_type": media,
                },
            )
            service.put_upload(owner, pid, pending["upload_session_id"], raw)
            return pending["artifact_id"]

        def current():
            return service.workspace(owner, pid, aid)

        reports = {}
        for path in (prototype, handbook):
            payload = {"kind": "prototype", "analysis_set_id": aid, "artifact_ids": [upload(path, "text/html")]}
            parsed = ImportService(service).prepare(actor=owner, pid=pid, payload=payload)
            selection = current()["selected_analysis_set"]["selection_version"]
            with service.db() as db:
                job = service._new_job(
                    db,
                    owner,
                    pid,
                    "import",
                    {
                        **payload,
                        "_prototype_preview": {
                            "selection_version": selection,
                            "digest": preview_fingerprint(parsed, payload, selection),
                        },
                    },
                )
                job_id = job.id
            service.process_job(job_id)
            result = service.get_job(owner, job_id)
            if result["state"] != "succeeded":
                raise RuntimeError(f"导入失败: {result.get('error')}")
            report = parsed.analysis_metrics["prototype_import"]
            reports[report["format"]] = report

        pages = {
            name: next(
                row for row in current()["pages"] if route in row["metadata"]["prototype_manifest"]["routeAliases"]
            )
            for name, route in PAGE_ROUTES.items()
        }
        captured = {}
        for item in capture_manifest:
            state_key, name = item["state_key"], item["page"]
            page = pages[name]
            path = capture_dir / Path(item["file"]).name
            with Image.open(path, formats=["PNG"]) as raw:
                width, height = raw.size
            viewport = {"width": width, "height": height, "devicePixelRatio": 1}
            anchors = []
            for anchor in item.get("anchors", []):
                x, y, w, h = anchor["rect_pixels"]
                anchors.append(
                    {
                        "anchorKey": "pilot." + anchor["action"],
                        "actionKey": "pilot." + anchor["action"],
                        "label": anchor["label"],
                        "rect": [x / width, y / height, w / width, h / height],
                        **({"targetStateKey": anchor["target"]} if "target" in anchor else {}),
                    }
                )
            scope = current()
            saved = editor.save_anchors(
                owner,
                pid,
                page["stable_key"],
                AnchorEdit(
                    analysis_set_id=aid,
                    page_revision_id=page["entity_revision_id"],
                    state_key=state_key,
                    name=item["name"],
                    viewport=viewport,
                    upload_artifact_id=upload(path, "image/png"),
                    anchors=anchors,
                ),
                str(scope["selected_analysis_set"]["selection_version"]),
                f"surface-{name}-{state_key}",
            )
            page["entity_revision_id"] = saved["page_revision_id"]
            captured[(name, state_key)] = (path, sha256(path.read_bytes()).hexdigest())

        verified = {}
        for key, (book, heading, line) in SOURCES.items():
            scope = current()
            matches = [
                row
                for row in scope["entities"]
                if row["type"] == "section"
                and row["locator"].get("startLine") == line
                and heading in row["name"]
                and any(
                    re.sub(r"\W", "", book) in re.sub(r"\W", "", part)
                    for part in row["metadata"].get("heading_path", [])
                )
            ]
            if len(matches) != 1:
                raise RuntimeError(f"无法唯一定位 {key} 的原文章节")
            section = matches[0]
            action = next(row for row in scope["actions"] if row["stable_key"] == f"pilot.{key}")
            excerpt = "\n".join(section["content"].splitlines()[1:]).strip()
            if not excerpt:
                raise RuntimeError(f"{key} 的原文无可读取的段落")
            editor.save_binding(
                owner,
                pid,
                action["stable_key"],
                BindingEdit(
                    analysis_set_id=aid,
                    action_revision_id=action["entity_revision_id"],
                    page_revision_id=pages["items"]["entity_revision_id"],
                    point_key=f"pilot.point.{key}",
                    point={
                        "title": f"{action['name']} · 原文要点",
                        "summary": excerpt[:520],
                        "applicability": "原文规定范围内的收费管理操作",
                    },
                    section_revision_ids=[section["entity_revision_id"]],
                    state_keys=["edit" if key == "penalty" else "default"],
                ),
                str(scope["selected_analysis_set"]["selection_version"]),
                f"point-{key}",
            )
            verified[key] = section

        scope = current()
        for name in pages:
            pages[name] = next(row for row in scope["pages"] if row["stable_key"] == pages[name]["stable_key"])
        section = verified["add"]
        action = next(row for row in scope["actions"] if row["stable_key"] == "pilot.add")

        def ref(entity):
            return {"entity_id": entity["entity_id"], "revision_id": entity["entity_revision_id"]}

        nodes = [
            {
                "key": "items",
                "kind": "action",
                "title": "新增收费项",
                "binding": ref(action),
                "x": 80,
                "y": 100,
                "prototype_state_key": "default",
            },
            *[
                {
                    "key": key,
                    "kind": "page",
                    "title": title,
                    "binding": ref(pages[key]),
                    "x": 80 + 220 * i,
                    "y": 100,
                    "prototype_state_key": "default",
                }
                for i, (key, title) in enumerate(
                    [("standards", "收费标准管理"), ("rooms", "房间收费设置"), ("billing", "计费出账")], 1
                )
            ],
            {"key": "end", "kind": "end", "title": "完成配置和出账流程核对", "x": 960, "y": 100},
        ]
        journey = JourneyService(service).create(
            owner,
            pid,
            {
                "analysis_set_id": aid,
                "dsl": {
                    "schema_version": "2.0",
                    "key": "charge-pilot-flow",
                    "title": "收费配置到批量出账",
                    "entry": "items",
                    "nodes": nodes,
                    "edges": [
                        {"key": f"flow-{i}", "source": left["key"], "target": right["key"], "label": ""}
                        for i, (left, right) in enumerate(zip(nodes, nodes[1:]))
                    ],
                    "semantics": {
                        "kind": "business_process",
                        "basis": "declared",
                        "purpose": "按试点手册核对收费配置与出账入口",
                        "outcome": "定位费用配置、应用和出账步骤",
                        "source_refs": [ref(section)],
                        "unknowns": ["页面来自离线演示，实际计费结果需要业务验证"],
                    },
                },
            },
            "create-journey",
        )
        editor.save_settings(
            owner,
            pid,
            SettingsEdit(
                analysis_set_id=aid,
                entry_page_key=pages["items"]["stable_key"],
                menu_order=[pages[key]["stable_key"] for key in PAGE_ROUTES],
            ),
            "0",
        )
        release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "save-pilot-snapshot")
        reader = PrototypeReader(service)
        times = []
        for _ in range(31):
            for key, section in verified.items():
                started = perf_counter()
                context = reader.context(
                    owner,
                    pid,
                    pages["items"]["stable_key"],
                    f"pilot.{key}",
                    "edit" if key == "penalty" else "default",
                    None,
                    release["id"],
                )
                assert context["points"][0]["sources"][0]["section_revision_id"] == section["entity_revision_id"]
                times.append((perf_counter() - started) * 1000)
        for (name, state), (path, digest) in captured.items():
            page = pages[name]
            assert (
                prototype_image(
                    service, owner, pid, page["stable_key"], state, page["entity_revision_id"], None, release["id"]
                )
                == path.read_bytes()
            )
        return {
            "project_id": pid,
            "analysis_set_id": aid,
            "release_id": release["id"],
            "journey_id": journey["journey_id"],
            "reports": reports,
            "pictures_checked": len(captured),
            "exact_sections_checked": len(verified),
            "viewport_sha256": {f"{page}:{state}": digest for (page, state), (_, digest) in captured.items()},
            "service_warm_ms_p95": round(sorted(times[3:])[85], 2),
        }
    finally:
        service.stop_worker()
        service.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("data-dir", "prototype", "handbook", "capture-dir", "capture-manifest"):
        parser.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.data_dir.resolve(),
                args.prototype,
                args.handbook,
                args.capture_dir,
                json.loads(args.capture_manifest.read_bytes()),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
