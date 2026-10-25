"""Load only the repository's synthetic validation project into a local VKS instance."""

import argparse
import hashlib
import json
from vks_api.service import Service
from vks_api.scenarios import ScenarioService
from scripts.build_pilot import bundle_bytes


def seed(data_dir):
    service = Service(data_dir)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(
        actor,
        {
            "key": "pilot-jfa",
            "name": "JF-A · 合成验证基线",
            "description": "30动作、15规范化API、30章节；三类声明场景；未进行真实业务或独立人工Gold验收。",
        },
        "seed-jfa",
    )
    raw = bundle_bytes()
    uploaded = service.create_upload(
        actor,
        project["id"],
        {
            "filename": "pilot-jfa.vksbundle.zip",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "application/zip",
        },
    )
    service.put_upload(actor, project["id"], uploaded["upload_session_id"], raw)
    job = service.import_bundle(
        actor,
        project["id"],
        {"kind": "bundle", "artifact_ids": [uploaded["artifact_id"]]},
        "seed-jfa-import-" + uploaded["artifact_id"],
    )
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    if result["state"] != "succeeded":
        raise RuntimeError(result.get("error", {}).get("code", "seed failed"))
    aid = result["result"]["analysis_set_id"]
    scenario_service = ScenarioService(service)
    for suffix, label in (("success", "成功状态"), ("validation", "参数校验失败"), ("forbidden", "权限不足")):
        dsl = {
            "schema_version": "1.0",
            "scenario_key": "jfa." + suffix,
            "name": label + " · 合成声明",
            "environment_ref": "jfa-mock-unconfigured",
            "dataset_ref": "jfa-synthetic",
            "risk_level": "read_only",
            "timeout_ms": 10000,
            "preconditions": ["仅在合成资料中声明；未进行真实录制"],
            "steps": [
                {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 3000, "risk": "read_only"},
                {"step_key": "snapshot", "kind": "snapshot", "name": label, "timeout_ms": 3000, "risk": "read_only"},
            ],
            "explanation_graph": {
                "projectKey": "pilot-jfa",
                "baselineKey": "synthetic-jfa-v1",
                "states": [
                    {"stateKey": "ready", "label": "初始", "evidenceOrigin": "declared"},
                    {"stateKey": suffix, "label": label, "evidenceOrigin": "declared"},
                ],
                "transitions": [
                    {
                        "transitionKey": "choose-" + suffix,
                        "fromState": "ready",
                        "toState": suffix,
                        "actionKey": "payments.invoice.confirm",
                        "conditionLabel": "合成条件：" + label,
                        "evidenceOrigin": "declared",
                        "scenarioRef": "jfa." + suffix,
                    }
                ],
            },
        }
        scenario_service.create(
            actor, project["id"], {"analysis_set_id": aid, "dsl": dsl}, "seed-jfa-scenario-" + suffix
        )
    w = service.workspace(actor, project["id"], aid)
    service.engine.dispose()
    return {
        "project_id": project["id"],
        "analysis_set_id": aid,
        "actions": len(w["actions"]),
        "scenarios": len(w["scenarios"]),
        "relations": len(w["relations"]),
        "published": False,
        "runtime_validated": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=".vks")
    args = parser.parse_args()
    print(json.dumps(seed(args.data_dir), ensure_ascii=False))
