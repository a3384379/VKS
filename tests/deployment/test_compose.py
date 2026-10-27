"""Opt-in isolated deployment acceptance. Never touches a pre-existing stack."""

from pathlib import Path
import json
import os
import secrets
import shutil
import subprocess
import time
from io import BytesIO
import zipfile
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.getenv("VKS_TEST_COMPOSE") != "1", reason="未启用隔离 Compose 部署验收")
def test_real_compose_reserved_passwords_durable_job_publish_and_exports(tmp_path):
    stack = "vks-verify-" + secrets.token_hex(5)
    api_image = stack + "-api:local"
    web_image = stack + "-web:local"
    env_file = tmp_path / "private.env"
    env_file.write_text(
        "POSTGRES_PASSWORD='" + secrets.token_urlsafe(22) + "@:/?#%+$![]&=中文'\n"
        "RABBITMQ_PASSWORD='" + secrets.token_urlsafe(22) + "@:/?#%+$![]&='\nVKS_PORT=0\n"
    )
    env_file.chmod(0o600)
    # BuildKit session headers reject the Unicode checkout path on some Docker
    # Desktop versions. Copy only allowlisted build inputs into a private ASCII
    # directory; private instance data and unrelated workspace files never enter it.
    build_context = tmp_path / "context"
    build_context.mkdir(mode=0o700)
    for name in ("services", "packages", "fixtures", "deploy", "scripts", "apps"):
        shutil.copytree(
            ROOT / name,
            build_context / name,
            ignore=shutil.ignore_patterns("node_modules", "dist", "__pycache__", ".env", ".env.*", ".vks"),
        )
    for name in ("package.json", "package-lock.json", "alembic.ini", ".dockerignore"):
        shutil.copy2(ROOT / name, build_context / name)
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "api": {"image": api_image, "build": {"context": str(build_context)}},
                    "worker-index": {"image": api_image},
                    "worker-model": {"image": api_image},
                    "web": {"image": web_image, "build": {"context": str(build_context)}},
                    # Keep Erlang scheduling finite on small verification VMs.
                    "broker": {"environment": {"ERL_FLAGS": "+S 2:2 +A 4"}},
                }
            }
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "VKS_PORT", "COMPOSE_PROJECT_NAME")
    }
    command = [
        "docker",
        "compose",
        "--project-name",
        stack,
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "deploy/compose.yaml"),
        "-f",
        str(override),
    ]
    log = tmp_path / "private-deployment.log"
    log.touch(mode=0o600)

    def compose(*args, timeout=360):
        result = subprocess.run(
            [*command, *args], env=env, cwd=tmp_path, capture_output=True, timeout=timeout, check=False
        )
        with log.open("ab") as f:
            f.write(result.stdout + result.stderr)
        assert result.returncode == 0, "隔离 Compose 操作失败；诊断仅保留在临时私有日志"
        return result.stdout.decode()

    try:
        compose("build", "api", "web", timeout=600)
        compose("up", "--detach", "--wait", "--wait-timeout", "180", timeout=220)
        port = compose("port", "web", "80").strip().rsplit(":", 1)[-1]
        url = "http://127.0.0.1:" + port
        with httpx.Client(base_url=url, timeout=20) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/v1/ready").json()["dispatcher"] == "celery"
            bootstrap = compose("exec", "-T", "api", "cat", "/data/vks/bootstrap-token").strip()
            session = client.post("/api/v1/auth/session", json={"bootstrap_token": bootstrap})
            assert session.status_code == 200
            csrf = session.json()["data"]["csrf_token"]
            client.headers.update({"x-csrf-token": csrf, "origin": url})

            def post(path, value=None, key=None):
                response = client.post(
                    "/api/v1" + path, json=value, headers={"Idempotency-Key": key or secrets.token_hex(8)}
                )
                assert response.status_code < 300, (response.status_code, response.json().get("error", {}).get("code"))
                return response.json()["data"]

            p = post("/projects", {"key": "charge-demo", "name": "隔离容器验收"})
            pid = p["id"]
            compose("stop", "worker-index")
            job = post(f"/projects/{pid}/imports/sample", key="durable-import")
            # The accepted job remains in the PostgreSQL outbox while the API
            # process is replaced and there is no available index worker.
            compose("restart", "api")
            compose("up", "--detach", "--wait", "--wait-timeout", "60", "api", timeout=80)
            compose("start", "worker-index")

            def finished(jid):
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline:
                    response = client.get(f"/api/v1/jobs/{jid}")
                    assert response.status_code == 200
                    value = response.json()["data"]
                    if value["state"] in ("succeeded", "failed", "cancelled"):
                        assert value["state"] == "succeeded", value.get("error", {}).get("code", "job_failed")
                        return value
                    time.sleep(0.25)
                raise AssertionError("隔离队列任务超时")

            value = finished(job["id"])
            assert value["attempt"] == 1
            w = client.get(f"/api/v1/projects/{pid}/workspace").json()["data"]
            assert len(w["actions"]) == 4
            aid = w["selected_analysis_set"]["id"]
            # The V2 deterministic template uses the model queue while the
            # provider remains disabled. Its output still needs human review.
            compose("stop", "worker-model")
            response = client.post(
                f"/api/v2/projects/{pid}/knowledge-tasks",
                json={
                    "analysis_set_id": aid,
                    "task_kind": "business_flow",
                    "action_ids": [row["entity_id"] for row in w["actions"][:3]],
                },
                headers={"Idempotency-Key": "compose-v2-task"},
            )
            assert response.status_code == 202, response.text
            task = response.json()["data"]
            assert client.get(f"/api/v1/jobs/{task['id']}").json()["data"]["state"] == "queued"
            compose("start", "worker-model")
            assert finished(task["id"])["attempt"] == 1
            task_result = client.get(f"/api/v2/knowledge-tasks/{task['id']}").json()["data"]
            assert task_result["stage"] == "READY_FOR_REVIEW" and task_result["stage_artifacts"]
            assert task_result["report"]["candidate_only"] and task_result["report"]["model_calls"] == 0
            journeys = client.get(f"/api/v2/projects/{pid}/journeys", params={"analysis_set_id": aid}).json()["data"]
            assert len(journeys) == 1 and len(journeys[0]["dsl"]["nodes"]) == 4
            for r in w["relations"]:
                response = client.post(
                    f"/api/v1/relations/{r['id']}/review",
                    json={
                        "decision": "confirm",
                        "reason": "独立容器核对合成 fixture",
                        "evidence_ids": r["evidence_ids"],
                    },
                    headers={"If-Match": str(r["row_version"])},
                )
                assert response.status_code == 200
            blocked = post(f"/projects/{pid}/release-candidates", {"analysis_set_id": aid})
            assert blocked["blockers"], "An unreviewed V2 flow must block publication in the deployed stack"
            for journey in journeys:
                response = client.post(
                    f"/api/v2/journeys/{journey['journey_id']}/review",
                    json={
                        "analysis_set_id": aid,
                        "revision_id": journey["revision_id"],
                        "decision": "approve",
                        "reason": "Synthetic Compose human review",
                    },
                    headers={
                        "If-Match": journey["revision_id"],
                        "Idempotency-Key": "review-" + journey["revision_id"],
                    },
                )
                assert response.status_code == 200, response.text
            candidate = post(
                f"/projects/{pid}/release-candidates", {"analysis_set_id": w["selected_analysis_set"]["id"]}
            )
            confirm = post(f"/release-candidates/{candidate['candidate_id']}/confirm")
            release = post(
                f"/release-candidates/{candidate['candidate_id']}/publish",
                {k: confirm[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")}
                | {"change_note": "隔离部署验收"},
            )
            frozen = client.get(f"/api/v2/projects/{pid}/journeys", params={"release_id": release["id"]}).json()["data"]
            assert [(row["revision_id"], row["dsl"]) for row in frozen] == [
                (row["revision_id"], row["dsl"]) for row in journeys
            ]
            explained = client.get(
                f"/api/v2/actions/{w['actions'][0]['entity_id']}/explanation",
                params={"project_id": pid, "release_id": release["id"]},
            )
            assert explained.status_code == 200 and explained.json()["data"]["release_id"] == release["id"]
            for format in ("pdf", "vksbundle"):
                export = post(
                    f"/projects/{pid}/exports", {"release_id": release["id"], "format": format, "include_source": False}
                )
                result = finished(export["id"])
                response = client.get(result["result"]["download_url"])
                assert response.status_code == 200
                if format == "pdf":
                    assert response.content.startswith(b"%PDF-") and response.headers["content-type"].startswith(
                        "application/pdf"
                    )
                else:
                    with zipfile.ZipFile(BytesIO(response.content)) as z:
                        manifest = json.loads(z.read("manifest.json"))
                        assert manifest["allow_platform_llm"] is False
                        assert not any(a["kind"] == "source" for a in manifest["artifacts"])
                        exported_journey = json.loads(z.read("export/journeys/" + journeys[0]["journey_id"] + ".json"))
                        assert exported_journey["definition"] == journeys[0]["dsl"]
                        assert exported_journey["approval_transferred"] is False
            assert len(client.get(f"/api/v1/projects/{pid}/releases").json()["data"]) == 1
    finally:
        diagnostic = subprocess.run(
            [*command, "logs", "--no-color", "--tail", "100"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            timeout=20,
            check=False,
        )
        with log.open("ab") as f:
            f.write(diagnostic.stdout + diagnostic.stderr)
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            timeout=60,
            check=False,
        )
        # Only remove image tags created for this test, never shared base images.
        subprocess.run(["docker", "image", "rm", api_image, web_image], capture_output=True, timeout=30, check=False)
