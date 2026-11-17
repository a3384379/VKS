"""Stable inventory avoids per-control state scans and ignores unrelated clocks."""

import json
import time

from playwright.sync_api import expect, sync_playwright
from vks_api.bulk_association import BulkAssociationService

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials

PROBE = """window.inventoryProbes=[];window.addEventListener('message',event=>{if(event.data?.type==='fixture.inventory-probe')window.inventoryProbes.push(event.data)})"""
COUNTERS = """const query=document.querySelectorAll.bind(document);let stateQueries=0;document.querySelectorAll=function(selector){if(selector.startsWith('[role="dialog"],'))stateQueries++;return query(selector)};setInterval(()=>top.postMessage({type:'fixture.inventory-probe',stateQueries},'*'),40);"""


def capture(materials, tmp_path, monkeypatch, source, *, visible_worker=False):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(service, owner, manager, pid, aid, "prototype", source, filename="page.html")
    test_materials.activate(service, owner, manager, pid, aid, [prototype])
    uploads = []
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(PROBE)
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
            if visible_worker:
                # Chromium can pause CSS animations in completely offscreen
                # frames. Paint this fixture to test a real CSS-only change.
                page.add_style_tag(content="[data-scan-worker]{left:0!important;opacity:1!important}")
            # A scan-only project needs no AI configuration or provider request.
            before = time.monotonic()
            page.get_by_role("button", name="扫描页面变更", exact=True).click()
            expect(page.get_by_label("动态原型预扫描", exact=True)).to_be_visible()
            page.wait_for_function("!document.querySelector('[aria-label=\"动态原型预扫描\"]')", timeout=20000)
            assert uploads
            elapsed = time.monotonic() - before
            probes = page.evaluate("window.inventoryProbes")
            return uploads, probes, elapsed, BulkAssociationService(service).inventory_status(owner, pid, aid)
        finally:
            browser.close()


def test_dense_static_page_calculates_dialog_state_once_per_observation(materials, tmp_path, monkeypatch):
    controls = "".join(f'<button id="action-{i}">业务动作 {i}</button>' for i in range(400))
    source = f'<title>密集模块</title><h1>密集模块</h1><section role="dialog" id="panel">{controls}</section><script>{COUNTERS}</script>'
    uploads, probes, _, inventory = capture(materials, tmp_path, monkeypatch, source)
    assert inventory["pages_captured"] == 1
    assert len(uploads) == 1 and uploads[0]["status"] == "captured"
    targets = uploads[0]["targets"]
    assert len(targets) == 401 and {t["selector"] for t in targets} == {"body"} | {f"#action-{i}" for i in range(400)}
    assert all(t["state"] == "#panel" for t in targets if t["selector"] != "body")
    assert targets[0]["state"] == "" and all(t["open_steps"] == [] for t in targets)
    # Counts measure actual DOM queries in the isolated source, not an inferred
    # implementation counter. The prior implementation queried per control on
    # every 100 ms pass (thousands of identical whole-document state searches).
    assert probes and max(p["stateQueries"] for p in probes) <= 6, probes


def test_unrelated_clock_does_not_starve_but_late_control_still_joins_capture(materials, tmp_path, monkeypatch):
    source = """<title>动态模块</title><div id="app"></div><script>""" + COUNTERS + """
setTimeout(()=>{document.querySelector('#app').innerHTML='<h1>动态模块</h1><span id="clock">0</span><button id="first">已有操作</button><section role="dialog" id="dialog" hidden><button id="late">延迟操作</button></section>';let n=0;setInterval(()=>document.querySelector('#clock').textContent=String(++n),40);setTimeout(()=>document.querySelector('#dialog').hidden=false,300)},250);
</script>"""
    uploads, probes, elapsed, inventory = capture(materials, tmp_path, monkeypatch, source)
    assert inventory["pages_captured"] == 1, json.dumps(uploads, ensure_ascii=False)
    assert len(uploads) == 1 and uploads[0]["status"] == "captured"
    targets = {t["selector"]: t for t in uploads[0]["targets"]}
    assert set(targets) == {"body", "#first", "#late"}
    assert targets["#late"]["label"] == "延迟操作" and targets["#late"]["state"] == "#dialog"
    assert targets["#first"]["state"] == "#dialog" and probes
    assert elapsed < 6, elapsed


def test_css_only_visibility_change_invalidates_cached_targets(materials, tmp_path, monkeypatch):
    source = """<title>动画模块</title><style>
@keyframes reveal {from{visibility:hidden}to{visibility:visible}}
#late{animation:reveal 450ms step-end both}
</style><h1>动画模块</h1><button id="first">已有操作</button><button id="late">延迟操作</button>"""
    uploads, _, _, inventory = capture(materials, tmp_path, monkeypatch, source, visible_worker=True)
    assert inventory["pages_captured"] == 1
    assert len(uploads) == 1 and uploads[0]["status"] == "captured"
    assert {t["selector"] for t in uploads[0]["targets"]} == {"body", "#first", "#late"}


def test_rendered_dialog_outside_empty_mount_root_is_captured(materials, tmp_path, monkeypatch):
    source = """<title>独立组件</title><div id="app"></div><script>
setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<section role="dialog" id="portal"><h2>客户账单接收人</h2><button id="confirm">确定</button></section>'),1200);
</script>"""
    uploads, _, elapsed, inventory = capture(materials, tmp_path, monkeypatch, source)
    assert inventory["pages_captured"] == 1, uploads
    assert len(uploads) == 1 and uploads[0]["status"] == "captured"
    targets = {t["selector"]: t for t in uploads[0]["targets"]}
    assert set(targets) == {"body", "#confirm"}
    assert targets["#confirm"]["state"] == "#portal" and elapsed < 6


def test_surrounding_toolbar_does_not_make_an_empty_mount_root_ready(materials, tmp_path, monkeypatch):
    source = """<title>延迟主模块</title><button id="toolbar">外围工具</button><div id="app"></div><script>
setTimeout(()=>document.querySelector('#app').innerHTML='<button id="main">模块操作</button>',2800);
</script>"""
    uploads, _, elapsed, inventory = capture(materials, tmp_path, monkeypatch, source)
    assert inventory["pages_captured"] == 1
    assert {t["selector"] for t in uploads[0]["targets"]} == {"body", "#toolbar", "#main"}
    assert elapsed >= 2.8, "An unrelated visible toolbar is not evidence that the main app has mounted"
