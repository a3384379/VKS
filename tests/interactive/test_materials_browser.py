"""Real browser task: create project, install files, replace docs and restore a version."""

import os
import socket
import subprocess
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
from playwright.sync_api import expect, sync_playwright

from apps.runtime.server import RuntimeHandler
from tests.interactive import test_materials
from tests.ui.test_frontend_browser import chromium_path, live_api

ROOT = Path(__file__).resolve().parents[2]
materials = test_materials.materials


def test_project_import_replace_and_complete_rollback_in_studio(materials, tmp_path, monkeypatch):
    web, _service, _owner, _manager, projects = materials
    monkeypatch.setenv("VKS_RUNTIME_DATA_DIR", str(tmp_path / "runtime-private"))
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    Thread(target=runtime.serve_forever, daemon=True).start()
    process = None
    try:
        with live_api(web.app) as api:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            script = """import {createServer} from 'vite';const s=await createServer({root:process.argv[1]+'/apps/studio',configFile:process.argv[1]+'/apps/studio/vite.config.ts',cacheDir:process.argv[2],define:{'import.meta.env.VITE_RUNTIME_PORT':JSON.stringify(process.argv[5])},server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}});await s.listen();"""
            with (tmp_path / "vite.log").open("wb") as log:
                process = subprocess.Popen(
                    [
                        "node",
                        "--input-type=module",
                        "-e",
                        script,
                        str(ROOT),
                        str(tmp_path / "cache"),
                        str(port),
                        api,
                        str(runtime.server_port),
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                url = f"http://127.0.0.1:{port}"
                monkeypatch.setenv("VKS_STUDIO_ALLOWED_ORIGINS", url)
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
                    page = browser.new_page(viewport={"width": 1600, "height": 1000})
                    errors = []
                    broken_packages = []
                    page.on(
                        "response",
                        lambda r: (
                            broken_packages.append(r.url)
                            if "/packages/" in r.url and "/content?" in r.url and r.status >= 400
                            else None
                        ),
                    )
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(url + f"/#project={projects[0][0]}&analysis={projects[0][1]}&view=projects")
                    page.get_by_label("密码", exact=True).fill("owner")
                    page.get_by_role("button", name="打开交互工作台").click()
                    page.get_by_role("button", name="新建项目", exact=True).click()
                    page.get_by_label("项目名称", exact=True).fill("独立的收费项目")
                    page.get_by_role("button", name="创建并进入", exact=True).click()
                    expect(page.get_by_role("button", name="导入项目资料", exact=True)).to_be_visible()
                    page.get_by_role("button", name="导入项目资料", exact=True).click()
                    picker = page.get_by_label("选择资料文件", exact=True)
                    page.get_by_label("导入方式", exact=True).select_option("")
                    picker.set_input_files(
                        {
                            "name": "收费原型.html",
                            "mimeType": "text/html",
                            "buffer": '<title>收费原型</title><button id="add" onclick="document.querySelector(\'#edit\').hidden=false">新增收费项</button><div id="edit" hidden><input aria-label="名称"></div>'.encode(),
                        }
                    )
                    page.get_by_role("button", name="解析并预览", exact=True).click()
                    expect(page.locator(".material-staging")).to_contain_text("收费原型")
                    page.get_by_label("资料类型", exact=True).select_option("documents")
                    page.get_by_label("导入方式", exact=True).select_option("")
                    picker.set_input_files(
                        {
                            "name": "收费指南.md",
                            "mimeType": "text/markdown",
                            "buffer": "# 收费指南\n\n原始版本填写名称。".encode(),
                        }
                    )
                    page.get_by_role("button", name="解析并预览", exact=True).click()
                    expect(page.locator(".material-staging")).to_contain_text("收费指南")
                    page.get_by_role("button", name="启用这些资料", exact=True).click()
                    expect(page.locator(".management-count")).to_contain_text("2 个")
                    page.get_by_label("版本名称", exact=True).fill("UI 原始完整版本")
                    page.get_by_role("button", name="保存版本", exact=True).click()
                    expect(page.locator(".material-history")).to_contain_text("UI 原始完整版本")
                    page.get_by_role("button", name="原型", exact=True).click()
                    source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
                    source.get_by_role("button", name="新增收费项", exact=True).click()
                    source.get_by_label("名称", exact=True).fill("保留原型表单")
                    page.get_by_role("button", name="资料管理", exact=True).click()
                    page.get_by_label("资料类型", exact=True).select_option("documents")
                    page.get_by_label("导入方式", exact=True).select_option(label="更新：收费指南")
                    picker.set_input_files(
                        {
                            "name": "收费指南.md",
                            "mimeType": "text/markdown",
                            "buffer": "# 收费指南\n\n更新版本增加类别。".encode(),
                        }
                    )
                    page.get_by_role("button", name="解析并预览", exact=True).click()
                    expect(page.locator(".material-staging")).to_contain_text("更新 1")
                    page.get_by_role("button", name="启用这些资料", exact=True).click()
                    expect(page.locator(".banner.success")).to_contain_text("替换前版本已自动保存")
                    page.get_by_role("button", name="原型", exact=True).click()
                    expect(source.get_by_label("名称", exact=True)).to_have_value("保留原型表单")
                    page.get_by_role("button", name="文档", exact=True).click()
                    expect(page.locator(".document-reader")).to_contain_text("更新版本增加类别")
                    page.get_by_role("button", name="资料管理", exact=True).click()
                    baseline = page.locator(".material-history article").filter(has_text="UI 原始完整版本")
                    baseline.get_by_role("button", name="恢复", exact=True).click()
                    expect(page.get_by_role("heading", name="原型与文档", exact=True)).to_be_visible()
                    page.get_by_role("button", name="文档", exact=True).click()
                    expect(page.locator(".document-reader")).to_contain_text("原始版本填写名称")
                    page.reload()
                    expect(page.locator(".document-reader")).to_contain_text("原始版本填写名称")
                    page.get_by_role("combobox", name="当前项目", exact=True).select_option(projects[1][0])
                    expect(page.get_by_role("button", name="导入项目资料", exact=True)).to_be_visible()
                    assert "独立的收费项目" not in page.locator(".contextbar").inner_text()
                    assert not errors, errors
                    assert not broken_packages, broken_packages
                    browser.close()
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
        runtime.shutdown()
        runtime.server_close()
