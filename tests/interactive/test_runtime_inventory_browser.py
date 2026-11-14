"""Declared pages are mounted in a separate browser before AI association."""

import base64
import gzip
import json

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.bulk_association import BulkAssociationService, InventoryPageInput
from vks_api.models import uid

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def dynamic_page(name, delay=240):
    return f"""<!doctype html><html><head><title>{name}</title></head><body><div id="app"></div><script>
window.businessClicks=0;
setTimeout(()=>{{document.querySelector('#app').innerHTML=`<h1>{name}</h1><button id="add">新增收费项</button><label for="customer">客户名称</label><input id="customer" value="DO_NOT_SEND_TYPED_VALUE"><button id="open">打开弹窗</button><section class="modal" role="dialog" hidden><button id="unopened">隐藏状态保存</button></section>`;
document.querySelector('#app').insertAdjacentHTML('beforeend','<div class="el-loading-mask el-loading-fade-leave-active" style="opacity:0">Loading...</div>');
for(const button of document.querySelectorAll('button'))button.onclick=()=>{{window.businessClicks++;top.postMessage({{type:'fixture.business-clicked'}},'*')}};
}},{delay});
</script></body></html>"""


def charge_source(pages):
    packed = {
        "pages": {path: {"title": path, "html": raw} for path, raw in pages},
        "routeMap": {"/a": "a.html", "/b": "b.html"},
        "resources": [],
        "repoAssets": {},
    }
    data = base64.b64encode(gzip.compress(json.dumps(packed, ensure_ascii=False).encode())).decode()
    return (
        """<!doctype html><html><head><title>Synthetic charge</title><style>html,body,iframe{width:100%;height:100%;margin:0;border:0}</style></head><body><iframe id="actualPage"></iframe><script id="prototypePackPayload" type="application/octet-stream">"""
        + data
        + r"""</script><script>
(async()=>{const payload=document.getElementById('prototypePackPayload');const bytes=Uint8Array.from(atob(payload.textContent),c=>c.charCodeAt(0));const data=JSON.parse(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text());payload.remove();window.__prototypePack={show:path=>{if(!data.pages[path])return;const doc=new DOMParser().parseFromString(data.pages[path].html,'text/html');const meta=doc.createElement('script');meta.textContent='window.__prototypePackPage='+JSON.stringify({path});doc.head.prepend(meta);document.querySelector('#actualPage').srcdoc='<!doctype html>'+doc.documentElement.outerHTML}};window.__prototypePack.show('a.html')})();
</script></body></html>"""
    )


