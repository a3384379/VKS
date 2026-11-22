"""Exercise the bundled MCP App in a sandbox with a host message channel."""

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.mcp_gateway import APIBridge, GatewayError
from vks_connect.sidebar import open_studio, sidebar_page

from tests.ui.test_frontend_browser import chromium_path

STUDIO = "http://127.0.0.1:5193"
PROJECT = "10000000-0000-0000-0000-000000000001"
RELEASE = "10000000-0000-0000-0000-000000000002"
DOCUMENT = "10000000-0000-0000-0000-000000000003"
REVISION = "10000000-0000-0000-0000-000000000004"
SECTION = "10000000-0000-0000-0000-000000000005"
FRAGMENT = f"project={PROJECT}&release={RELEASE}&view=documents&doc={REVISION}&section={SECTION}&reader=standard"
LINK = STUDIO + "/#" + FRAGMENT
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/Ep8AAAAASUVORK5CYII="
LOCATOR = {
    "project_id": PROJECT,
    "release_id": RELEASE,
    "document_id": DOCUMENT,
    "document_revision_id": REVISION,
    "section_revision_id": SECTION,
    "content_sha256": "a" * 64,
}
MARKDOWN = """## 收费台合并缴费

**先核对账单**

核对[缴费说明](https://example.com/)中的 `账单条数`。

- 保留已选项

> 确认后再提交。

| 条数 | 行为 |
| --- | --- |
| 100 | 允许 |
| 101 | 拦截 |

```javascript
// 校验上限
const limit = 100;
```

```mermaid
%%{init: {"flowchart": {"useMaxWidth": false}}}%%
flowchart LR
  A[选择账单] --> B[确认收费]
```

![原文图片](attachments/费用.png)
![外部图片](https://untrusted.example/track.png)

<script>document.body.dataset.executed = "yes"</script>
"""


def assert_readable_text(locator):
    contrast = locator.evaluate("""element => {
      const rgb = color => color.match(/[\\d.]+/g).map(Number);
      const luminance = color => rgb(color).slice(0, 3).reduce((sum, channel, i) => {
        const s = channel / 255;
        const linear = s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        return sum + linear * [0.2126, 0.7152, 0.0722][i];
      }, 0);
      let surface = element;
      while (getComputedStyle(surface).backgroundColor === "rgba(0, 0, 0, 0)") {
        surface = surface.parentElement;
      }
      const text = luminance(getComputedStyle(element).color);
      const background = luminance(getComputedStyle(surface).backgroundColor);
      return (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05);
    }""")
    assert contrast >= 4.5, f"{locator}: contrast {contrast:.2f}:1"


