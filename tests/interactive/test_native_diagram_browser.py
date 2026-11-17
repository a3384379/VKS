"""Classic scrollbars must not feed native diagram fit observers indefinitely."""

import base64
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio, source_frame
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials
ROOT = Path(__file__).resolve().parents[2]


def native_diagram():
    viewer = (ROOT / "packages/handbook-template/standalone/diagram.js").read_text()
    return (
        """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:">
<title>Native diagrams</title><style>
*{box-sizing:border-box}body{margin:0;font:16px system-ui}
.diagram-canvas{width:600px}.diagram-canvas svg{width:500px;height:auto}
#diagram-dialog{width:800px;height:440px;padding:0;background:white}
.dialog-bar{height:50px;display:flex;align-items:center;justify-content:space-between}
.diagram-actions{display:flex}#diagram-viewport{width:720px;height:364px;padding:12px;overflow:auto;touch-action:none}
#diagram-viewport::-webkit-scrollbar{width:17px;height:17px}
#diagram-zoom-content{display:grid;place-items:center;min-width:100%;min-height:100%}
#diagram-zoom-content svg{max-width:none;display:block}
</style></head><body><h1>Native diagram handbook</h1>
<figure class="diagram"><figcaption><span>Flow diagram</span><button class="zoom-diagram">放大查看</button></figcaption>
<div class="diagram-canvas"><svg id="original-chart" xmlns="http://www.w3.org/2000/svg" viewBox="-8 -9 1587 783.25" width="1587" height="783.25">
<style>#original-chart text{font:24px sans-serif;fill:#142232}</style>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="#245bac"/></marker></defs>
<rect x="10" y="10" width="350" height="120" fill="white" stroke="#245bac"/>
<text x="24" y="70">收费配置与输入</text><path d="M360 60L600 60" stroke="#245bac" marker-end="url(#arrow)"/>
</svg></div></figure>
<dialog id="diagram-dialog"><div class="dialog-bar"><h2 id="diagram-title">Diagram</h2><div class="diagram-actions"><button id="zoom-out">缩小</button><button id="zoom-reset">100%</button><button id="zoom-in">放大</button><button class="close-dialog" id="close">关闭</button></div></div>
<div id="diagram-viewport"><div id="diagram-zoom-content"></div></div></dialog>
<script>document.querySelector('#close').onclick=()=>document.querySelector('#diagram-dialog').close();</script>
<script>"""
        + viewer
        + "</script></body></html>"
    )


def sample(viewport):
    return viewport.evaluate("""e=>new Promise(resolve=>{
      let rows=[],n=0;
      function tick(){const r=e.querySelector('svg').getBoundingClientRect();
        rows.push([e.clientWidth,e.clientHeight,r.width,r.height]);
        if(++n<45)requestAnimationFrame(tick);else resolve(rows)}
      requestAnimationFrame(tick)
    })""")


@pytest.mark.parametrize("pixel_ratio,replace_root", [(1, False), (1.25, True)])
def test_native_fit_scrollbars_settle_and_original_zoom_pan_continue(
    materials, tmp_path, monkeypatch, pixel_ratio, replace_root
):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    raw = native_diagram()
    if replace_root:
        encoded = base64.b64encode(raw.encode()).decode()
        raw = (
            "<title>Opening</title><h1>Opening handbook</h1><script>setTimeout(()=>{document.open();"
            f'document.write(new TextDecoder().decode(Uint8Array.from(atob("{encoded}"),x=>x.charCodeAt(0))));'
            "document.close()},150)</script>"
        )
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", raw, filename="handbook.html")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        # Headless Chromium normally hides scrollbars, masking this defect.
        browser = p.chromium.launch(
            executable_path=chromium_path(p), headless=True, ignore_default_args=["--hide-scrollbars"]
        )
        context = browser.new_context(viewport={"width": 1450, "height": 1000}, device_scale_factor=pixel_ratio)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, url, pid, aid)
            source = source_frame(page)
            expect(source.get_by_role("heading", name="Native diagram handbook", exact=True)).to_be_visible(
                timeout=30000
            )
            original = source.locator(".diagram-canvas").inner_html()
            viewport = source.locator("#diagram-viewport")
            svg = source.locator("#diagram-zoom-content > svg")
            for _ in range(2):
                source.get_by_role("button", name="放大查看", exact=True).click()
                expect(svg).to_be_visible()
                for mode in ["diagram-fit-width", "diagram-fit-window"]:
                    source.locator("#" + mode).click()
                    values = sample(viewport)
                    assert len({tuple(row) for row in values[-20:]}) == 1
                    client = values[-1][:2]
                    assert client[0] < 720 and client[1] < 364
                source.locator("#zoom-reset").click()
                expect(svg).to_have_css("width", "1587px")
                for delta in [-40, 90, -80]:
                    viewport.dispatch_event("wheel", {"deltaY": delta, "clientX": 400, "clientY": 300})
                    values = sample(viewport)
                    assert len({tuple(row) for row in values[-20:]}) == 1
                    assert values[-1][:2] == client
                box = viewport.bounding_box()
                x, y = box["x"] + 350, box["y"] + 200
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x - 80, y - 50, steps=5)
                page.mouse.up()
                assert viewport.evaluate("e=>e.scrollLeft>0 && e.scrollTop>0")
                expect(svg).to_have_count(1)
                source.locator("#close").click()
                assert source.locator(".diagram-canvas").inner_html() == original
            assert not errors
        finally:
            context.close()
            browser.close()
