"""Real Studio browser regression for native documents and their independent state."""

import asyncio
import json
import os
import re
import socket
import subprocess
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
from playwright.sync_api import expect, sync_playwright
from vks_api.interactive import InteractiveKnowledgeService

from apps.runtime.server import RuntimeHandler
from tests.interactive import test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.ui.test_frontend_browser import chromium_path, live_api

ROOT = Path(__file__).resolve().parents[2]
materials = test_materials.materials
DOCUMENT_KEY = "DOCUMENT_LOCAL_MARKER_KEY"
PLATFORM_KEY = "PLATFORM_CREDENTIAL_MUST_STAY_SERVER"
NATIVE_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Original handbook UI</title>
<style>body{margin:0;font:16px system-ui}#cover{padding:32px;background:rgb(17,34,51);color:white;min-height:400px}label{display:block;margin:12px 0}button{margin:4px}article{padding:32px}#first{margin-top:750px}.gap{height:800px}#tail{height:1000px}</style>
</head><body><div id="cover"><b>Original entry home</b><nav aria-label="Original directory"><a href="#first">First article</a> <a href="#exact-rule">Exact rule</a></nav>
<label>Original search<input aria-label="Original search"></label><button id="search">Search original</button><output id="search-output"></output>
<label>Original note<input aria-label="Original note"></label><button id="action">Native action</button><output id="action-output">0</output><output id="native-context"></output>
<label>Original API Key<input aria-label="Original API Key"></label><button id="save-key">Save original settings</button><output id="storage-status"></output><output id="idb-output"></output>
<form id="ai-form"><button id="ask" type="submit">Ask original AI</button></form><output id="answer"></output><output id="submit-count">0</output></div>
<article><h1 id="first">Native handbook</h1><p>Original introduction.</p><div class="gap"></div><h2 id="exact-rule">Exact rule</h2><p>Exact original rule text.</p><div class="gap"></div><h2>No ID rule</h2><p>Accurate structural location without an ID.</p><div id="tail"></div></article>
<script>
const keyField=document.querySelector('[aria-label="Original API Key"]');keyField.value=localStorage.getItem('native-marker-key')||'';
const opened=indexedDB.open('original-handbook',1);let db;opened.onupgradeneeded=()=>opened.result.createObjectStore('settings');
opened.onsuccess=()=>{db=opened.result;const read=db.transaction('settings').objectStore('settings').get('marker');read.onsuccess=()=>document.querySelector('#idb-output').textContent=read.result||'empty'};
document.querySelector('#save-key').onclick=()=>{localStorage.setItem('native-marker-key',keyField.value);const put=db.transaction('settings','readwrite').objectStore('settings').put(keyField.value,'marker');put.onsuccess=()=>{document.querySelector('#storage-status').textContent='Original settings saved';document.querySelector('#idb-output').textContent=keyField.value}};
document.querySelector('#search').onclick=()=>document.querySelector('#search-output').textContent='Original result: '+document.querySelector('[aria-label="Original search"]').value;
let count=0;document.querySelector('#action').onclick=()=>document.querySelector('#action-output').textContent=String(++count);
document.querySelector('#action').addEventListener('contextmenu',event=>{event.preventDefault();document.querySelector('#native-context').textContent='Original right click'});
document.querySelector('#ai-form').onsubmit=async event=>{event.preventDefault();document.querySelector('#submit-count').textContent=String(Number(document.querySelector('#submit-count').textContent)+1);try{const r=await fetch('https://api.openai.com/v1/models',{headers:{Authorization:'Bearer '+keyField.value},credentials:'omit'});const data=await r.json();document.querySelector('#answer').textContent='Original AI: '+data.data[0].id}catch(error){document.querySelector('#answer').textContent='ERROR '+error.message}};
</script></body></html>"""


@contextmanager
def native_studio(app, tmp_path, monkeypatch):
    """Separate API, Vite cache, runtime policy key, and browser origin per test."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("VKS_STUDIO_ALLOWED_ORIGINS", url)
    monkeypatch.setenv("VKS_RUNTIME_DATA_DIR", str(tmp_path / "runtime-state"))
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    thread = Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    process = None
    script = """import {createServer} from 'vite';const s=await createServer({root:process.argv[1]+'/apps/studio',configFile:process.argv[1]+'/apps/studio/vite.config.ts',cacheDir:process.argv[2],define:{'import.meta.env.VITE_RUNTIME_PORT':JSON.stringify(process.argv[5])},server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,proxy:{'/api':{target:process.argv[4],changeOrigin:false}}}});await s.listen();"""
    try:
        with live_api(app) as api, (tmp_path / "vite.log").open("wb") as log:
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
            for _ in range(180):
                assert process.poll() is None, (tmp_path / "vite.log").read_text()
                try:
                    if httpx.get(url, timeout=0.4).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Studio did not start")
            yield url
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
        runtime.shutdown()
        runtime.server_close()
        thread.join(timeout=5)


