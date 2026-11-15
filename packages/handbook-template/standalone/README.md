# 独立知识手册空白框架

资料管理的“下载手册模板”由这里的静态资产组装，沿用用户提供的《云收费系统项目知识手册》的独立阅读、搜索、图表、AI 和文档更新框架。它没有 Studio 导航或原型页面，也没有原手册的业务正文、图片、目录、语料和向量。

原件解包后的 HTML SHA-256 为 `a6ea8f246240ac23c3760cf246b4e1694f233e76799030d19bc6de99d7b72b09`；原始文件和私有提取过程不进入 Git。`shell.html` 是空白布局，`book.js` / `codec.js` 管理阅读与压缩保存，`assistant.js` 保留原手册 AI / 更新运行时，`mermaid.js` / `diagram.js` 保留图表，`integration.js` 只添加独立文档更新入口。供应商 bundle 中的许可证注释保留，禁止用业务手册正文重新填充这些框架资产。

## 本地使用

下载 HTML 后直接打开，选择“文档更新”，导入含 MD / Markdown / TXT 和位图的目录。再次导入相同路径更新该文档；下载更新后的 HTML 可离线重开，也可以作为知识文档导回 Studio。导入和保存不需要 API Key；可选的向量重建和联网问答使用使用者自己的配置。浏览器目录选择不依赖原手册的本地 Node 伴随服务。

保存文件含版本 `2.0.0` 的惰性 JSON 数据清单，保留 Markdown 原始文本与图片字节。平台仅解析清单，不执行来源脚本。本地设置、历史和文件绑定使用模板自己的缓存名称，不读取旧业务手册的缓存。独立模板及原样只读成品走不同构建路径；只读成品移除原手册更新和 AI 入口，由外层只读助手提供问答。旧版本 `1.0.0` 的可编辑模板仍可导入。

生成入口为 `services/api/vks_api/handbook_authoring.py::template_html`；导回验证为 `template_ingest.py`，专项测试为 `tests/ui/test_standalone_original_template.py`。
