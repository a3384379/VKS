"""Prove actual isolated-page overlap and ACK-based capture, not just a UI lane label."""

import json
import time

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.models import Job, uid
from vks_api.scan_pipeline import reconcile

from tests.interactive import test_assistant, test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.interactive.test_runtime_inventory_browser import charge_source, dynamic_page
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials

PROBE = """if(window===window.top){window.scanStarts=[];const send=MessagePort.prototype.postMessage;MessagePort.prototype.postMessage=function(message,...args){if(message?.type==='inventory.scan')window.scanStarts.push({id:message.runtime_instance_id,route:message.payload.route,time:Date.now()});return Reflect.apply(send,this,[message,...args])}}"""


def test_three_independent_lanes_overlap_and_match_serial_results(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    pages = [(name + ".html", dynamic_page("账单 " + name.upper(), 1500)) for name in ("a", "b", "c")]
    prototype = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", charge_source(pages), filename="parallel.html"
    )
    docs = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 收费管理\n\n新增收费项需要客户名称。账单提供默认操作。"
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
        data = json.loads(body["input"][1]["content"])
        calls.append(data)
        return test_assistant.response(data["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(PROBE)
        captures, elapsed, targets, starts = [], {}, {}, {}
        page.on(
            "request",
            lambda request: (
                captures.append((time.time() * 1000, request.post_data_json))
                if request.method == "PUT" and request.url.endswith("/associations/inventory")
                else None
            ),
        )
        try:
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            for lanes in (1, 3):
                captures.clear()
                page.evaluate("window.scanStarts=[]")
                page.get_by_label("页面采集并发", exact=True).select_option(str(lanes))
                began = time.monotonic()
                page.get_by_role("button", name="全量 AI 关联", exact=True).click()
                expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
                expect(page.get_by_label("已保存的预扫描进度", exact=True)).to_contain_text(
                    "已采集 3 / 3 页", timeout=30000
                )
                elapsed[lanes] = time.monotonic() - began
                starts[lanes] = page.evaluate("window.scanStarts")
                assert len(captures) == 3 and all(body["status"] == "captured" for _, body in captures)
                targets[lanes] = {
                    (target["route"], target["selector"], tuple(target["frame_path"]))
                    for _, body in captures
                    for target in body["targets"]
                }
                if lanes == 3:
                    assert len({item["id"] for item in starts[3]}) == 3
                    assert len(starts[3]) == 3, "A global queue must never hand the same page to two workers"
                    assert max(item["time"] for item in starts[3]) < min(when for when, _ in captures), (
                        "All three capture commands must overlap before the first page can be persisted"
                    )
                else:
                    assert len({item["id"] for item in starts[1]}) == 1, "One runtime is reused across a source's pages"
                with service.db() as db:
                    jobs = [
                        job.id
                        for job in db.scalars(
                            select(Job).where(Job.project_id == pid, Job.kind == "associate_all", Job.state == "queued")
                        )
                    ]
                assert jobs
                for job_id in jobs:
                    service.process_job(job_id)
                reconcile(service)
                # Refresh is explicit and does not queue duplicate page jobs.
                page.reload()
                expect(page.get_by_role("button", name="全量 AI 关联", exact=True)).to_be_enabled(timeout=15000)
            assert targets[1] == targets[3] and len(targets[3]) == 12
            assert elapsed[3] < elapsed[1] * 0.82, elapsed
            print(
                f"capture_elapsed_seconds serial={elapsed[1]:.3f} parallel3={elapsed[3]:.3f} ratio={elapsed[3] / elapsed[1]:.3f}"
            )
            assert calls, "Only the local mocked model was used"
        finally:
            browser.close()