def test_sidebar_renders_pinned_chapters_and_opens_studio():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright))
        try:
            page = browser.new_page(viewport={"width": 440, "height": 1000}, color_scheme="dark")
            page.route(
                "http://127.0.0.1:49152/",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body='<iframe title="VKS" sandbox="allow-scripts" style="width:100%;height:100vh"></iframe>',
                ),
            )
            page.goto("http://127.0.0.1:49152/")
            page.evaluate(
                """({html, studio, link, locator, markdown, png}) => {
                  const view = document.querySelector("iframe");
                  window.messages = [];
                  const send = (payload) => view.contentWindow.postMessage({jsonrpc:"2.0",...payload}, "*");
                  window.changeHostTheme = theme => send({method:"ui/notifications/host-context-changed",params:{theme}});
                  window.addEventListener("message", ({source, data}) => {
                    if (source !== view.contentWindow) return;
                    const {id, method, params} = data;
                    window.messages.push({method, params});
                    if (method === "ui/initialize") {
                      send({id, result:{protocolVersion:params.protocolVersion,hostContext:{theme:"dark"}}});
                    } else if (method === "ui/notifications/initialized") {
                      send({method:"ui/notifications/tool-result",params:{_meta:{"vks/workspace":{
                        studioBaseUrl:studio,defaultProjectId:locator.project_id,
                        projects:[{id:locator.project_id,name:"收费"}]
                      }}}});
                    } else if (method === "tools/call") {
                      let result;
                      switch (params.name) {
                        case "get_project_context":
                          result = {scope:{project_id:locator.project_id,release_id:locator.release_id,
                            release_label:"v23"}};
                          break;
                        case "search_knowledge":
                          result = [
                            {entity:{name:"收费台合并缴费"},excerpt:"-> 被截断的图表片段",locator,studio_url:link},
                            {entity:{name:"外部引用"},studio_url:"https://untrusted.example/"},
                          ];
                          break;
                        case "get_workspace_document": {
                          const offset = params.arguments.offset;
                          const next = offset === 0 ? 60 : null;
                          result = {offset,next_offset:next,document_id:locator.document_id,
                            entity_revision_id:locator.document_revision_id,sections:[{locator,
                              section_revision_id:locator.section_revision_id,
                              content:markdown.slice(offset,next || undefined)}]};
                          break;
                        }
                        case "read_vks_document_image":
                          send({id,result:{content:[{type:"image",mimeType:"image/png",data:png}]}});
                          return;
                        case "open_vks_studio":
                          if (!params.arguments.fragment) {
                            send({id,result:{isError:true}});
                            return;
                          }
                          result = {requested:true};
                          break;
                      }
                      send({id,result:{structuredContent:{data:result}}});
                    }
                  });
                  view.srcdoc = html;
                }""",
                {
                    "html": sidebar_page(),
                    "studio": STUDIO,
                    "link": LINK,
                    "locator": LOCATOR,
                    "markdown": MARKDOWN,
                    "png": PNG,
                },
            )
            app = page.frame_locator('iframe[title="VKS"]')
            expect(app.get_by_text("保存版本 v23", exact=True)).to_be_visible()
            expect(app.locator("html")).to_have_attribute("data-theme", "dark")
            app.get_by_label("搜索保存版本").fill("合并缴费")
            app.get_by_role("button", name="搜索").click()
            expect(app.locator("#status")).to_have_text("找到 2 个章节")
            expect(app.locator("article strong")).to_have_text("先核对账单")
            expect(app.locator("article ul li")).to_have_text("保留已选项")
            expect(app.get_by_role("cell", name="101")).to_be_visible()
            image = app.get_by_role("img", name="原文图片")
            image.scroll_into_view_if_needed()
            expect(image).to_have_js_property("naturalWidth", 1)
            diagram = app.get_by_role("img", name="Mermaid 图表")
            expect(diagram).to_be_attached(timeout=20_000)
            diagram.scroll_into_view_if_needed()
            assert diagram.evaluate("image => image.naturalWidth > 0")
            markdown = app.locator("article .rich-markdown")
            for theme in ("dark", "light", "dark"):
                page.evaluate("theme => window.changeHostTheme(theme)", theme)
                expect(app.locator("html")).to_have_css("color-scheme", theme)
                for selector in (
                    "p:has(> a)", "strong", "li", "blockquote p", "th", "tbody tr:first-child td",
                    "tbody tr:nth-child(2) td", "p code", "pre code", ".md-code-language", ".hljs-comment",
                    ".hljs-keyword", ".hljs-number", "a", ".md-image-note", ".md-diagram-head button",
                    ".md-diagram summary",
                ):
                    assert_readable_text(markdown.locator(selector).first)
            diagram.click()
            viewer = app.get_by_role("dialog")
            expect(viewer).to_be_visible()
            assert_readable_text(viewer.locator(".md-image-title"))
            assert_readable_text(viewer.locator(".md-image-help"))
            viewer.get_by_role("button", name="关闭").click()
            expect(viewer).to_have_count(0)
            expect(app.get_by_text("-> 被截断的图表片段", exact=True)).to_have_count(0)
            expect(app.locator("body")).not_to_have_attribute("data-executed", "yes")
            expect(app.get_by_role("img", name="外部图片")).to_have_count(0)
            app.get_by_role("link", name="打开 Studio", exact=True).click()
            expect(app.locator("#navigation-status")).to_have_text("系统未能打开 Studio，可以复制地址。")
            expect(app.get_by_label("Studio 地址")).to_have_value(STUDIO + "/")
            open_preview = app.get_by_role("button", name="预览原文章节")
            open_preview.click()
            preview = app.get_by_role("dialog", name="收费台合并缴费")
            expect(preview).to_be_visible()
            expect(preview.get_by_role("cell", name="101")).to_be_visible()
            expect(preview.get_by_role("img", name="原文图片")).to_have_js_property("naturalWidth", 1)
            for theme in ("light", "dark"):
                page.evaluate("theme => window.changeHostTheme(theme)", theme)
                expect(app.locator("html")).to_have_css("color-scheme", theme)
                assert_readable_text(preview.locator("p:has(> a)"))
                assert_readable_text(preview.get_by_role("link", name="在 Studio 打开原文章节"))
            page.keyboard.press("Escape")
            expect(preview).to_be_hidden()
            expect(open_preview).to_be_focused()
            title = app.get_by_role("button", name="收费台合并缴费", exact=True)
            title.click()
            expect(preview.get_by_text("先核对账单", exact=True)).to_be_visible()
            preview.get_by_role("button", name="关闭", exact=True).click()
            expect(preview).to_be_hidden()
            expect(title).to_be_focused()
            launches = page.evaluate("window.messages.filter(m => m.params?.name === 'open_vks_studio')")
            assert len(launches) == 1  # 页面顶部的跳转
            title.click()
            preview.get_by_role("link", name="在 Studio 打开原文章节").click()
            expect(preview).to_be_hidden()
            expect(app.locator("#navigation-status")).to_have_text("已请求系统浏览器打开 Studio。")
            expect(app.get_by_label("Studio 地址")).to_have_value(LINK)
            calls = page.evaluate("window.messages.filter(m => m.method === 'tools/call').map(m => m.params)")
            assert calls[0]["arguments"] == {"project_id": PROJECT, "release_selector": "latest_saved"}
            assert calls[1]["arguments"] == {
                "project_id": PROJECT,
                "release_id": RELEASE,
                "query": "合并缴费",
                "retrieval_mode": "keyword",
                "kinds": ["section"],
                "limit": 8,
            }
            assert [call["arguments"] for call in calls if call["name"] == "get_workspace_document"] == [
                {
                    "project_id": PROJECT,
                    "release_id": RELEASE,
                    "document_id": DOCUMENT,
                    "revision_id": REVISION,
                    "section_revision_ids": [SECTION],
                    "max_chars": 8000,
                    "offset": offset,
                }
                for offset in (0, 60)
            ]
            assert [call["arguments"] for call in calls if call["name"] == "read_vks_document_image"] == [
                {
                    "release_id": RELEASE,
                    "document_id": DOCUMENT,
                    "document_revision_id": REVISION,
                    "path": "attachments/%E8%B4%B9%E7%94%A8.png",
                }
            ]
            assert [call["arguments"] for call in calls if call["name"] == "open_vks_studio"] == [
                {"fragment": ""},
                {"fragment": FRAGMENT},
            ]
        finally:
            browser.close()


