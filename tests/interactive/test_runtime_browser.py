"""Native document code runs only in a registered, credential-free browser origin."""

import json
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.material_site import site_html

from apps.runtime.server import RuntimeHandler
from tests.ui.test_frontend_browser import chromium_path

HARNESS = b"""<!doctype html><iframe id="runtime" sandbox="allow-scripts allow-same-origin allow-modals allow-downloads" style="width:100%;height:700px"></iframe><script>
window.events=[];
window.openMaterial=async options=>{
 const response=await fetch(options.runtime+'/register',{method:'POST',credentials:'omit',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:options.mode,digest:options.digest,storageScope:options.scope,networkOrigins:options.origins||[]})});
 const registration=await response.json(); if(!response.ok)throw Error(registration.error);
 window.registration=registration; window.events=[]; window.channel?.close();
 const iframe=document.getElementById('runtime'),instance=crypto.randomUUID();
 iframe.setAttribute('sandbox','allow-scripts allow-same-origin allow-modals allow-downloads'+(options.mode==='document'?' allow-forms':''));
 return new Promise(resolve=>{
  const listener=event=>{
   if(event.source!==iframe.contentWindow||event.origin!==registration.origin||event.data?.type!=='vks.runtime.ready'||event.data.instance!==instance)return;
   window.removeEventListener('message',listener);
   const channel=new MessageChannel();window.channel=channel.port1;
   channel.port1.onmessage=({data})=>{
    events.push(data);
    if(data.type==='runtime.ready'){
     const raw=new TextEncoder().encode(options.material).buffer;
     channel.port1.postMessage({protocol_version:1,runtime_instance_id:instance,type:'material.load',payload:{bytes:raw}},[raw]);
    }
    if(data.type==='material.loaded')resolve(registration.origin);
   };
   window.command=(type,payload)=>channel.port1.postMessage({protocol_version:1,runtime_instance_id:instance,request_id:crypto.randomUUID(),type,payload});
   iframe.contentWindow.postMessage({type:'vks.connect',instance},registration.origin,[channel.port2]);
  };
  window.addEventListener('message',listener);
  iframe.src=registration.origin+'/?registration='+registration.registration+'&instance='+instance;
 });
};
</script>"""


