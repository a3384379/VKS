"""Studio reads complete imported Markdown and assistant text through one renderer."""

import os
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import uid

from tests.interactive import test_materials
from tests.ui.test_frontend_browser import chromium_path, live_api

ROOT = Path(__file__).resolve().parents[2]
materials = test_materials.materials

MARKDOWN = r"""# 完整格式手册

开头段落保留 **开头加粗**。

## 格式与引用

**普通加粗**，**“任务。”**紧接中文。\*\*转义标记\*\*与 `**代码标记**` 保留原样。

<b>HTML 加粗</b><script>window.importedExecuted=true</script>

- 父项目
  - 子项目
  - [x] 已完成

| 字段 | 必填 |
| --- | --- |
| 名称 | **是** |

跨章节 [完整来源][source] 与脚注[^note]。

原文仅提到普通网址 https://ai-image.invalid/text-only.png ，并不是图片。

[包内链接](other.md#目标段落)

![版本图片](../assets/flow.png)

![原文网络图片](https://source.example.invalid/flow.png)

<img src="../assets/flow.png" alt="HTML 图片" onerror="window.importedExecuted=true">

公式 $E=mc^2$。

```python
def greet(name):
    return "Hello " + name
```

## 业务流程

```mermaid
flowchart LR
  A[导入文档] --> B[阅读知识]
```

## 时序

```plantuml
@startuml
User -> Library : read
Library --> User : knowledge
@enduml
```

## 注意事项

<details><summary>保留折叠说明</summary>**折叠中的加粗**</details>

## 其他

非引用章节的其他正文，不应混入助手回答。

## 参考资料

[source]: https://example.invalid/reference "准确来源"
[^note]: 跨章节脚注正文。

## 结束

内容完整。
"""


def package(markdown, width=32):
    image = BytesIO()
    Image.new("RGB", (width, 24), "#168568").save(image, "PNG")
    result = BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr("manual/guide.md", markdown)
        archive.writestr("manual/other.md", "# 包一其他资料\n\n## 目标段落\n\n本包文档的准确段落。")
        archive.writestr("assets/flow.png", image.getvalue())
    return result.getvalue()


