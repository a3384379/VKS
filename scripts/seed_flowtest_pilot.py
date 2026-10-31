"""Import a prevalidated FlowTest pilot bundle into VKS. Never writes to FlowTest."""

from pathlib import Path
import argparse
from hashlib import sha256
import json
from vks_api.service import Service
from vks_api.project_profile import ProfileService
from vks_api.knowledge import KnowledgeService
from vks_api.scenarios import ScenarioService


def seed(data_dir, bundle_path):
    service = Service(data_dir)
    _, actor = service.login(service.bootstrap_token)
    projects, _ = service.list_projects(actor, limit=100)
    project = next((p for p in projects if p["key"] == "flowtest"), None)
    if project is None:
        project = service.create_project(
            actor,
            {
                "key": "flowtest",
                "name": "FlowTest · 真实项目试点",
                "description": "固定Git源码、原始使用手册与单列运行OpenAPI；版本未对齐；所有关系待人员审核。",
            },
            "flowtest-pilot",
        )
    profile = ProfileService(service).get(actor, project["id"])
    ProfileService(service).update(
        actor,
        project["id"],
        {
            "business_domain": "接口自动化与可视化工作流",
            "responsible_user_id": actor.id,
            "visibility": "project_members",
            "default_analysis_language": "source_anchors",
        },
        profile["row_version"],
    )
    connectors = KnowledgeService(service).connectors(actor, project["id"])
    if not any(c["key"] == "flowtest-code" for c in connectors):
        KnowledgeService(service).add_connector(
            actor,
            project["id"],
            {
                "key": "flowtest-code",
                "name": "FlowTest 固定Git源码",
                "provider": "git",
                "remote_locator": "local-git:FlowTest",
                "secret_ref": None,
            },
        )
    raw = Path(bundle_path).read_bytes()
    h = sha256(raw).hexdigest()
    upload = service.create_upload(
        actor,
        project["id"],
        {"filename": "flowtest.vksbundle.zip", "size_bytes": len(raw), "sha256": h, "media_type": "application/zip"},
    )
    service.put_upload(actor, project["id"], upload["upload_session_id"], raw)
    preview = service.preflight(actor, project["id"], {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]})
    job = service.import_bundle(
        actor,
        project["id"],
        {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]},
        "flowtest-pilot-import-" + upload["artifact_id"],
    )
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    if result["state"] != "succeeded":
        raise RuntimeError(result.get("error", {}).get("code", "import failed"))
    aid = result["result"]["analysis_set_id"]
    w = service.workspace(actor, project["id"], aid)
    baseline = w["selected_analysis_set"]["key"]
    scenarios = ScenarioService(service)
    for suffix, label in (("success", "成功路径"), ("validation", "输入校验失败"), ("forbidden", "权限不足")):
        dsl = {
            "schema_version": "1.0",
            "scenario_key": "flowtest.pilot." + suffix,
            "name": "FlowTest " + label + " · 待录制声明",
            "environment_ref": "flowtest-test-unconfigured",
            "dataset_ref": "flowtest-synthetic-unconfigured",
            "risk_level": "read_only",
            "timeout_ms": 10000,
            "preconditions": [
                "使用专用隔离测试账号及合成项目；未采集现有用户数据",
                "此分支为人工声明，不代表已观测结果",
            ],
            "steps": [
                {"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 3000, "risk": "read_only"},
                {
                    "step_key": "snapshot",
                    "kind": "snapshot",
                    "name": "待验证：" + label,
                    "timeout_ms": 3000,
                    "risk": "read_only",
                },
            ],
            "explanation_graph": {
                "projectKey": "flowtest",
                "baselineKey": baseline,
                "states": [
                    {"stateKey": "ready", "label": "初始", "evidenceOrigin": "declared"},
                    {"stateKey": suffix, "label": label, "evidenceOrigin": "declared"},
                ],
                "transitions": [
                    {
                        "transitionKey": "choose-" + suffix,
                        "fromState": "ready",
                        "toState": suffix,
                        "actionKey": "flowtest.action.create-api",
                        "conditionLabel": "待验证条件：" + label,
                        "evidenceOrigin": "declared",
                        "scenarioRef": "flowtest.pilot." + suffix,
                    }
                ],
            },
        }
        scenarios.create(
            actor, project["id"], {"analysis_set_id": aid, "dsl": dsl}, "flowtest-pilot-scenario-" + suffix
        )
    w = service.workspace(actor, project["id"], aid)
    data = {
        "project_id": project["id"],
        "analysis_set_id": aid,
        "job_id": job["id"],
        "bundle_sha256": h,
        "file_count": preview["file_count"],
        "actions": len(w["actions"]),
        "api_declarations": sum(e["type"] == "api" for e in w["entities"]),
        "chapters": sum(e["type"] == "section" for e in w["entities"]),
        "scenarios": len(w["scenarios"]),
        "relations": len(w["relations"]),
        "saved_directly": True,
        "published": False,
        "runtime_source_alignment": w["selected_analysis_set"]["runtime_alignment"],
        "platform_model_calls": 0,
    }
    service.engine.dispose()
    return data


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default=".vks")
    p.add_argument("--bundle", required=True)
    p.add_argument("--report", required=True)
    a = p.parse_args()
    result = seed(a.data_dir, a.bundle)
    out = Path(a.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    out.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False))