@pytest.fixture
def runtime_sites(tmp_path, monkeypatch):
    class Harness(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/"):
                runtime.platform_api_requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HARNESS)

        def log_message(self, *args):
            pass

    host = ThreadingHTTPServer(("127.0.0.1", 0), Harness)
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    runtime.platform_api_requests = []
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (host, runtime)]
    for thread in threads:
        thread.start()
    host_url = f"http://127.0.0.1:{host.server_port}"
    monkeypatch.setenv("VKS_STUDIO_ALLOWED_ORIGINS", host_url + ",https://studio.example.com")
    monkeypatch.setenv("VKS_RUNTIME_DATA_DIR", str(tmp_path / "runtime"))
    try:
        yield host_url, runtime, tmp_path
    finally:
        for server, thread in zip((host, runtime), threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def registration_options(runtime, material, **changes):
    return {
        "runtime": f"http://localhost:{runtime.server_port}",
        "material": material,
        "mode": "document",
        "scope": {"accountId": "owner", "projectId": "project-a", "packageId": "book"},
        "digest": sha256(material.encode()).hexdigest(),
        "origins": [],
        **changes,
    }


def register(client, host_url, runtime, *, mode="document", origins=None, scope=None):
    return client.post(
        f"http://127.0.0.1:{runtime.server_port}/register",
        headers={"Origin": host_url, "Host": f"localhost:{runtime.server_port}"},
        json={
            "mode": mode,
            "digest": "a" * 64,
            "storageScope": scope or {"accountId": "owner", "projectId": "project", "packageId": "book"},
            "networkOrigins": origins or [],
        },
    )


def test_only_trusted_json_registration_can_define_an_exact_network_policy(runtime_sites):
    host_url, runtime, tmp_path = runtime_sites
    url = f"http://127.0.0.1:{runtime.server_port}"
    with httpx.Client(trust_env=False) as client:
        assert register(client, "https://attacker.example.com", runtime).status_code == 403
        assert client.get(url + "/register", headers={"Host": f"localhost:{runtime.server_port}"}).status_code == 404
        assert (
            client.post(
                url + "/register",
                headers={"Host": f"localhost:{runtime.server_port}", "Origin": host_url},
                content='{"mode":"document"}',
            ).status_code
            == 415
        )
        for origin in [
            "https://*",
            "https://127.0.0.1",
            "https://10.0.0.1",
            "https://[::1]",
            "https://api.localhost",
            "https://169.254.169.254",
            "https://0x7f.1",
            "https://model.internal",
            "https://api.example.com/v1",
            "https://user:password@api.example.com",
            "https://api.example.com?network=*",
            "https://studio.example.com",
            "https://api.example.com; connect-src *",
        ]:
            assert register(client, host_url, runtime, origins=[origin]).status_code == 400, origin
        doc = register(client, host_url, runtime, origins=["https://api.example.com"]).json()
        prototype = register(client, host_url, runtime, mode="prototype").json()
        assert doc["origin"] != prototype["origin"]
        assert (
            register(client, host_url, runtime, mode="prototype", origins=["https://api.example.com"]).status_code
            == 400
        )
        for record, expected in [(doc, "connect-src https://api.example.com"), (prototype, "connect-src 'none'")]:
            name = urlsplit(record["origin"]).netloc
            response = client.get(
                url + "/?registration=" + record["registration"] + "&mode=document&network=https://evil.example.com",
                headers={"Host": name},
            )
            assert response.status_code == 200
            assert expected in response.headers["content-security-policy"]
            assert "evil.example.com" not in response.headers["content-security-policy"]
        assert (
            client.get(url + "/?mode=document&network=*", headers={"Host": urlsplit(doc["origin"]).netloc}).status_code
            == 403
        )
        assert (
            client.get(
                url + "/?registration=" + doc["registration"], headers={"Host": urlsplit(prototype["origin"]).netloc}
            ).status_code
            == 403
        )
        again = register(client, host_url, runtime, origins=["https://api.example.com"]).json()
        assert doc["origin"] == again["origin"]
        assert doc["registration"] != again["registration"]
        assert (tmp_path / "runtime/origin.key").stat().st_mode & 0o077 == 0


def test_document_native_ai_context_navigation_and_storage_are_isolated(runtime_sites):
    host_url, runtime, _tmp = runtime_sites
    material = """<!doctype html><title>Native AI handbook</title><style>h2{outline:1px dotted blue}</style>
<link rel="stylesheet" href="https://api.example.com/native.css"><script src="https://api.example.com/native.js"></script><img src="https://api.example.com/native.svg" alt="native resource">
<a href="#rule">原目录</a><button id="original">原文按钮</button><div id="own-menu" hidden>原文右键菜单</div>
<iframe title="original embedded content" srcdoc="<p>原文嵌入内容</p>"></iframe>
<label>文档自己的Key<input id="key"></label><button id="ask">原生AI问答</button><pre id="answer"></pre>
<div style="height:900px"></div><h2 id="rule">准确原文章节</h2><script>
window.originalClicks=0; document.getElementById('original').onclick=()=>window.originalClicks++;
document.addEventListener('contextmenu',event=>{event.preventDefault();document.getElementById('own-menu').hidden=false});
const key=document.getElementById('key');key.value=localStorage.getItem('native-key')||'';
key.oninput=()=>localStorage.setItem('native-key',key.value);
document.getElementById('ask').onclick=async()=>{try{const response=await fetch('https://api.example.com/v1/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+key.value},body:JSON.stringify({message:'合成文档问题'})});document.getElementById('answer').textContent=await response.text()}catch{document.getElementById('answer').textContent='blocked'}};
try{window.platform=parent.parent.localStorage.getItem('platform-secret')}catch{window.platform='blocked'}
</script>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        external, local_api = [], []

        def model_route(route):
            external.append(
                {"method": route.request.method, "headers": route.request.headers, "body": route.request.post_data}
            )
            if route.request.url.endswith("native.css"):
                route.fulfill(content_type="text/css", body="body{--native-resource:preserved}")
                return
            if route.request.url.endswith("native.js"):
                route.fulfill(content_type="text/javascript", body="window.externalNativeLoaded=true")
                return
            if route.request.url.endswith("native.svg"):
                route.fulfill(
                    content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
                )
                return
            if route.request.url.endswith("redirect"):
                route.fulfill(
                    status=302,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Location": host_url + "/api/platform-secret",
                    },
                )
                return
            route.fulfill(
                status=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "authorization,content-type",
                    "Access-Control-Allow-Methods": "POST,GET,OPTIONS",
                },
                content_type="application/json",
                body='{"answer":"合成回答"}',
            )

        page.route("https://api.example.com/**", model_route)
        page.on(
            "request", lambda request: local_api.append(request.url) if "/api/platform-secret" in request.url else None
        )
        try:
            page.goto(host_url)
            page.evaluate("localStorage.setItem('platform-secret','platform-synthetic-secret')")
            options = registration_options(runtime, material, origins=["https://api.example.com"])
            original_origin = page.evaluate("options=>openMaterial(options)", options)
            source = page.frame_locator("#runtime").frame_locator("#material")
            expect(source.locator("#key")).to_be_visible()
            assert source.locator("body").evaluate("()=>window.externalNativeLoaded") is True
            assert (
                source.locator("body").evaluate("e=>getComputedStyle(e).getPropertyValue('--native-resource')")
                == "preserved"
            )
            assert source.get_by_alt_text("native resource").evaluate("e=>e.complete&&e.naturalWidth>0")
            assert source.locator("body").evaluate("()=>window.platform") == "blocked"
            source.locator("#original").click()
            source.locator("#original").click(button="right")
            expect(source.locator("#own-menu")).to_be_visible()
            assert source.locator("body").evaluate("()=>window.originalClicks") == 1
            assert not page.evaluate(
                "events.some(e=>['context.menu','control.activated','action.activated'].includes(e.type))"
            )
            source.locator("#key").fill("synthetic-native-only-key")
            source.locator("#ask").click()
            expect(source.locator("#answer")).to_contain_text("合成回答")
            actual = [request for request in external if request["method"] == "POST"]
            assert len(actual) == 1 and actual[0]["headers"]["authorization"] == "Bearer synthetic-native-only-key"
            assert "platform-synthetic-secret" not in json.dumps(actual)
            source.locator("body").evaluate("url=>fetch(url).catch(()=>{})", host_url + "/api/platform-secret")
            source.locator("body").evaluate("()=>fetch('https://api.example.com/redirect').catch(()=>{})")
            # Chromium may announce a redirected request before CSP cancels it;
            # the isolated platform server must never actually receive it.
            assert not runtime.platform_api_requests
            page.evaluate(
                "command('document.reveal',{location:{anchor:'rule',selector:'#rule',heading:'准确原文章节',heading_level:2,heading_ordinal:0}})"
            )
            page.wait_for_function("events.some(e=>e.type==='command.result'&&e.payload.ok)")
            expect(source.locator("#rule")).to_be_in_viewport()
            assert source.locator("#rule").evaluate("e=>getComputedStyle(e).outlineWidth") == "1px"
            assert not page.evaluate("events.some(e=>e.type==='document.changed')")
            source.get_by_role("link", name="原目录").click()
            source.locator("body").evaluate("()=>location.hash='different-original-position'")
            page.wait_for_function(
                "events.some(e=>e.type==='document.changed'&&e.payload.anchor==='different-original-position')"
            )
            source.locator("body").evaluate(
                """async()=>{const request=indexedDB.open('native-config',1);const db=await new Promise((resolve,reject)=>{request.onupgradeneeded=()=>request.result.createObjectStore('settings');request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error)});await new Promise(resolve=>{const tx=db.transaction('settings','readwrite');tx.objectStore('settings').put('persistent-value','connection');tx.oncomplete=resolve});db.close()}"""
            )
            page.reload()
            assert page.evaluate("options=>openMaterial(options)", options) == original_origin
            expect(source.locator("#key")).to_have_value("synthetic-native-only-key")
            assert (
                source.locator("body").evaluate(
                    """async()=>{const request=indexedDB.open('native-config',1);const db=await new Promise(resolve=>request.onsuccess=()=>resolve(request.result));return await new Promise(resolve=>{const read=db.transaction('settings').objectStore('settings').get('connection');read.onsuccess=()=>{resolve(read.result);db.close()}})}"""
                )
                == "persistent-value"
            )
            before = len(external)
            assert page.evaluate("options=>openMaterial(options)", {**options, "origins": []}) == original_origin
            expect(source.locator("#key")).to_have_value("synthetic-native-only-key")
            source.locator("#ask").click()
            expect(source.locator("#answer")).to_have_text("blocked")
            assert len(external) == before
            for field in ["accountId", "projectId", "packageId"]:
                changed = {**options, "scope": {**options["scope"], field: "other"}}
                assert page.evaluate("options=>openMaterial(options)", changed) != original_origin
                expect(source.locator("#key")).to_have_value("")
            changed = registration_options(runtime, material + "<!--new exact version-->", origins=options["origins"])
            assert page.evaluate("options=>openMaterial(options)", changed) != original_origin
            expect(source.locator("#key")).to_have_value("")
            blocked = registration_options(runtime, material, mode="prototype")
            assert page.evaluate("options=>openMaterial(options)", blocked) != original_origin
            expect(source.locator("#key")).to_have_value("")
            before = len(external)
            source.locator("#ask").click()
            expect(source.locator("#answer")).to_have_text("blocked")
            assert len(external) == before
        finally:
            browser.close()


def test_document_reveal_static_paths_and_lazy_handbook_use_original_navigation(runtime_sites):
    host_url, runtime, _tmp = runtime_sites
    static = site_html(
        "index.html",
        [
            ("index.html", b'<title>Home</title><a href="docs/other.html#rule">Other</a>'),
            (
                "docs/other.html",
                b'<title>Other</title><a href="#bottom">Same page</a><input id="retained"><h2 id="rule">Rule</h2><div style="height:900px"></div><h2 id="bottom">Bottom</h2>',
            ),
        ],
    ).decode()
    handbook = """<!doctype html><article id="doc-a" hidden></article><article id="doc-b" hidden></article>