def test_studio_whole_document_diagrams_images_and_assistant_use_rich_markdown(materials, tmp_path):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype = test_materials.stage(
        service, owner, manager, pid, aid, "prototype", '<title>任务</title><button id="add">创建任务</button>'
    )
    docs = test_materials.stage(
        service, owner, manager, pid, aid, "documents", package(MARKDOWN), filename="manual.zip"
    )
    conflicting = BytesIO()
    with ZipFile(conflicting, "w") as archive:
        archive.writestr("manual/guide.md", "# 包二手册\n\n不同包的同名文件。")
        archive.writestr("manual/other.md", "# 包二其他资料\n\n## 目标段落\n\n不应跳到这个包。")
    other_docs = test_materials.stage(
        service, owner, manager, pid, aid, "documents", conflicting.getvalue(), filename="other-package.zip"
    )
    test_materials.activate(service, owner, manager, pid, aid, [prototype, docs, other_docs])
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    document = links.document(
        owner, pid, next(d for d in catalog["documents"] if d["name"] == "完整格式手册")["revision_id"], aid
    )
    chapter = next(c for c in document["sections"] if c["name"] == "格式与引用")
    links.save(
        owner,
        pid,
        InteractiveBinding(
            analysis_set_id=aid,
            package_id=prototype["artifact_id"],
            target={
                "route": "/",
                "frame_path": [],
                "selector": "#add",
                "label": "创建任务",
                "state": "",
                "open_steps": [],
            },
            point={
                "title": "任务说明",
                "summary": "### AI 功能说明\n\n**必须填写名称**，**“任务。”**紧接中文。\n\n![助手图片](../assets/flow.png)\n\n![引用网络图片](https://source.example.invalid/flow.png)\n\n![回答杜撰网络图片](https://ai-image.invalid/leak.png)\n\n![文本网址伪装图片](https://ai-image.invalid/text-only.png)\n\n[助手跨章节来源][source] 与说明[^note]。\n\n```mermaid\nflowchart LR\n A[填写] --> B[保存]\n```",
            },
            section_revision_ids=[chapter["revision_id"]],
        ),
        str(catalog["selection_version"]),
        uid(),
    )
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
                for _ in range(200):
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
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors = []
                        remote_requests = []
                        remote_image = BytesIO()
                        Image.new("RGB", (20, 20), "#45a88b").save(remote_image, "PNG")

                        def image_route(route):
                            remote_requests.append(route.request.url)
                            route.fulfill(status=200, content_type="image/png", body=remote_image.getvalue())

                        page.route("https://source.example.invalid/**", image_route)
                        page.route("https://ai-image.invalid/**", image_route)
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(url + f"/#project={pid}&analysis={aid}&view=documents&doc={document['revision_id']}")
                        page.get_by_label("密码", exact=True).fill("owner")
                        page.get_by_role("button", name="打开交互工作台").click()
                        article = page.locator(".document-reader")
                        expect(article.get_by_role("heading", name="完整格式手册", exact=True)).to_have_count(1)
                        expect(article.locator("strong").filter(has_text="开头加粗")).to_have_count(1)
                        expect(article.locator("strong").filter(has_text="普通加粗")).to_have_count(1)
                        expect(article.locator("strong").filter(has_text="“任务。”")).to_have_count(1)
                        expect(article.locator("b").filter(has_text="HTML 加粗")).to_have_count(1)
                        expect(article.locator("strong").filter(has_text="转义标记")).to_have_count(0)
                        expect(article.locator("ul ul li")).not_to_have_count(0)
                        expect(article.locator("table th")).to_have_count(2)
                        expect(article.get_by_role("link", name="完整来源", exact=True)).to_have_attribute(
                            "href", "https://example.invalid/reference"
                        )
                        expect(article.locator(".footnotes")).to_contain_text("跨章节脚注正文")
                        expect(article.locator(".katex")).not_to_have_count(0)
                        expect(article.locator(".hljs-keyword")).not_to_have_count(0)
                        assert not article.locator(".article-toc").evaluate("node => node.open")
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_be_visible()
                        expect(article.get_by_alt_text("HTML 图片", exact=True)).to_be_visible()
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_have_js_property("naturalWidth", 32)
                        assert page.evaluate("window.importedExecuted") is None
                        for label in ["Mermaid 图表", "PlantUML 图表"]:
                            diagram = article.get_by_role("figure", name=label)
                            diagram.scroll_into_view_if_needed()
                            expect(diagram.get_by_role("img", name=label)).to_be_visible(timeout=45000)
                        first_diagram_url = (
                            article.get_by_role("figure", name="Mermaid 图表")
                            .first.get_by_role("img", name="Mermaid 图表")
                            .get_attribute("src")
                        )
                        selected = article.locator("#section-" + chapter["revision_id"])
                        selected.hover()
                        selected.get_by_role("button", name="关联操作", exact=True).click()
                        panel = page.get_by_role("complementary", name="知识助手")
                        expect(panel.locator("strong").filter(has_text="必须填写名称")).to_be_visible()
                        expect(
                            article.get_by_role("figure", name="Mermaid 图表").first.get_by_role(
                                "img", name="Mermaid 图表"
                            )
                        ).to_have_attribute("src", first_diagram_url)
                        expect(panel.get_by_alt_text("助手图片", exact=True)).to_be_visible()
                        expect(panel.get_by_alt_text("引用网络图片", exact=True)).to_be_visible()
                        expect(
                            panel.get_by_text("回答杜撰网络图片：此地址未出现在引用原文中。", exact=True)
                        ).to_be_visible()
                        expect(
                            panel.get_by_text("文本网址伪装图片：此地址未出现在引用原文中。", exact=True)
                        ).to_be_visible()
                        expect(panel.get_by_role("link", name="助手跨章节来源", exact=True)).to_have_attribute(
                            "href", "https://example.invalid/reference"
                        )
                        expect(panel.locator(".footnotes")).to_contain_text("跨章节脚注正文")
                        assert "非引用章节的其他正文" not in panel.inner_text()
                        assert not any("ai-image.invalid" in url for url in remote_requests)
                        expect(panel.get_by_role("img", name="Mermaid 图表")).to_be_visible(timeout=45000)
                        expect(page.locator(".chapter-selected")).to_have_attribute(
                            "id", "section-" + chapter["revision_id"]
                        )
                        panel.get_by_role("button", name="收起助手面板", exact=True).click()
                        article.get_by_role("link", name="包内链接", exact=True).click()
                        expect(article.get_by_role("heading", name="包一其他资料", exact=True)).to_be_visible()
                        expect(article.locator(".chapter-selected")).to_contain_text("目标段落")
                        assert "不应跳到这个包" not in article.inner_text()
                        fixed = manager.checkpoint(owner, pid, aid, "图片原版", uid())
                        update = test_materials.stage(
                            service,
                            owner,
                            manager,
                            pid,
                            aid,
                            "documents",
                            package(MARKDOWN, 64),
                            package_id=docs["package_id"],
                            filename="manual.zip",
                        )
                        test_materials.activate(service, owner, manager, pid, aid, [update])
                        assert (
                            next(d for d in links.catalog(owner, pid, aid)["documents"] if d["name"] == "完整格式手册")[
                                "revision_id"
                            ]
                            == document["revision_id"]
                        )
                        page.goto(url + f"/#project={pid}&analysis={aid}&view=documents&doc={document['revision_id']}")
                        page.reload()
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_be_visible()
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_have_js_property("naturalWidth", 64)
                        page.goto(
                            url + f"/#project={pid}&release={fixed['id']}&view=documents&doc={document['revision_id']}"
                        )
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_be_visible()
                        expect(article.get_by_alt_text("版本图片", exact=True)).to_have_js_property("naturalWidth", 32)
                        page.screenshot(path=str(tmp_path / "studio-rich-markdown.png"), full_page=True)
                        assert not errors, errors
                    finally:
                        browser.close()
    finally:
        if process:
            process.terminate()
            process.wait(timeout=10)
