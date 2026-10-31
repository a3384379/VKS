
from test_model_gateway import system as system  # pytest fixture registration
from pathlib import Path
from threading import Thread
import os
import shutil
import socket
import subprocess
import time

import pytest
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright
import uvicorn

from test_task_planning import plan
from vks_api.app import create_app
from vks_api.journeys import JourneyService

ROOT = Path(__file__).resolve().parents[2]


def test_real_browser_executes_model_task_directly_with_one_synthetic_call(system, tmp_path):
    service, actor, project, aid, _, requests, response, _ = system
    if not shutil.which("node") or not (ROOT / "node_modules/vite").is_dir():
        pytest.skip("前端依赖未安装；运行 npm ci")
    response["plan"] = plan
    with (tmp_path / "frontend-build.log").open("wb") as output:
        built = subprocess.run(["npm", "run", "build"], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=180)
    assert built.returncode == 0
    app = create_app(service.data_dir, enable_worker=True)
    app.mount("/", StaticFiles(directory=ROOT / "apps/web/dist", html=True))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs=dict(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert server.started
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        with sync_playwright() as p:
            executable = os.environ.get("VKS_CHROMIUM_EXECUTABLE", p.chromium.executable_path)
            if not Path(executable).is_file():
                installed = sorted(
                    (Path.home() / "Library/Caches/ms-playwright").glob(
                        "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
                    )
                )
                if installed:
                    executable = str(installed[-1])
            if not Path(executable).is_file():
                pytest.skip("独立 Chromium 未安装")
            browser = p.chromium.launch(executable_path=executable, headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1100))
            try:
                page.goto(url + f"/#view=agent&project={project['id']}")
                page.get_by_label("登录名", exact=True).fill("owner")
                page.get_by_label("密码", exact=True).fill("owner")
                page.get_by_role("button", name="打开工作空间").click()
                page.get_by_label("知识任务模板").select_option("knowledge_plan")
                page.get_by_label("知识任务说明").fill(
                    "Inspect this fixed action and propose a bounded flow and document"
                )
                expect(page.get_by_role("checkbox", name="授权本任务至多一次模型调用", exact=False)).to_have_count(0)
                with page.expect_response(
                    lambda r: "/knowledge-tasks" in r.url and r.request.method == "POST"
                ) as created:
                    page.get_by_role("button", name="预检并创建任务", exact=True).click()
                assert created.value.status == 202, created.value.text()
                jid = created.value.json()["data"]["id"]
                expect(
                    page.get_by_text("产物直接保存到当前知识版本；静态与声明信息不能证明运行因果。", exact=True)
                ).to_be_visible(timeout=15000)
                expect(page.get_by_role("button", name="确认计划并继续", exact=True)).to_have_count(0)
                current = page.request.get(url + f"/api/v2/knowledge-tasks/{jid}").json()["data"]
                assert current["state"] == "succeeded" and current["report"]["saved_directly"]
                assert len(requests) == 1 and len(JourneyService(service).list(actor, project["id"], aid)) == 1
                page.set_viewport_size(dict(width=375, height=1000))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app.state.service.engine.dispose()