<script type="application/json" id="source-data">{"doc-a":{"path":"a.md"},"doc-b":{"path":"nested/b.md"}}</script>
<script>window.fhBookPages={ensure:anchor=>{const article=document.getElementById(anchor.split('--')[0]);if(!article.innerHTML)article.innerHTML='<h3 id="'+article.id+'--rule">Repeated</h3><h3>Repeated</h3>'}};window.addEventListener('hashchange',()=>{const anchor=decodeURIComponent(location.hash.slice(1));fhBookPages.ensure(anchor);document.querySelectorAll('article').forEach(a=>a.hidden=!anchor.startsWith(a.id));});</script>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 1000})
        try:
            page.goto(host_url)
            page.evaluate("options=>openMaterial(options)", registration_options(runtime, static))
            source = page.frame_locator("#runtime").frame_locator("#material").frame_locator("#actualPage")
            source.get_by_role("link", name="Other", exact=True).click()
            expect(source.locator("#rule")).to_be_in_viewport()
            page.wait_for_function("events.some(e=>e.type==='document.changed'&&e.payload.path==='docs/other.html')")
            source.locator("#retained").fill("preserved")
            source.get_by_role("link", name="Same page", exact=True).click()
            expect(source.locator("#bottom")).to_be_in_viewport()
            expect(source.locator("#retained")).to_have_value("preserved")
            page.evaluate(
                "command('document.reveal',{location:{path:'docs/other.html',anchor:'rule',heading:'Rule',heading_level:2,heading_ordinal:0}})"
            )
            page.wait_for_function("events.some(e=>e.type==='command.result'&&e.payload.ok)")
            expect(source.locator("#retained")).to_have_value("preserved")
            expect(source.locator("#rule")).to_be_in_viewport()
            page.evaluate("options=>openMaterial(options)", registration_options(runtime, handbook))
            handbook_source = page.frame_locator("#runtime").frame_locator("#material")
            expect(handbook_source.locator("#source-data")).to_be_attached()
            handbook_source.locator("body").evaluate("()=>location.hash='doc-a--rule'")
            page.wait_for_function("events.some(e=>e.type==='document.changed'&&e.payload.path==='handbook/a.md')")
            page.evaluate("events.length=0")
            page.evaluate(
                "command('document.reveal',{location:{path:'handbook/nested/b.md',articleId:'doc-b',anchor:'doc-b--rule',heading:'Repeated',heading_level:3,heading_ordinal:0}})"
            )
            page.wait_for_function("events.some(e=>e.type==='command.result'&&e.payload.ok)")
            expect(handbook_source.locator("#doc-b--rule")).to_be_visible()
            expect(handbook_source.locator("#doc-a")).not_to_be_visible()
            page.evaluate(
                "command('document.reveal',{location:{path:'handbook/nested/b.md',anchor:'doc-b--rule',heading:'Repeated',heading_level:3,heading_ordinal:1}})"
            )
            page.wait_for_function("events.some(e=>e.type==='command.result'&&!e.payload.ok)")
            assert not page.evaluate("events.some(e=>e.type==='context.menu')")
        finally:
            browser.close()


