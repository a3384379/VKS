"""Import the two user-selected offline packages into an isolated Studio workspace."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from vks_api.knowledge import ImportService
from vks_api.prototype import preview_fingerprint
from vks_api.service import Service


def prepare(directory, prototype, handbook):
    if (directory / "vks.db").exists():
        raise SystemExit("目标实例已存在，请使用新的数据目录")
    service = Service(directory, database_url=f"sqlite:///{directory / 'vks.db'}")
    try:
        _, owner = service.login(service.bootstrap_token)
        pid = service.create_project(owner, {"key": "charge-studio", "name": "云收费知识工作台"}, "studio-project")[
            "id"
        ]
        aid = service.create_analysis(owner, pid, {"name": "3.0 交互工作台草稿"}, "studio-analysis")["id"]
        for path in (prototype, handbook):
            raw = path.read_bytes()
            upload = service.create_upload(
                owner,
                pid,
                {
                    "filename": path.name,
                    "size_bytes": len(raw),
                    "sha256": sha256(raw).hexdigest(),
                    "media_type": "text/html",
                },
            )
            service.put_upload(owner, pid, upload["upload_session_id"], raw)
            payload = {"kind": "prototype", "artifact_ids": [upload["artifact_id"]], "analysis_set_id": aid}
            parsed = ImportService(service).prepare(actor=owner, pid=pid, payload=payload)
            selected = service.workspace(owner, pid, aid)["selected_analysis_set"]["selection_version"]
            with service.db() as db:
                job = service._new_job(
                    db,
                    owner,
                    pid,
                    "import",
                    {
                        **payload,
                        "_prototype_preview": {
                            "selection_version": selected,
                            "digest": preview_fingerprint(parsed, payload, selected),
                        },
                    },
                )
                jid = job.id
            service.process_job(jid)
            result = service.get_job(owner, jid)
            if result["state"] != "succeeded":
                raise RuntimeError(result.get("error"))
        config = {"project_id": pid, "analysis_set_id": aid}
        (directory.parent / "preview.json").write_text(json.dumps(config, indent=2))
        print(json.dumps(config))
    finally:
        service.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--prototype", type=Path, required=True)
    parser.add_argument("--handbook", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.data.resolve(), args.prototype, args.handbook)
