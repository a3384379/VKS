"""The exported original reader works offline in a credential-free sandbox."""

import base64
import gzip
import json
import re
from hashlib import sha256

from playwright.sync_api import expect, sync_playwright
from vks_api.readonly_export import html_library

from tests.interactive import test_materials
from tests.interactive.test_document_navigation import zip_docs
from tests.interactive.test_native_documents import source_handbook
from tests.interactive.test_native_export import project
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_original_static_site_offline_styles_navigation_exact_sections_and_isolation(materials, tmp_path):
    raw = zip_docs(
        [
            (
                "index.html",
                '<title>Home</title><link rel="stylesheet" href="theme.css"><h1 id="home">Original home</h1><p>First original page.</p><a href="next.html#rule">Read next</a>',
            ),
            (
                "next.html",
                '<title>Next</title><style>h1{color:rgb(23,45,67)}</style><h1 id="rule">Exact rule</h1><p>Second original page.</p>',
            ),
            ("theme.css", "h1{color:rgb(12,34,56)}"),
        ]
    )
    payload, _, _, _ = project(materials, raw, "site.zip")
    path = tmp_path / "original-library.html"
    path.write_bytes(html_library(payload))
    second = next(doc for doc in payload["documents"] if doc["native"]["path"] == "next.html")
    section = second["native"]["sections"][0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            source = page.frame_locator("iframe.native-document-frame")
            expect(source.get_by_role("heading", name="Original home", exact=True)).to_be_visible()
            expect(page.locator(".document-library-bar")).to_be_hidden()
            assert (
                page.locator(".native-document-toolbar").evaluate(
                    "node => Math.abs(node.getBoundingClientRect().top-document.querySelector('.topbar').getBoundingClientRect().bottom)"
                )
                < 2
            )
            assert source.locator("h1").evaluate("el=>getComputedStyle(el).color") == "rgb(12, 34, 56)"
            assert (
                source.locator("body").evaluate("()=>{try{return parent.document.body.tagName}catch{return 'opaque'}}")
                == "opaque"
            )
            assert page.locator("iframe.native-document-frame").get_attribute("sandbox") == "allow-scripts allow-modals"
            # Opening the native view also reveals its initial selection across
            # animation frames. Finish that scroll before a physical link click
            # so a moving iframe cannot receive its mouse-down/up at two targets.
            source.locator("body").evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))))"
            )
            source.get_by_role("link", name="Read next", exact=True).click()
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_visible()
            expect(page).to_have_url(re.compile(r"#section=" + re.escape(section["revision_id"])))
            assert source.locator("h1").evaluate("el=>getComputedStyle(el).color") == "rgb(23, 45, 67)"
            expect(page.get_by_role("button", name="标准阅读", exact=True)).to_have_count(0)
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_visible()
            expect(page.locator(".document-library-bar")).to_be_hidden()
            assert not errors and not [url for url in requests if url.startswith("http")]
        finally:
            browser.close()


def active_handbook():
    packed = source_handbook().decode()
    data = packed.split('<script id="fh-pack-data" type="application/octet-stream">')[1].split("</script>")[0]
    inner = gzip.decompress(base64.b64decode(data)).decode()
    inner = (
        '<title>Original handbook</title><style>body{margin:0}#cover{padding:30px;color:rgb(23,45,67)}</style><div id="cover"><h1>Original cover</h1><a href="#article-b--duplicate-1">Open original rule</a></div>'
        + inner
    )
    inner += """<input id="fh-update-file" type="file"><button id="fh-update-save">Update source</button>
<div id="fh-ai-root"><input id="fh-ai-key" value="SOURCE_SAVED_KEY"></div>
<script id="fh-ai-runtime">window.forbiddenSourceUpdater=true;</script>
<script id="fh-pack-template" type="application/json">"unused-portable-loader"</script>
<script id="fh-pack-codec">(()=>{window.fhBookPages={};window.compatibleReaderReady=true;const template=JSON.parse(document.getElementById('fh-pack-template').textContent);window.forbiddenPortableExport=template;})();</script>
<script>
const pages=JSON.parse(document.querySelector('#fh-book-pages').textContent);
window.fhBookPages={ensure:id=>{const el=document.getElementById(id);if(el&&pages[id])el.innerHTML=pages[id].html}};
function route(){let hash=decodeURIComponent(location.hash.slice(1));const key=Object.keys(pages).find(id=>hash===id||hash.startsWith(id+'--'));for(const el of document.querySelectorAll('article'))el.hidden=el.id!==key;document.querySelector('#cover').hidden=!!key;if(key)window.fhBookPages.ensure(key)};
window.addEventListener('hashchange',route);route();
</script>"""
    raw = inner.encode()
    metadata = {"format": "fh-gzip-html-1", "bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    return (
        '<script id="fh-pack-meta" type="application/json">'
        + json.dumps(metadata)
        + '</script><script id="fh-pack-data" type="application/octet-stream">'
        + base64.b64encode(gzip.compress(raw)).decode()
        + "</script>"
    )