def test_native_document_submit_handlers_run_while_default_form_writes_and_prototype_forms_stay_blocked(runtime_sites):
    host_url, runtime, _tmp = runtime_sites
    material = """<!doctype html><title>Native form AI</title>
<form id="ai-form"><input aria-label="Original question" name="question"><button type="submit">Submit native AI</button></form>
<output id="submit-count">0</output><pre id="answer"></pre>
<form id="default-form" action="https://api.example.com/native-form" method="post"><input name="private-field" value="synthetic-private-field"><button type="submit">Submit default form</button></form>
<script>document.getElementById('ai-form').addEventListener('submit',async event=>{
 event.preventDefault();document.getElementById('submit-count').textContent=String(Number(document.getElementById('submit-count').textContent)+1);
 try{const response=await fetch('https://api.example.com/v1/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:document.querySelector('[name="question"]').value})});document.getElementById('answer').textContent=await response.text()}catch{document.getElementById('answer').textContent='blocked'}
});</script>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page()
        received = []

        def external(route):
            received.append({"url": route.request.url, "body": route.request.post_data})
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "content-type",
                    "Access-Control-Allow-Methods": "POST,OPTIONS",
                },
                body='{"answer":"synthetic form response"}',
            )

        page.route("https://api.example.com/**", external)
        try:
            page.goto(host_url)
            page.evaluate(
                "options=>openMaterial(options)",
                registration_options(runtime, material, origins=["https://api.example.com"]),
            )
            source = page.frame_locator("#runtime").frame_locator("#material")
            source.get_by_label("Original question", exact=True).fill("Original submit event")
            source.get_by_role("button", name="Submit native AI", exact=True).click()
            expect(source.locator("#submit-count")).to_have_text("1")
            expect(source.locator("#answer")).to_contain_text("synthetic form response")
            assert page.locator("#runtime").evaluate("iframe=>iframe.sandbox.contains('allow-forms')")
            assert (
                page.frame_locator("#runtime")
                .locator("#material")
                .evaluate("iframe=>iframe.sandbox.contains('allow-forms')")
            )
            assert len(received) == 1 and "Original submit event" in received[0]["body"]
            source.get_by_role("button", name="Submit default form", exact=True).click()
            expect(source.locator("#submit-count")).to_have_text("1")
            expect(source.locator("#answer")).to_contain_text("synthetic form response")
            assert len(received) == 1 and "private-field" not in json.dumps(received)
            page.evaluate("options=>openMaterial(options)", registration_options(runtime, material, mode="prototype"))
            source.get_by_label("Original question", exact=True).fill("Must stay sandboxed")
            source.get_by_role("button", name="Submit native AI", exact=True).click()
            expect(source.locator("#submit-count")).to_have_text("0")
            expect(source.locator("#answer")).to_have_text("")
            assert not page.locator("#runtime").evaluate("iframe=>iframe.sandbox.contains('allow-forms')")
            assert (
                not page.frame_locator("#runtime")
                .locator("#material")
                .evaluate("iframe=>iframe.sandbox.contains('allow-forms')")
            )
            assert len(received) == 1
        finally:
            browser.close()