def login(page, url, pid, aid, extra=""):
    page.goto(url + f"/#project={pid}&analysis={aid}&view=documents" + extra)
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开交互工作台", exact=True).click()


def source_frame(page):
    return page.frame_locator('iframe[title="原 HTML 手册"]').frame_locator("#material")


def test_native_handbook_home_controls_fake_ai_storage_reader_switch_and_exact_links(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    monkeypatch.setenv("VKS_MODEL_API_KEY", PLATFORM_KEY)
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", NATIVE_HTML, filename="handbook.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    shared = InteractiveKnowledgeService(service)
    entry = shared.catalog(owner, pid, aid)["documents"][0]
    document = shared.document(owner, pid, entry["revision_id"], aid)
    exact = next(section for section in document["sections"] if section["name"] == "Exact rule")
    no_id = next(section for section in document["sections"] if section["name"] == "No ID rule")
    no_id_location = next(
        location for location in document["native"]["sections"] if location["revision_id"] == no_id["revision_id"]
    )
    assert no_id_location["anchor"] == "" and no_id_location["selector"]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        model_requests, errors = [], []
        context.route("https://**/*", lambda route: route.abort("blockedbyclient"))

        def fake_models(route):
            if route.request.method != "OPTIONS":
                model_requests.append(route.request.headers)
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Authorization",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                },
                body=json.dumps({"data": [{"id": "synthetic-original-model"}]}),
            )

        context.route("https://api.openai.com/v1/models", fake_models)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            source = source_frame(page)
            expect(source.get_by_text("Original entry home", exact=True)).to_be_in_viewport(timeout=30000)
            expect(source.locator("#first")).not_to_be_in_viewport()
            assert (
                source.locator("#cover").evaluate("node=>getComputedStyle(node).backgroundColor") == "rgb(17, 34, 51)"
            )
            expect(page.locator(".doc-sidebar")).not_to_be_visible()
            assert "doc=" not in page.url
            source.get_by_role("navigation", name="Original directory").get_by_role(
                "link", name="Exact rule", exact=True
            ).click()
            expect(page).to_have_url(re.compile(r"(?:[#&])doc=" + re.escape(document["revision_id"]) + r"(?:&|$)"))
            expect(page).to_have_url(re.compile(r"(?:[#&])section=" + re.escape(exact["revision_id"]) + r"(?:&|$)"))
            page.get_by_role("button", name="标准阅读", exact=True).click()
            expect(page.locator(".document-reader .chapter-selected")).to_have_attribute(
                "id", "section-" + exact["revision_id"]
            )
            page.get_by_role("button", name="原样文档", exact=True).click()
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_in_viewport()
            source.get_by_label("Original search", exact=True).fill("Own directory search")
            source.get_by_role("button", name="Search original", exact=True).click()
            expect(source.locator("#search-output")).to_have_text("Original result: Own directory search")
            source.get_by_role("button", name="Native action", exact=True).click()
            expect(source.locator("#action-output")).to_have_text("1")
            source.get_by_role("button", name="Native action", exact=True).click(button="right")
            expect(source.locator("#native-context")).to_have_text("Original right click")
            expect(page.get_by_role("menu", name="控件知识菜单")).not_to_be_visible()
            source.get_by_label("Original API Key", exact=True).fill(DOCUMENT_KEY)
            source.get_by_role("button", name="Save original settings", exact=True).click()
            expect(source.locator("#idb-output")).to_have_text(DOCUMENT_KEY)
            source.get_by_role("button", name="Ask original AI", exact=True).click()
            expect(source.locator("#answer")).to_have_text("Original AI: synthetic-original-model")
            expect(source.locator("#submit-count")).to_have_text("1")
            assert len(model_requests) == 1 and model_requests[0]["authorization"] == "Bearer " + DOCUMENT_KEY
            source.get_by_label("Original note", exact=True).fill("Keep native form state")
            original_frame = source.locator("body").evaluate(
                "()=>{window.instanceMarker=crypto.randomUUID();return window.instanceMarker}"
            )
            page.get_by_role("button", name="标准阅读", exact=True).click()
            expect(page.locator(".document-reader")).to_be_visible()
            page.get_by_role("button", name="原样文档", exact=True).click()
            expect(source.get_by_label("Original note", exact=True)).to_have_value("Keep native form state")
            assert source.locator("body").evaluate("()=>window.instanceMarker") == original_frame
            page.reload()
            expect(source.get_by_label("Original API Key", exact=True)).to_have_value(DOCUMENT_KEY, timeout=30000)
            expect(source.locator("#idb-output")).to_have_text(DOCUMENT_KEY)
            runtime_storage = source.locator("body").evaluate(
                "()=>({local:Object.entries(localStorage),session:Object.entries(sessionStorage),html:document.documentElement.outerHTML})"
            )
            assert PLATFORM_KEY not in json.dumps(runtime_storage)
            page.get_by_role("button", name="标准阅读", exact=True).click()
            sidebar = page.locator(".doc-sidebar")
            sidebar.get_by_role("button", name="Exact rule", exact=True).click()
            expect(page.locator(".document-reader .chapter-selected")).to_contain_text("Exact rule")
            page.get_by_role("button", name="原样文档", exact=True).click()
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_in_viewport()
            external = f"project={pid}&analysis={aid}&view=documents&doc={document['revision_id']}&section={no_id['revision_id']}"
            page.evaluate("hash=>{location.hash=hash}", external)
            expect(source.get_by_role("heading", name="No ID rule", exact=True)).to_be_in_viewport()
            assert source.get_by_role("heading", name="No ID rule", exact=True).evaluate(
                "node=>Math.abs(node.getBoundingClientRect().top)<80"
            )
            assert "reader=" not in page.url
            external = f"project={pid}&analysis={aid}&view=documents&doc={document['revision_id']}&section={exact['revision_id']}"
            page.goto(url + "/#" + external)
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_in_viewport(timeout=30000)
            assert not errors, errors
        finally:
            context.close()
            browser.close()