@pytest.mark.asyncio
async def test_studio_launcher_restricts_input_to_configured_origin(monkeypatch):
    from vks_connect import sidebar

    commands = []

    async def launch(argv, stdin):
        commands.append(argv)

    monkeypatch.setattr(sidebar.sys, "platform", "darwin")
    monkeypatch.setattr(sidebar.anyio, "run_process", launch)
    with pytest.raises(ValueError):
        await open_studio(STUDIO, "https://untrusted.example/")
    with pytest.raises(ValueError):
        await open_studio(STUDIO, FRAGMENT + "&next=https://untrusted.example/")
    assert commands == []
    await open_studio(STUDIO, "")
    await open_studio(STUDIO, FRAGMENT)
    assert commands == [["/usr/bin/open", STUDIO + "/"], ["/usr/bin/open", LINK]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, media, succeeds",
    [
        (200, "image/png", True),
        (200, "image/svg+xml", False),
        (302, "image/png", False),
        (403, "application/json", False),
    ],
)
async def test_document_image_uses_saved_attachment_route(status, media, succeeds):
    def handle(request):
        assert request.url.path == f"/api/v1/documents/{DOCUMENT}/revisions/{REVISION}/attachments"
        assert dict(request.url.params) == {"release_id": RELEASE, "path": "images/费用.png"}
        assert request.headers["authorization"] == "Bearer synthetic"
        return httpx.Response(
            status,
            headers={"content-type": media, "location": "https://untrusted.example/"},
            content=b"synthetic image bytes",
        )

    bridge = APIBridge(STUDIO, "synthetic", transport=httpx.MockTransport(handle))
    args = (RELEASE, DOCUMENT, REVISION, "images/费用.png")
    if succeeds:
        assert (await bridge.document_image(*args)).content[0].mimeType == media
    else:
        with pytest.raises(GatewayError):
            await bridge.document_image(*args)
