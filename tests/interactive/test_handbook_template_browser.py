"""The content-free handbook adapts host data and delegates all authoring actions."""

import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from tests.ui.test_frontend_browser import chromium_path

ROOT = Path(__file__).resolve().parents[2]

HOST = r"""
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {HandbookTemplate} from 'COMPONENT_PATH';
const documents=[
{id:'entity-a',revision_id:'a',name:'Overview',path:'guide/overview.md',package_id:'one'},
{id:'entity-b',revision_id:'b',name:'Rules',path:'business/rules.md',package_id:'one'},
{id:'entity-c',revision_id:'c',name:'Other',path:'other.md',package_id:'two'}];
const articles={a:{revision_id:'a',name:'Overview',content:'# Overview\n\n## Introduction\n\nA secret-jellyfish lives only in article text.',sections:[{id:'s0',revision_id:'s0',name:'Overview',content:'# Overview'},{id:'s1',revision_id:'s1',name:'Introduction',content:'## Introduction\n\nA secret-jellyfish lives only in article text.'}]},b:{revision_id:'b',name:'Rules',sections:[{id:'s2',revision_id:'s2',name:'Rule detail',content:'## Rule detail\n\nAuthoritative policy.'}]},c:{revision_id:'c',name:'Other',sections:[{id:'s3',revision_id:'s3',name:'Separate source',content:'## Separate source'}]}};
const navigation={packages:[{id:'one',name:'One',categories:[{id:'intro',name:'Introduction',path:'guide',children:[{kind:'folder',id:'folder',name:'Guide',path:'guide',children:[{kind:'document',id:'a',name:'Overview',path:'guide/overview.md'}]}]},{id:'business',name:'Business',path:'business',children:[{kind:'document',id:'b',name:'Rules',path:'business/rules.md'}]},{id:'private',name:'Hidden category',path:'private',children:[{kind:'document',id:'hidden',name:'Hidden document',path:'private.md'}]}]},{id:'two',name:'Two',categories:[{id:'other',name:'Other',path:'',children:[{kind:'document',id:'c',name:'Other',path:'other.md'}]}]}]};
window.loads=[];
function App(){const [selected,setSelected]=useState(''),[section,setSection]=useState(''),[event,setEvent]=useState(''),[readonly,setReadonly]=useState(false),[empty,setEmpty]=useState(false);
return <><div id="host-toolbar"><button onClick={()=>setReadonly(!readonly)}>Toggle readonly</button><button onClick={()=>{setEmpty(!empty);setSelected('')}}>Toggle empty</button><output aria-label="Host event">{event}</output></div><div style={{height:'calc(100vh - 35px)'}}><HandbookTemplate projectName="Synthetic Atlas" documents={empty?[]:documents} navigation={empty?undefined:navigation} selectedDocument={selected} selectedSection={section} currentDocument={articles[selected]||null} scopeKey={empty?'empty':'demo'} selectionVersion={1} readOnly={readonly} onOpen={(id,chapter='')=>{setSelected(id);setSection(chapter);setEvent(id+':'+chapter)}} onOverview={()=>{setSelected('');setSection('')}} searchDocuments={window.syntheticSearch} loadDocument={async(id,signal)=>{window.loads.push(id);await new Promise(resolve=>setTimeout(resolve,40));if(signal.aborted)throw new DOMException('Aborted','AbortError');return articles[id]}} onAskAI={()=>setEvent('ai:'+selected)} onUpdate={()=>setEvent('update')} onPrint={()=>setEvent('print')} renderReader={<article className="document-reader"><h1>{articles[selected]?.name}</h1><p><strong>Markdown emphasis retained</strong></p><img alt="Inline asset" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='20'%3E%3Crect width='40' height='20' fill='teal'/%3E%3C/svg%3E"/><p>{section}</p></article>}/></div></>}
createRoot(document.getElementById('root')).render(<App/>);
"""