def setup(materials, monkeypatch, charge=False, partial=False):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    pages = [
        ("a.html", dynamic_page("账单 A")),
        ("b.html", '<title>Unready page</title><div id="app"></div>' if partial else dynamic_page("账单 B", 2800)),
    ]
    source = charge_source(pages) if charge else zip_docs(pages)
    prototype = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", source, filename="dynamic.html" if charge else "dynamic.zip"
    )
    docs = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 收费管理\n\n新增收费项需要客户名称。默认账单页面提供查询和维护，打开弹窗后填写收费信息。",
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype, docs])
    with service.db() as db:
        db.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    calls = []

    def model(_config, body):
        payload = json.loads(body["input"][1]["content"])
        calls.append(payload)
        return test_assistant.response(payload["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    return web, service, owner, pid, aid, calls


@pytest.mark.parametrize("charge", [False, True])
def test_two_dynamic_routes_discovered_before_clicks_and_ai_enqueue(materials, tmp_path, monkeypatch, charge):
    web, service, owner, pid, aid, calls = setup(materials, monkeypatch, charge=charge)
    bulk = BulkAssociationService(service)
    assert bulk.status(owner, pid, aid)["coverage"]["controls"] == 0
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.add_init_script(
            "window.businessClicks=0;window.addEventListener('message',e=>{if(e.data?.type==='fixture.business-clicked')window.businessClicks++});localStorage.setItem('vks.studio.auto-associate','off')"
        )
        uploads, errors = [], []
        page.on(
            "request",
            lambda request: (
                uploads.append(request.post_data_json)
                if request.method == "PUT" and request.url.endswith("/associations/inventory")
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="原型", exact=True).click()
            original = (
                page.frame_locator('iframe[title="原 HTML 原型"]')
                .frame_locator("#material")
                .frame_locator("#actualPage")
            )
            expect(original.get_by_role("heading", name="账单 A", exact=True)).to_be_visible(timeout=15000)
            original.get_by_label("客户名称", exact=True).fill("UNSAVED_CURRENT_FORM")
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="停止任务", exact=True)).to_be_visible()
            page.wait_for_function("!document.querySelector('[aria-label=\"动态原型预扫描\"]')", timeout=30000)
            expect(page.get_by_label("后台 AI 关联进度", exact=True)).to_be_visible()
            assert len(uploads) == 2
            assert {item["route"] for item in uploads} == {"/a.html", "/b.html"}
            assert all(item["status"] == "captured" and item["frame_path"] == ["actualPage"] for item in uploads)
            for item in uploads:
                assert {target["selector"] for target in item["targets"]} == {"body", "#add", "#customer", "#open"}
                assert (
                    next(target for target in item["targets"] if target["selector"] == "#customer")["label"]
                    == "客户名称"
                )
                assert all(target["open_steps"] == [] for target in item["targets"])
            assert "DO_NOT_SEND_TYPED_VALUE" not in json.dumps(uploads)
            assert "UNSAVED_CURRENT_FORM" not in json.dumps(uploads)
            assert page.evaluate("window.businessClicks") == 0
            assert not calls, "Synthetic worker is processed explicitly by this test"
            assert bulk.status(owner, pid, aid)["coverage"]["controls"] == 6
            jobs = bulk.status(owner, pid, aid)["jobs"]
            assert jobs and all(job["scan_pipeline"] for job in jobs)
            for job in jobs:
                service.process_job(job["id"])
                assert service.get_job(owner, job["id"])["state"] == "succeeded"
            assert calls
            page.get_by_role("button", name="原型", exact=True).click()
            expect(original.get_by_role("heading", name="账单 A", exact=True)).to_be_visible()
            expect(original.get_by_label("客户名称", exact=True)).to_have_value("UNSAVED_CURRENT_FORM")
            assert not errors, errors
        finally:
            browser.close()


def test_cancel_prescan_stops_queued_ai_and_next_run_reuses_captured_pages(materials, tmp_path, monkeypatch):
    web, service, owner, pid, aid, calls = setup(materials, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        uploads = []
        page.on(
            "request",
            lambda request: (
                uploads.append(request.post_data_json)
                if request.method == "PUT" and request.url.endswith("/associations/inventory")
                else None
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_label("页面采集并发", exact=True).select_option("1")
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.endswith("/associations/inventory")
                    and response.status == 200
                )
            ):
                page.get_by_role("button", name="补齐未关联", exact=True).click()
            page.get_by_role("button", name="停止任务", exact=True).click()
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
            expect(page.get_by_role("button", name="补齐未关联", exact=True)).to_be_enabled()
            assert all(
                job["state"] in {"cancelled", "cancelling"}
                for job in BulkAssociationService(service).status(owner, pid, aid)["jobs"]
            )
            assert not calls
            assert BulkAssociationService(service).inventory_status(owner, pid, aid)["pages_captured"] == 1
            page.get_by_role("button", name="补齐未关联", exact=True).click()
            expect(page.get_by_label("已保存的预扫描进度", exact=True)).to_contain_text(
                "已采集 2 / 2 页", timeout=20000
            )
            assert len(uploads) == 2
            assert len({item["route"] for item in uploads}) == 2, "Resume must keep the already captured page"
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
        finally:
            browser.close()


def test_navigation_keeps_scan_and_does_not_restart_completed_pages(materials, tmp_path, monkeypatch):
    web, service, owner, pid, aid, calls = setup(materials, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_label("页面采集并发", exact=True).select_option("1")
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.endswith("/associations/inventory")
                    and response.status == 200
                )
            ):
                page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            current = page.request.get(f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}").json()[
                "data"
            ]["run"]
            assert current["pages_completed"] == 1 and current["state"] == "running"
            first_jobs = BulkAssociationService(service).status(owner, pid, aid)["jobs"]
            assert first_jobs, "First captured page must queue AI while the next page is still mounting"
            for job in first_jobs:
                service.process_job(job["id"])
            assert calls, "Mocked AI must process captured content before the scan finishes"
            run_id = current["id"]
            page.get_by_role("button", name="文档", exact=True).click()
            expect(page.locator(".document-reader > h1")).to_have_text("收费管理")
            page.get_by_role("button", name="AI 设置", exact=True).click()
            page.get_by_role("button", name="关联管理", exact=True).click()
            expect(page.get_by_label("已保存的预扫描进度", exact=True)).to_contain_text(
                "已采集 2 / 2 页", timeout=20000
            )
            current = page.request.get(f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}").json()[
                "data"
            ]["run"]
            assert current["id"] == run_id and current["pages_completed"] == 2
            assert BulkAssociationService(service).status(owner, pid, aid)["jobs"]
            assert calls and all(call["control"]["route"] == "/a.html" for call in calls)
        finally:
            browser.close()