def test_modified_index_defaults_standard_with_source_notice_and_markdown_stays_standard(
    materials, tmp_path, monkeypatch
):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", NATIVE_HTML, filename="handbook.html"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    shared = InteractiveKnowledgeService(service)
    entry = shared.catalog(owner, pid, aid)["documents"][0]
    document = shared.document(owner, pid, entry["revision_id"], aid)
    service.save_document(
        owner,
        entry["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": entry["revision_id"],
            "markdown": document["content"].replace("Exact original rule text.", "Latest authored rule text."),
            "locked_section_ids": [],
        },
        "1",
    )
    modified = shared.catalog(owner, pid, aid)["documents"][0]
    other_pid, other_aid = projects[1]
    markdown = test_materials.stage(
        service,
        owner,
        manager,
        other_pid,
        other_aid,
        "documents",
        "# Plain Markdown guide\n\nOnly the standard reader is appropriate.",
    )
    test_materials.activate(service, owner, manager, other_pid, other_aid, [markdown])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.route("https://**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        try:
            login(page, url, pid, aid)
            expect(page).to_have_url(re.compile(r"(?:[#&])doc=" + re.escape(modified["revision_id"]) + r"(?:&|$)"))
            expect(page.locator(".document-reader")).to_contain_text("Latest authored rule text.")
            expect(page.locator(".native-document-space")).not_to_be_visible()
            expect(page.locator(".native-source-notice")).to_contain_text("工作区正文已有修改")
            page.get_by_role("button", name="查看导入原件", exact=True).click()
            source = source_frame(page)
            expect(source.locator("body")).to_contain_text("Exact original rule text.", timeout=30000)
            expect(source.locator("body")).not_to_contain_text("Latest authored rule text.")
            expect(page.locator(".native-source-notice")).to_contain_text("正在查看导入原件")
            page.goto(url + f"/#project={other_pid}&analysis={other_aid}&view=documents")
            expect(page.locator(".document-reader")).to_contain_text("Plain Markdown guide")
            expect(page.locator(".native-document-space")).not_to_be_visible()
            expect(page.get_by_role("button", name="原样文档", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="文档设置", exact=True)).to_have_count(0)
        finally:
            context.close()
            browser.close()


def test_native_site_links_preserve_cross_page_and_same_page_hash_routes(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    index = '<!doctype html><h1>Start</h1><a href="nested/other.html#rule">Cross page rule</a>'
    other = """<!doctype html><h1>Other</h1><p id="active">unrouted</p><output id="count">0</output>
<label>Keep site state<input aria-label="Keep site state"></label>
<a href="other.html#second">Same page second</a><a href="other.html#rule">Same page rule</a>
<section id="rule" hidden><h2>Rule</h2></section><section id="second" hidden><h2>Second rule</h2></section>
<script>let count=0;function route(){document.getElementById('active').textContent=location.hash;document.getElementById('rule').hidden=location.hash!=='#rule';document.getElementById('second').hidden=location.hash!=='#second';document.getElementById('count').textContent=String(++count)}window.addEventListener('hashchange',route);route()</script>"""
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs([("index.html", index), ("nested/other.html", other)]),
        filename="routed-site.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.route("https://**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        try:
            login(page, url, pid, aid)
            site = source_frame(page).frame_locator("#actualPage")
            site.get_by_role("link", name="Cross page rule", exact=True).click()
            expect(site.locator("#active")).to_have_text("#rule")
            expect(site.locator("#rule")).to_be_visible()
            assert site.locator("body").evaluate("()=>location.hash") == "#rule"
            site.get_by_label("Keep site state", exact=True).fill("Same frame state survives route")
            site.get_by_role("link", name="Same page second", exact=True).click()
            expect(site.locator("#active")).to_have_text("#second")
            expect(site.locator("#second")).to_be_visible()
            expect(site.locator("#rule")).not_to_be_visible()
            expect(site.get_by_label("Keep site state", exact=True)).to_have_value("Same frame state survives route")
            site.get_by_role("link", name="Same page rule", exact=True).click()
            expect(site.locator("#active")).to_have_text("#rule")
            expect(site.locator("#rule")).to_be_visible()
            count = int(site.locator("#count").inner_text())
            site.get_by_role("link", name="Same page rule", exact=True).click()
            expect(site.locator("#count")).to_have_text(str(count + 1))
            expect(site.get_by_label("Keep site state", exact=True)).to_have_value("Same frame state survives route")
        finally:
            context.close()
            browser.close()


def test_native_location_failure_cannot_replace_a_new_pending_document_request(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        zip_docs(
            [
                ("index.html", '<!doctype html><h1 id="start">Start</h1><h2 id="bad">Folded rule</h2>'),
                ("next.html", '<!doctype html><h1 id="next">Next</h1><h2 id="good">Next rule</h2>'),
            ]
        ),
        filename="pending-navigation.zip",
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    shared = InteractiveKnowledgeService(service)
    documents = {
        row["name"]: shared.document(owner, pid, row["revision_id"], aid)
        for row in shared.catalog(owner, pid, aid)["documents"]
    }
    first, second = documents["Start"], documents["Next"]
    first_section = next(section for section in first["sections"] if section["name"] == "Folded rule")
    second_section = next(section for section in second["sections"] if section["name"] == "Next rule")
    details_started, release_details = Event(), Event()
    delayed_path = f"/api/v3/projects/{pid}/interactive/documents/{second['revision_id']}"

    async def delayed_app(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == delayed_path:
            details_started.set()
            await asyncio.to_thread(release_details.wait, 15)
        await web.app(scope, receive, send)

    with native_studio(delayed_app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.route("https://**/*", lambda route: route.abort("blockedbyclient"))
        page = context.new_page()
        try:
            login(page, url, pid, aid)
            runtime = page.frame_locator('iframe[title="原 HTML 手册"]')
            source = source_frame(page).frame_locator("#actualPage")
            expect(source.get_by_role("heading", name="Start", exact=True)).to_be_visible(timeout=30000)
            # Observe the public result protocol so the old failure is delivered
            # before the newer document response is released.
            runtime.locator("body").evaluate(
                """() => {
                  const send = MessagePort.prototype.postMessage;
                  document.body.dataset.auditFailures = '0';
                  MessagePort.prototype.postMessage = function(message, ...transfer) {
                    if (message?.type === 'command.result' && message.payload?.ok === false)
                      document.body.dataset.auditFailures = String(Number(document.body.dataset.auditFailures) + 1);
                    return send.call(this, message, ...transfer);
                  };
                }"""
            )
            # A folded original chapter fails after its own two animation
            # frames. Hold those frames to reproduce a slower native page.
            source.locator("body").evaluate(
                """() => {
                  document.getElementById('bad').hidden = true;
                  window.auditFrames = [];
                  window.requestAnimationFrame = callback => window.auditFrames.push(callback);
                }"""
            )
            base = f"project={pid}&analysis={aid}&view=documents"
            page.evaluate(
                "hash => { location.hash = hash; }",
                base + f"&doc={first['revision_id']}&section={first_section['revision_id']}",
            )
            source.locator("body").evaluate(
                """() => new Promise((resolve, reject) => {
                  const deadline = setTimeout(() => reject(Error('Original reveal did not start')), 5000);
                  const check = () => {
                    if (window.auditFrames.length) { clearTimeout(deadline); resolve(); }
                    else setTimeout(check, 10);
                  };
                  check();
                })"""
            )
            page.evaluate(
                "hash => { location.hash = hash; }",
                base + f"&doc={second['revision_id']}&section={second_section['revision_id']}",
            )
            assert details_started.wait(timeout=5), "The new exact document was not requested"
            source.locator("body").evaluate(
                """() => {
                  for (let frame = 0; frame < 2; frame++) {
                    const callback = window.auditFrames.shift();
                    if (!callback) throw Error('Missing pending original animation frame');
                    callback(performance.now());
                  }
                }"""
            )
            expect(runtime.locator("body")).to_have_attribute("data-audit-failures", "1")
            page.wait_for_timeout(100)  # Deliver the already-observed MessagePort result.
            assert page.evaluate("new URLSearchParams(location.hash.slice(1)).get('reader')") != "standard"
            expect(page.locator(".native-document-space")).to_be_visible()
            expect(page.locator(".toast")).not_to_be_visible()
            release_details.set()
            expect(source.get_by_role("heading", name="Next rule", exact=True)).to_be_in_viewport(timeout=30000)
            expect(page).to_have_url(
                re.compile(r"(?:[#&])section=" + re.escape(second_section["revision_id"]) + r"(?:&|$)")
            )
            assert page.evaluate("new URLSearchParams(location.hash.slice(1)).get('reader')") != "standard"
        finally:
            release_details.set()
            context.close()
            browser.close()