def test_rapid_native_reveals_keep_latest_scroll_and_ignore_forged_source(materials, tmp_path):
    payload, _, _, _ = project(
        materials,
        '<h1 id="first">First</h1><p>First rule.</p><h2 id="last">Last</h2><p>Last rule.</p>',
    )
    path = tmp_path / "rapid-native.html"
    path.write_bytes(html_library(payload))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            source = page.frame_locator("iframe.native-document-frame")
            expect(source.get_by_role("heading", name="Last", exact=True)).to_be_visible()
            token = source.locator("head script").first.evaluate(
                'node => node.textContent.match(/\\)\\("([^"]+)",/)[1]'
            )
            source.locator("body").evaluate("""() => {
              window.pendingPaints=[]; window.scrolled=[];
              window.requestAnimationFrame=callback=>{pendingPaints.push(callback);document.body.dataset.pendingReveals=String(pendingPaints.length);return pendingPaints.length};
              Element.prototype.scrollIntoView=function(){scrolled.push(this.id)};
            }""")
            page.evaluate(
                """token => {
              const frame=document.querySelector('iframe.native-document-frame').contentWindow;
              for(const anchor of ['first','last']) frame.postMessage({vks_native_reader:token,type:'reveal',location:{anchor}},'*');
            }""",
                token,
            )
            expect(source.locator("body")).to_have_attribute("data-pending-reveals", "2")
            source.locator("body").evaluate("""() => {
              while(pendingPaints.length) pendingPaints.splice(0).forEach(callback=>callback());
            }""")
            assert source.locator("body").evaluate("() => scrolled") == ["last"]
            current_hash = page.evaluate("location.hash")
            page.evaluate(
                """token => dispatchEvent(new MessageEvent('message', {source:window,data:{vks_native_reader:token,type:'changed',payload:{path:'guide.html',anchor:'first'}}}))""",
                token,
            )
            assert page.evaluate("location.hash") == current_hash
            assert not errors
        finally:
            browser.close()


def test_exact_native_heading_without_html_id_keeps_host_revision_selection(materials, tmp_path):
    payload, _, _, _ = project(materials, "<h1>Guide</h1><p>First.</p><h2>Exact rule</h2><p>Second.</p>")
    selected = payload["documents"][0]["native"]["sections"][-1]
    assert selected["anchor"] == ""
    path = tmp_path / "unanchored-native.html"
    path.write_bytes(html_library(payload))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(path.as_uri() + "#section=" + selected["revision_id"])
            source = page.frame_locator("iframe.native-document-frame")
            expect(source.get_by_role("heading", name="Exact rule", exact=True)).to_be_visible()
            source.locator("body").evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))))"
            )
            expect(page).to_have_url(re.compile(r"#section=" + re.escape(selected["revision_id"])))
        finally:
            browser.close()


def test_known_handbook_keeps_home_and_native_navigation_and_disables_updates(materials, tmp_path):
    payload, _, _, _ = project(materials, active_handbook(), "handbook.html")
    second = next(doc for doc in payload["documents"] if doc["native"]["document_anchor"] == "article-b")
    section = next(item for item in second["native"]["sections"] if item["anchor"] == "article-b--duplicate-1")
    path = tmp_path / "original-handbook.html"
    path.write_bytes(html_library(payload))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            source = page.frame_locator("iframe.native-document-frame")
            expect(source.get_by_role("heading", name="Original cover", exact=True)).to_be_visible()
            expect(source.locator("#fh-update-save")).to_be_hidden()
            expect(source.locator("#fh-update-file")).to_be_disabled()
            assert source.locator("body").evaluate("()=>window.forbiddenSourceUpdater") is None
            assert source.locator("body").evaluate("()=>window.compatibleReaderReady") is True
            assert source.locator("body").evaluate("()=>window.forbiddenPortableExport") is None
            assert "SOURCE_SAVED_KEY" not in page.content()
            source.get_by_role("link", name="Open original rule", exact=True).click()
            expect(source.get_by_text("Second rule.", exact=True)).to_be_visible()
            expect(page).to_have_url(re.compile(r"#section=" + re.escape(section["revision_id"])))
            expect(page.get_by_role("button", name="标准阅读", exact=True)).to_have_count(0)
            expect(source.get_by_text("Second rule.", exact=True)).to_be_visible()
            page.get_by_role("button", name="打开知识助手", exact=True).click()
            document_ai = page.frame_locator('iframe[title="知识手册 AI 助手"]')
            expect(document_ai.locator("#fh-ai-panel")).to_be_visible()
            expect(document_ai.get_by_role("button", name="AI 讲解", exact=True)).to_have_count(0)
            assert not errors
        finally:
            browser.close()
