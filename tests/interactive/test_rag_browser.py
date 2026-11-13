"""The actual Studio can index ordinary Markdown without an imported prototype."""

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.interactive_ai import InteractiveAssistant
from vks_api.models import Job, uid
from vks_api.vector_rag import RagSettings

from tests.interactive import test_materials
from tests.ui.test_frontend_browser import chromium_path, live_api

ROOT = Path(__file__).resolve().parents[2]
materials = test_materials.materials


def test_doc_only_studio_displays_status_and_submits_real_index_job(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    document = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 普通 Markdown\n\n没有任何原型也能建立知识索引。"
    )
    installed = test_materials.activate(service, owner, manager, pid, aid, [document])
    service.process_job(installed["job_id"])
    with service.db() as db:
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(
                    enabled=True, allow_external=True, endpoint="https://api.openai.com/v1", model_id="synthetic"
                ).model_dump(),
            )
        )
    rag = InteractiveAssistant(service).rag
    rag.save(owner, pid, RagSettings(enabled=True, dimensions=512))
    calls = []

    def embeddings(config, payload):
        calls.append(payload)
        return {
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 511} for i in range(len(payload["input"]))],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(ai_gateway, "request_model", embeddings)
    process = None
    try:
        with live_api(web.app) as api:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            script = """import {createServer} from 'vite';const s=await createServer({root:process.argv[1]+'/apps/studio',configFile:process.argv[1]+'/apps/studio/vite.config.ts',cacheDir:process.argv[2],server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}});await s.listen();"""
            with (tmp_path / "vite.log").open("wb") as log:
                process = subprocess.Popen(
                    ["node", "--input-type=module", "-e", script, str(ROOT), str(tmp_path / "cache"), str(port), api],
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                url = f"http://127.0.0.1:{port}"
                for _ in range(150):
                    assert process.poll() is None
                    try:
                        if httpx.get(url, timeout=0.4).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(url + f"/#project={pid}&analysis={aid}&view=ai")
                        page.get_by_label("密码", exact=True).fill("owner")
                        page.get_by_role("button", name="打开交互工作台").click()
                        panel = page.get_by_role("region", name="RAG 向量知识库")
                        expect(panel).to_contain_text("0 / 1 章节已索引")
                        button = panel.get_by_role("button", name="建立向量索引", exact=True)
                        expect(button).to_be_enabled()
                        button.click()
                        expect(panel).to_contain_text("文档索引任务已提交")
                        with service.db() as db:
                            job = db.scalar(
                                select(Job).where(
                                    Job.project_id == pid, Job.kind == "material_migrate", Job.state == "queued"
                                )
                            )
                            assert job and job.input_json["index_documents"] and not job.input_json["index_package_id"]
                            jid = job.id
                        service.process_job(jid)
                        panel.get_by_role("button", name="刷新状态", exact=True).click()
                        expect(panel).to_contain_text("1 / 1 章节已索引")
                        expect(panel).to_contain_text("混合检索可用")
                        assert calls and not errors
                        release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "UI doc-only index")
                        page.goto(url + f"/#project={pid}&release={release['id']}&view=ai")
                        expect(panel).to_contain_text("1 / 1 章节已索引")
                        expect(panel).to_contain_text("当前为只读快照")
                        expect(panel.get_by_role("button", name="更新 / 继续索引", exact=True)).to_be_disabled()
                    finally:
                        browser.close()
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