@contextmanager
def handbook_host(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = HOST.replace("COMPONENT_PATH", str(ROOT / "apps/studio/src/HandbookTemplate.tsx"))
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0"><div id="root"></div><script type="module" src="/host.tsx"></script></body></html>'
    )
    (tmp_path / "host.tsx").write_text(source)
    program = """import {createServer} from 'vite';import react from '@vitejs/plugin-react';const server=await createServer({root:process.argv[1],configFile:false,cacheDir:process.argv[1]+'/cache',plugins:[react()],resolve:{dedupe:['react','react-dom'],alias:{'react':process.argv[2]+'/node_modules/react','react-dom':process.argv[2]+'/node_modules/react-dom'}},server:{host:'127.0.0.1',port:Number(process.argv[3]),strictPort:true,fs:{allow:[process.argv[1],process.argv[2]]}}});await server.listen();"""
    with (tmp_path / "vite.log").open("wb") as log:
        process = subprocess.Popen(
            ["node", "--input-type=module", "-e", program, str(tmp_path), str(ROOT), str(port)],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(150):
                assert process.poll() is None, (tmp_path / "vite.log").read_text()
                try:
                    if httpx.get(url, timeout=0.4).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Template host did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_handbook_cover_exact_navigation_fulltext_callbacks_and_readonly(tmp_path):
    with handbook_host(tmp_path) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(url)
            book = page.get_by_role("region", name="知识手册阅读模板")
            expect(book.get_by_role("heading", name="Synthetic Atlas 项目知识手册")).to_be_visible()
            page.screenshot(path=str(tmp_path / "handbook-overview.png"))
            assert page.evaluate("window.loads") == [], "Template overview must not load all article bodies"
            expect(book.get_by_text("Hidden category", exact=True)).to_have_count(0)
            book.get_by_role("button", name="开始阅读", exact=True).click()
            expect(book.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
            expect(
                book.get_by_role("navigation", name="手册文章目录").get_by_role("button", name="Overview", exact=True)
            ).to_be_visible()
            expect(
                book.get_by_role("navigation", name="手册文章目录").get_by_role(
                    "button", name="Introduction", exact=True
                )
            ).to_be_visible()
            expect(book.get_by_text("Markdown emphasis retained", exact=True)).to_be_visible()
            expect(book.get_by_role("img", name="Inline asset")).to_be_visible()
            page.screenshot(path=str(tmp_path / "handbook-article.png"))
            book.get_by_role("navigation", name="文章翻页").get_by_role("button", name="下一篇 Rules").click()
            expect(book.get_by_role("heading", name="Rules", exact=True)).to_be_visible()
            expect(book.get_by_role("navigation", name="文章翻页").get_by_text("Other", exact=True)).to_have_count(0)
            book.get_by_role("button", name="向手册 AI 提问", exact=True).click()
            expect(page.get_by_label("Host event", exact=True)).to_have_text("ai:b")
            book.get_by_role("button", name="更新文档", exact=True).click()
            expect(page.get_by_label("Host event", exact=True)).to_have_text("update")
            book.get_by_role("button", name="打印 / PDF", exact=True).click()
            expect(page.get_by_label("Host event", exact=True)).to_have_text("print")
            book.get_by_role("button", name="搜索全文", exact=True).click()
            dialog = page.get_by_role("dialog", name="手册全文搜索")
            dialog.get_by_label("手册搜索内容", exact=True).fill("secret-jellyfish")
            expect(dialog.get_by_role("button", name="Overview", exact=False)).to_be_visible()
            dialog.get_by_role("button", name="Overview", exact=False).click()
            expect(page.get_by_label("Host event", exact=True)).to_have_text("a:s1")
            expect(dialog).not_to_be_visible()
            page.get_by_role("button", name="Toggle readonly", exact=True).click()
            expect(book.get_by_role("button", name="更新文档", exact=True)).to_have_count(0)
            expect(book.get_by_role("button", name="向手册 AI 提问", exact=True)).to_be_visible()
            page.get_by_role("button", name="Toggle empty", exact=True).click()
            expect(book.get_by_role("heading", name="Synthetic Atlas 项目知识手册")).to_be_visible()
            expect(book.get_by_text("这个手册尚未包含文档。", exact=True)).to_be_visible()
            expect(book.get_by_role("button", name="导入文档", exact=True)).to_have_count(0)
            page.get_by_role("button", name="Toggle readonly", exact=True).click()
            expect(book.get_by_role("button", name="导入文档", exact=True)).to_be_visible()
        finally:
            browser.close()


def test_handbook_mobile_directory_and_keyboard_search(tmp_path):
    with handbook_host(tmp_path) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 375, "height": 800})
            page.goto(url)
            book = page.get_by_role("region", name="知识手册阅读模板")
            expect(book.get_by_role("heading", name="Synthetic Atlas 项目知识手册")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            book.get_by_role("button", name="打开手册目录", exact=True).click()
            book.get_by_role("button", name="Guide", exact=True).click()
            book.get_by_role("button", name="Overview", exact=True).click()
            expect(book.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
            expect(book.get_by_role("button", name="关闭手册目录", exact=True)).to_have_count(0)
            page.keyboard.press("Control+k")
            dialog = page.get_by_role("dialog", name="手册全文搜索")
            expect(dialog).to_be_visible()
            dialog.get_by_label("手册搜索内容", exact=True).fill("Rules")
            dialog.get_by_role("button", name="Rules", exact=False).click()
            expect(book.get_by_role("heading", name="Rules", exact=True)).to_be_visible()
            page.keyboard.press("Control+k")
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
        finally:
            browser.close()


def test_indexed_search_filters_unselected_results_and_discards_late_query(tmp_path):
    with handbook_host(tmp_path) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script(
                "window.pendingSearches=[];window.syntheticSearch=(query,signal)=>new Promise(resolve=>window.pendingSearches.push({query,signal,resolve}))"
            )
            page.goto(url)
            book = page.get_by_role("region", name="知识手册阅读模板")
            book.get_by_role("button", name="搜索全文", exact=True).click()
            dialog = page.get_by_role("dialog", name="手册全文搜索")
            query = dialog.get_by_label("手册搜索内容", exact=True)
            query.fill("first-query")
            page.wait_for_function("window.pendingSearches.some(row=>row.query==='first-query')")
            query.fill("second-query")
            page.wait_for_function("window.pendingSearches.some(row=>row.query==='second-query')")
            page.evaluate(
                "window.pendingSearches.find(row=>row.query==='second-query').resolve([{document:'b',section:'s2',title:'Second hit',snippet:'Current query result'},{document:'hidden',title:'Private unselected title',snippet:'Private unselected body'}])"
            )
            expect(dialog.get_by_role("button", name="Second hit Current query result")).to_be_visible()
            expect(dialog.get_by_text("Private unselected title", exact=True)).to_have_count(0)
            expect(dialog.get_by_text("Private unselected body", exact=True)).to_have_count(0)
            assert page.evaluate("window.pendingSearches.find(row=>row.query==='first-query').signal.aborted")
            page.evaluate(
                "window.pendingSearches.find(row=>row.query==='first-query').resolve([{document:'a',title:'Stale query title',snippet:'Stale query body'}])"
            )
            page.wait_for_function("!document.querySelector('.hb-search-status').textContent.includes('正在')")
            expect(dialog.get_by_text("Stale query title", exact=True)).to_have_count(0)
            expect(dialog.get_by_role("button", name="Second hit Current query result")).to_be_visible()
            assert page.evaluate("window.loads") == [], "Indexed search must avoid downloading the corpus"
            dialog.get_by_role("button", name="Second hit Current query result").click()
            expect(page.get_by_label("Host event", exact=True)).to_have_text("b:s2")
        finally:
            browser.close()