def test_refresh_keeps_persisted_progress_and_requires_explicit_resume(materials, tmp_path, monkeypatch):
    web, _service, _owner, pid, aid, calls = setup(materials, monkeypatch)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        batches = []
        page.on(
            "request",
            lambda request: (
                batches.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith("/associations/batch")
                else None
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_label("页面采集并发", exact=True).select_option("1")
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.endswith("/associations/inventory")
                    and response.status == 200
                )
            ):
                page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            previous = page.request.get(f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}").json()[
                "data"
            ]["run"]
            page.reload()
            progress = page.get_by_label("已保存的预扫描进度", exact=True)
            expect(progress).to_be_visible()
            expect(progress).to_contain_text("1 / 2 页")
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
            assert not batches and not calls
            page.get_by_role("button", name="恢复预扫描", exact=True).click()
            expect(page.get_by_label("已保存的预扫描进度", exact=True)).to_contain_text(
                "已采集 2 / 2 页", timeout=20000
            )
            current = page.request.get(f"{url}/api/v3/projects/{pid}/associations/scans?analysis_set_id={aid}").json()[
                "data"
            ]["run"]
            assert current["id"] == previous["id"] and current["pages_completed"] == 2
            assert not batches, "Pipeline pages enqueue fixed AI children without a second whole-run batch"
            page.reload()
            expect(page.get_by_label("后台 AI 关联进度", exact=True)).to_be_visible()
            assert not batches and not calls
        finally:
            browser.close()


def test_failed_page_preserves_partial_progress_while_captured_page_ai_runs(materials, tmp_path, monkeypatch):
    web, service, owner, pid, aid, calls = setup(materials, monkeypatch, partial=True)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            page.get_by_role("button", name="全量 AI 关联", exact=True).click()
            progress = page.get_by_label("已保存的预扫描进度", exact=True)
            expect(progress).to_contain_text("1 页待重试", timeout=20000)
            expect(progress).to_contain_text("预扫描部分完成")
            assert not calls
            expect(page.get_by_role("button", name="重试未完成页面", exact=True)).to_be_enabled()
            expect(page.get_by_role("button", name="仅关联已采集内容", exact=True)).to_have_count(0)
            expect(page.get_by_label("后台 AI 关联进度", exact=True)).to_be_visible()
            jobs = BulkAssociationService(service).status(owner, pid, aid)["jobs"]
            assert jobs and all(job["scan_pipeline"] for job in jobs)
            for job in jobs:
                service.process_job(job["id"])
            assert calls and all(call["control"]["route"] == "/a.html" for call in calls)
            expect(progress).to_contain_text("1 页待重试")
        finally:
            browser.close()


def test_legacy_inventory_progress_is_visible_without_inventing_a_scan_run(materials, tmp_path, monkeypatch):
    web, service, owner, pid, aid, calls = setup(materials, monkeypatch)
    bulk = BulkAssociationService(service)
    inventory = bulk.inventory_status(owner, pid, aid)
    package = inventory["packages"][0]
    page_info = package["pages"][0]
    target = {
        "route": page_info["route"],
        "frame_path": page_info["frame_path"],
        "selector": "body",
        "label": page_info["label"],
        "state": "",
        "open_steps": [],
    }
    bulk.save_inventory(
        owner,
        pid,
        InventoryPageInput(
            analysis_set_id=aid,
            package_id=package["package_id"],
            artifact_sha256=package["sha256"],
            composition=inventory["composition"],
            route=page_info["route"],
            frame_path=page_info["frame_path"],
            targets=[target, {**target, "selector": "#add", "label": "新增收费项"}],
            observed=2,
            truncated=0,
            status="captured",
        ),
        str(inventory["selection_version"]),
        uid(),
    )
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            progress = page.get_by_label("已保存的页面清单", exact=True)
            expect(progress).to_contain_text("已采集 1 / 2 页 · 1 控件")
            expect(page.get_by_role("button", name="补齐未关联", exact=True)).to_be_enabled()
            expect(page.locator('iframe[title="预扫描原型"]')).to_have_count(0)
            assert not bulk.status(owner, pid, aid)["jobs"] and not calls
        finally:
            browser.close()
