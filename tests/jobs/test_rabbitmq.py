"""Opt-in real RabbitMQ/Celery roundtrip, never a protocol mock."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from vks_api.jobs import dispatch_pending
from vks_api.service import Service

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not os.getenv("VKS_TEST_BROKER_URL"), reason="未配置隔离 RabbitMQ 验收地址")
def test_actual_celery_worker_reconnects_and_commits_duplicate_delivery_once(tmp_path, monkeypatch):
    broker = os.environ["VKS_TEST_BROKER_URL"]
    monkeypatch.setenv("VKS_BROKER_URL", broker)
    monkeypatch.setenv("VKS_DATA_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("VKS_TASK_MODE", "external")
    monkeypatch.delenv("VKS_DATABASE_URL", raising=False)
    service = Service(tmp_path / "instance")
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "charge-demo", "name": "真实消息队列"}, "project")
    job = service.demo_import(owner, p["id"], "demo")
    from services.worker.celery_app import celery

    celery.conf.broker_url = broker
    log_path = tmp_path / "worker-private.log"
    with log_path.open("w") as output:
        log_path.chmod(0o600)
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "services.worker.celery_app:celery",
                "worker",
                "-P",
                "solo",
                "--concurrency",
                "1",
                "-Q",
                "vks-index",
                "--loglevel",
                "WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "services/api") + os.pathsep + str(ROOT)},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            assert dispatch_pending(service) == 1
            celery.send_task("vks.execute_job", args=[job["id"]], queue="vks-index", retry=False)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                value = service.get_job(owner, job["id"])
                if value["state"] in ("succeeded", "failed"):
                    break
                assert worker.poll() is None, "隔离 Worker 启动失败；私有日志保留在临时目录"
                time.sleep(0.2)
            assert value["state"] == "succeeded", value.get("error", {}).get("code", "task-timeout")
            assert value["attempt"] == 1
            assert len(service.workspace(owner, p["id"])["analysis_sets"]) == 1
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            service.engine.dispose()
