"""Studio projects imported category order and lazily expands article chapters."""

import os
import re
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from playwright.sync_api import expect, sync_playwright
from vks_api.interactive import InteractiveKnowledgeService

from tests.interactive import test_materials
from tests.ui.test_frontend_browser import chromium_path, live_api

ROOT = Path(__file__).resolve().parents[2]
materials = test_materials.materials


def test_studio_categories_source_order_lazy_heading_tree_and_global_search(materials, tmp_path):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    files = [
        (
            "02业务/运维/z.md",
            "开头介绍文字保留在正文。\n\n# 业务概览 Z\n\n## 办理步骤\n\n### 深层节点\n\n准确办理内容。\n\n## 异常处理\n\n需要复核。",
        ),
        ("02业务/运维/a.md", "# 业务补充 A\n\n## 备用章节\n\n补充办理内容。"),
        ("01基础/b.md", "# 基础说明 B\n\n## 基础章节\n\n基础资料。"),
        ("02业务/手册.md", "# 业务总手册\n\n正文。"),
    ]
    source = BytesIO()
    with ZipFile(source, "w") as archive:
        for name, content in files:
            archive.writestr(name, content)
    main = test_materials.stage(
        service, owner, manager, pid, aid, "documents", source.getvalue(), name="主手册", filename="source.zip"
    )
    appendix = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 附录说明\n\n## 附录章节\n\n独立包。", name="附录资料"
    )
    test_materials.activate(service, owner, manager, pid, aid, [main, appendix])
    links = InteractiveKnowledgeService(service)
    docs = {doc["name"]: doc for doc in links.catalog(owner, pid, aid)["documents"]}
    z_doc = links.document(owner, pid, docs["业务概览 Z"]["revision_id"], aid)
    deep = next(section for section in z_doc["sections"] if section["name"] == "深层节点")
    second_pid, second_aid = projects[1]
    extra = test_materials.stage(
        service, owner, manager, second_pid, second_aid, "documents", "# 新项目说明\n\n新项目资料。"
    )
    test_materials.activate(service, owner, manager, second_pid, second_aid, [extra])
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
                for _ in range(180):
                    assert process.poll() is None, (tmp_path / "vite.log").read_text()
                    try:
                        if httpx.get(url, timeout=0.4).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1600, "height": 1000})
                        errors, requests = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: requests.append(request.url))
                        page.goto(url + f"/#project={pid}&analysis={aid}&view=documents")
                        page.get_by_label("密码", exact=True).fill("owner")
                        page.get_by_role("button", name="打开交互工作台").click()
                        article = page.locator(".document-reader")
                        sidebar = page.locator(".doc-sidebar")
                        categories = page.get_by_role("navigation", name="文档分类", exact=True)
                        expect(article.get_by_role("heading", name="业务概览 Z", exact=True)).to_have_count(1)
                        expect(article).to_contain_text("开头介绍文字保留在正文")
                        expect(
                            sidebar.locator(".document-heading-tree").get_by_role(
                                "button", name="业务概览 Z", exact=True
                            )
                        ).to_have_count(0)
                        expect(categories.locator(".document-category-tabs > button")).to_have_text(
                            ["02业务3", "01基础1"]
                        )
                        expect(sidebar.locator(".document-open")).to_have_text(
                            ["业务概览 Z", "业务补充 A", "业务总手册"]
                        )
                        assert not any(docs["业务补充 A"]["revision_id"] in request for request in requests)
                        expect(sidebar).to_contain_text("办理步骤")
                        sidebar.get_by_role("button", name="展开下级章节 办理步骤", exact=True).click()
                        sidebar.get_by_role("button", name="深层节点", exact=True).click()
                        expect(article.locator(".chapter-selected")).to_have_attribute(
                            "id", "section-" + deep["revision_id"]
                        )
                        sidebar.get_by_role("button", name="展开章节 业务补充 A", exact=True).click()
                        expect(sidebar.get_by_role("button", name="备用章节", exact=True)).to_be_visible()
                        assert any(docs["业务补充 A"]["revision_id"] in request for request in requests)
                        sidebar.get_by_role("button", name="备用章节", exact=True).click()
                        expect(article.get_by_role("heading", name="业务补充 A", exact=True)).to_be_visible()
                        expect(article.locator(".chapter-selected")).to_contain_text("备用章节")
                        # Expanding the heading tree already fetched the full body;
                        # choosing it or another section must not request it again.
                        extra_revision = docs["业务补充 A"]["revision_id"]
                        assert sum("/documents/" + extra_revision in request for request in requests) == 1
                        assert sum("/documents/" + z_doc["revision_id"] in request for request in requests) == 1, [
                            request for request in requests if "/documents/" + z_doc["revision_id"] in request
                        ]
                        categories.get_by_role("button", name=re.compile(r"^01基础")).click()
                        expect(article.get_by_role("heading", name="基础说明 B", exact=True)).to_be_visible()
                        sidebar.get_by_label("搜索文档", exact=True).fill("业务补充")
                        result = sidebar.locator(".document-search-results button").filter(has_text="业务补充 A")
                        expect(result).to_be_visible()
                        result.click()
                        expect(article.get_by_role("heading", name="业务补充 A", exact=True)).to_be_visible()
                        expect(categories.get_by_role("button", name=re.compile(r"^02业务"))).to_have_attribute(
                            "aria-current", "page"
                        )
                        expect(sidebar.get_by_label("搜索文档", exact=True)).to_have_value("")
                        page.goto(
                            url
                            + f"/#project={pid}&analysis={aid}&view=documents&doc={z_doc['revision_id']}&section={deep['revision_id']}"
                        )
                        expect(sidebar.get_by_role("button", name="深层节点", exact=True)).to_be_visible()
                        expect(article.locator(".chapter-selected")).to_have_attribute(
                            "id", "section-" + deep["revision_id"]
                        )
                        fail_once = [True]

                        def temporary_failure(route):
                            if fail_once[0]:
                                fail_once[0] = False
                                route.fulfill(status=503, json={"error": {"message": "Temporary document failure"}})
                            else:
                                route.continue_()

                        page.route("**/interactive/documents/" + z_doc["revision_id"] + "?*", temporary_failure)
                        page.reload()
                        expect(article.get_by_role("alert")).to_contain_text("Temporary document failure")
                        expect(article).not_to_contain_text("选择一篇文档开始阅读")
                        article.get_by_role("button", name="重新加载文档", exact=True).click()
                        expect(article).to_contain_text("开头介绍文字保留在正文")
                        expect(sidebar).to_contain_text("办理步骤")
                        expect(sidebar.get_by_role("button", name="章节未能读取，重试", exact=True)).to_have_count(0)
                        page.unroute("**/interactive/documents/" + z_doc["revision_id"] + "?*", temporary_failure)
                        page.get_by_label("文档资料包", exact=True).select_option(label="附录资料")
                        expect(article.get_by_role("heading", name="附录说明", exact=True)).to_be_visible()
                        page.goto(url + f"/#project={second_pid}&analysis={second_aid}&view=documents")
                        expect(article.get_by_role("heading", name="新项目说明", exact=True)).to_be_visible()
                        assert "业务概览 Z" not in sidebar.inner_text()
                        assert not errors, errors
                    finally:
                        browser.close()
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
