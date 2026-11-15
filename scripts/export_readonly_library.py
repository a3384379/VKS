"""Export a local owner's project through the normal snapshot/job boundary."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vks_api.materials import MaterialService
from vks_api.models import uid
from vks_api.service import DomainError, Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--analysis-id")
    parser.add_argument("--include-source", action="store_true")
    parser.add_argument("--document-mode", choices=("project", "original"), default="project")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("目标文件已存在，请选择新的输出文件名")
    service = Service(args.data_dir)
    try:
        _, owner = service.login(service.bootstrap_token)
        release = args.release_id
        if not release:
            materials = MaterialService(service)
            aid = args.analysis_id or materials.open(owner, args.project_id)["analysis_set_id"]
            release = materials.checkpoint(owner, args.project_id, aid, "只读成品库导出", uid())["id"]
        job = service.export(
            owner,
            args.project_id,
            {"release_id": release, "format": "readonly_html", "include_source": args.include_source,
             "document_mode": args.document_mode},
            uid(),
        )
        print(json.dumps({"job_id": job["id"], "release_id": release, "state": "preparing"}), flush=True)
        service.process_job(job["id"])
        while True:
            result = service.get_job(owner, job["id"])
            if result["state"] not in ("queued", "running", "retry_wait"):
                break
            time.sleep(0.5)
        if result["state"] != "succeeded":
            error = result.get("error") or {}
            raise DomainError(error.get("code", "VKS_EXPORT_FAILED"), error.get("message", "成品库未生成"))
        raw, _ = service.download_export(owner, args.project_id, job["id"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as target:
            target.write(raw)
        print(
            json.dumps(
                {"output": str(args.output.resolve()), "bytes": len(raw), "state": "succeeded"}, ensure_ascii=False
            )
        )
    finally:
        service.engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        print(json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
