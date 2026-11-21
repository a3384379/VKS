# VKS 3.1 只读接入验证记录

测试日期为 2026-11-21。基线 `b26994b`，开发分支 `codex/vks-3.1-readonly-mcp`。全部项目和材料为隔离目录中的合成数据。

## M0 · SDK 与宿主环境

- macOS 26.6.2（25G83）、arm64、Python 3.12.13、MCP SDK 1.30.0。基线 `tests/mcp/test_sdk.py tests/mcp/test_http.py` 两项通过。stdio SDK 成功初始化并获取 30 个工具、4 个资源模板和 4 个提示，未配置凭据时 `list_projects` 返回认证错误。Streamable HTTP SDK 使用合成项目令牌获取 `data: [{id, key, name, …}], meta: {request_id, …}`，撤权后请求得到 401。获授权的业务调用读取了项目列表。
- ChatGPT 桌面版实际安装版本为 26.1111.71025，CLI 为 0.169.0-alpha.11.1，本机检查了 `codex mcp --help` 和 `codex plugin --help`。官方文档说明桌面版和 CLI 具有插件入口，IDE 可直接配置 stdio 或 Streamable HTTP。独立的产品接入测试仍需在各宿主内验证工具调用、链接打开和图片呈现。 [插件支持](https://learn.chatgpt.com/docs/plugins) · [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- 按官方打包文档记录 portable manifest 和 `${PLUGIN_ROOT}` 的适用条件。发行包仍需在选定客户端中验证路径替换和本地进程启动。 [插件包装](https://developers.openai.com/plugins/build/plugins) · [桌面本地 MCP](https://developers.openai.com/plugins/deploy/desktop-only)
- 在隔离临时目录中验证 macOS 创建 runtime 密钥时模式为 0600，改为 0644 时读取被拒绝。同机直接验证 `O_NOFOLLOW` 拒绝符号链接。Windows 的 ACL、重解析点、原生进程及签名均待设备验收。测试矩阵沿用[跨平台与真实使用验收清单](跨平台与真实使用验收清单.md)。

## M1 · 合成链路

```sh
make contracts
VKS_DATA_DIR=$(mktemp -d) uv run pytest -q tests/contracts/test_contracts.py tests/mcp \
  tests/interactive/test_project_tools.py tests/interactive/test_exact_binding_navigation_browser.py
```

- 专项 58 项通过。官方 MCP SDK 使用实际 stdio 子进程和回环 HTTP 网关访问隔离 REST API。新字段从 `structuredContent` 读取，涵盖保存版本 scope、父文档和章节修订、分页原文、绑定定位器与 Studio URL。
- 合成验收验证强权限令牌不能调用隐藏写工具或读取草稿，资源和提示不能旁路。可用只读令牌成功读取。测试覆盖缺失、撤销凭据、跨项目、跨版本证据、同时间排序、无保存版本和显式旧版。兼容回归包含原 30 工具链路和原请求格式。
- 契约验证包括 OpenAPI 验证、工具名唯一、生成源与冻结文件一致及 44 份原始设计文件校验。交接包中的 12 个文件按字节归档。
- 独立 Google Chrome for Testing 156.0.8078.4 使用实际生成的两个链接。冷启动文档链接经过登录抵达准确章节，章节进入视口。另一个链接定位至两个同类原型包中的指定版本，准确打开弹窗并聚焦目标控件。
- 完整检查中原样文档的 Escape 关闭菜单断言失败。把按键直接发送至文档帧后在基线工作树复现，确认帧端缺少转发。修复后文档助手和准确定位专项 14 项通过，`node --check apps/runtime/bridge.js` 通过。
- 模块、权限、读取性能和 V3 原型工作流另行通过 30 项回归。
- `npm test` 88 项通过，`npm run build` 生成 Reader 并完成 Web、Studio 生产构建。
- 首轮 `make check` 的 Python 阶段为 1,585 通过、16 跳过和 1 项 Escape 失败。全量启动后的修订由上述专项覆盖，前端测试和构建单独运行。

## M2 · 检索回归和延迟抽样

```sh
uv run pytest -q tests/interactive/test_knowledge_search.py tests/interactive/test_document_presentation.py \
  tests/interactive/test_rag_export.py tests/interactive/test_native_vector_search.py \
  tests/modules/test_mcp_modules.py tests/performance/test_reading.py tests/mcp/test_requirements_readonly.py
```

- 专项覆盖真实 stdio 子进程及 Streamable HTTP 上的官方 MCP SDK，使用只有项目读取权限的令牌。合成资料验证长中文问句、文末命中、公式与表格、同名章节、文档类型过滤、固定旧版、摘要预算、未配置嵌入、空索引、部分索引、失配分片、模型和维度切换、模拟 429、外联期间凭据撤销及文档权限收紧。回环 HTTP 嵌入端点在超时和客户端断开后均观察到连接关闭。检索前后知识修订和任务数量一致。
- 端到端抽样在 Apple M4、16 GiB、macOS arm64、SQLite 上执行。使用 `tests/interactive/test_knowledge_search.py` 的合成语料和 64 维向量，1 份文档、6 个章节、10 个分片。单客户端向独立回环 Uvicorn 实例的 `/api/v1/projects/{pid}/search` 顺序发起真实 HTTP 请求，包含鉴权、检索和回包。冷查询使用 30 个不同查询文本，热查询重复已缓存文本。嵌入由回环 HTTP 桩返回确定的 64 维向量。

| 路径 | 请求数 | P95 | HTTP 嵌入调用 |
| --- | ---: | ---: | ---: |
| Legacy 长中文问句 | 30 | 3.3 ms | 0 |
| Keyword 长中文问句 | 30 | 6.1 ms | 0 |
| Hybrid 冷查询缓存 | 30 | 19.4 ms | 30 |
| Hybrid 热查询缓存 | 30 | 9.9 ms | 0 |

- Legacy 对长中文问句返回 0 项，以准确短语检索时其摘要仍漏掉文末的「逐笔确认」。Keyword 返回「服务条款」及命中窗口。英语改述 `bulk waiver` 在关键词模式没有命中，确定性向量桩使 Hybrid 召回同一条款。该抽样验证模式切换与缓存路径，实际相似度质量、真实嵌入时延、业务数据规模及高并发容量需现场基准与人工对照。

## M3 · Studio 浏览器验证

在 macOS 26.6.2（25G83）arm64 使用本机 Google Chrome 157.0.8103.79、独立浏览器上下文、独立 SQLite 和随机回环端口。TypeScript 编译、`node --check apps/runtime/bridge.js` 及 `git diff --check` 通过。

```sh
VKS_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  uv run pytest -q tests/interactive/test_exact_binding_navigation_browser.py \
  tests/interactive/test_native_documents_browser.py tests/interactive/test_document_navigation_browser.py \
  tests/interactive/test_browser.py tests/interactive/test_studio_bundle.py \
  tests/interactive/test_runtime_browser.py tests/interactive/test_runtime_inventory_browser.py \
  tests/interactive/test_runtime_inventory_audit.py
```

上述专项 29 项通过，用时 141 秒。

- 观察 MCP 同源处理器生成的文档和绑定链接分别从无会话开始，登录后准确章节进入视口，目标原型弹窗可见并聚焦控件。验证复制地址的项目、保存版本、包和绑定参数与服务端链接一致。两包共用同一组 selector 和控件文案，实际内容及当前包均与指定包相符。
- 同一路径、frame 和控件存在两种可观察弹窗状态。切换链接及浏览器后退打开相应真实状态。未知状态显示控件可见，用户可仅打开页面。测试延迟一个包的内容读取并切到另一包，在旧请求放行后仍显示新包，再用后退恢复旧绑定。运行页面的派生 `.localhost` 来源和 Studio 端口独立。
- 旧链接在保存新版本后仍打开旧版规则和默认页。目标未知、显式错包、重复参数以及浏览器帐号撤销资料权限时保留输入目标并显示失败。有效的登记 `page_ref` 经浏览器打开无绑定页面，前后版本分别显示对应 HTML。合成截图写入 pytest 临时目录中的 `exact-section.png`、`exact-binding.png` 和 `exact-unbound-page.png`。
- 会话回归分别在热工作区清空浏览器 Cookie、以及目录读取后撤销发起原型文件读取的会话。独立 Chrome 验证复核期间旧正文和原型帧退场，重新登录恢复准确链接，迟到的旧会话 401 不影响新会话。精确链接、原样文档、文档导航、原型交互及关联管理模块共 14 项通过。
- 标准阅读与手册模式由章节打开原型时保留助手收起状态。通过「返回文档」或浏览器后退均恢复阅读滚动位置。控件可见但状态无法核验时，助手仍使用用户选中的保存关联作为知识来源。章节入口、原型控件助手与精确链接三模块共 14 项通过。
- 在独立 Runtime 中复测 Markdown 合成手册。两个资料包有同名文件时，阅读器仍打开链接来源包的文档，并把中文标题锚点解析为准确章节修订。三路预扫描通过实际 iframe 地址验证独立来源和存储，停止后没有迟到写入。
- Safari 26.6.2（21624.5.1.11.3）的原生 WebDriver 独立会话创建请求被拒绝，本机未启用远程自动化。Safari、Edge、Intel Mac 和原生 Windows 浏览器交互待现场验收。 [Apple Safari WebDriver 设置](https://developer.apple.com/documentation/safari-developer-tools/macos-enabling-webdriver)

## M4 · 发行包、凭据与启动器

```sh
uv run --frozen python scripts/build_vks_connector.py --native macos-arm64
uv run --frozen pytest -q tests/mcp tests/models/test_model_credentials.py tests/models/test_model_connections.py tests/recovery/test_backup_restore.py tests/contracts/test_contracts.py
```

- macOS 26.6.2（25G83），原生 arm64，Python 3.12.13。临时目录里的独立 wheel 环境与解压后迁移位置的冻结包均通过官方 SDK 的初始化、工具枚举、只读调用和拒绝隐蔽写入。检查包括无 shell PATH、非 UTF-8 模式、中文与空格路径、约 200 字符配置目录、配置差异不回显凭据、拒绝重复注册。wheel 和源码包里的 MCP 契约与仓库生成文件逐字节一致。插件及 MCP 清单通过 Agent Plugins 1.0 官方 JSON Schema。
- 跨平台启动器在本机临时数据目录及三个临时端口完成正向检查。真实 API 和 Studio 代理返回同一实例标识，Runtime 使用相同的启动标识。冲突测试保留原监听套接字，正常退出回收三个子进程。以生产构建进行预览时，在 Chrome 实际打开临时端口的原型运行实例并完成登记。官方 Streamable HTTP SDK 连接到隔离 VKS，诊断读回指定项目的准确保存版，并识别另一套 Studio 实例。
- 上述相关 Python 回归 46 项通过、1 项跳过。跳过的是只在隔离 Windows runner 执行的 Credential Manager 往返测试。本机文件回归验证了 Runtime 来源密钥稳定性、拒绝公开权限、符号链接、容量限制和原子替换。备份/恢复合成测试保留隔离副本的 RAG 配置与用量账本。预览读取另测超限流在 JSON 解析前中断并关闭。
- Native 包的 Mach-O 架构为 arm64，`codesign --verify --strict` 通过临时签名校验，发行公证待验。本地构建基于 `b26994bb6822b25590d2ad61d1896b05c691da84` 的未提交工作树，冻结契约 SHA-256 为 `4e68d08e8ef5dc4bcafc5da04b0447ce1fe2e864e49a20ab090c7f6d588bb91d`。
- macOS arm64 的合成私有文件回归验证了 `0600` 文件附带 `everyone allow read` 时拒绝读取与替换。继承的限制性 ACL 和授予当前用户的 ACL 可正常使用。正常系统卷的临时文件可以创建和读取，启用「忽略此宗卷的所有权」的雷电卷拒绝读取与创建，且没有残留新文件。Apple SDK 校验了 arm64 与 Intel 的文件系统结构布局及调用符号。Intel 原生运行仍待验。

| 本地构建产物 | SHA-256 |
| --- | --- |
| `output/vks-connect/vks-plugin-3.1.0-rc.1-macos-arm64.tar.gz` | `9907e35ee900932011bd2b695f9c6948f56912d208e18a2448e48a1e625a6e75` |
| `output/vks-connect/vks_connect-3.1.0rc1-py3-none-any.whl` | `671cb4b07003a6dedc317079e25749192f9d24b1c92aca7ccb6390f6c117ce7c` |

| 首发组合 | 当前证据 |
| --- | --- |
| macOS arm64 | 26.6.2 上已验证 wheel、冻结包、官方 SDK、本机启动器、个人本地插件安装与 Keychain 读取及原生 Codex 工具调用。桌面目录显示、升级及最低系统版本待验。 |
| macOS Intel x64 | 构建/测试 CI 已配置，未运行。Intel 实机、签名、宿主和浏览器待验。 |
| Windows 10 22H2 x64 | Windows x64 构建/测试 CI 已配置，未运行。该系统的普通用户 ACL、Credential Manager、原生 Codex、浏览器和升级待验。 |
| Windows 11 x64 | 同一 x64 安装包需要在 Windows 11 独立执行上述验收。 |

首轮本机 Codex CLI `0.169.0-alpha.11.1` 使用进程临时配置加载已迁移的原生包，并在 App Server 隔离会话中枚举 8 个只读工具，实际调用 `get_project_context` 与 `get_prototype_preview`。首轮使用隔离实例与临时配置。IDE 接入、企业代理与 CA、Windows Credential Manager、离线升级及卸载行为待验。

- 后续本机接入通过 `codex plugin add` 完成个人本地市场安装。此版本宿主拒绝 portable `mcp.json` 中带占位符的 stdio 命令。改用包内 `./bin/vks-connect` 后，App Server 从已安装插件枚举 8 个工具，读到 macOS Keychain 凭据，并完成发布版本、关键词检索与原文工具调用。重建后的包通过官方 SDK 冒烟与 SHA-256 校验。
- rc.2 增加左栏的全局 MCP App 入口。原生包使用官方 MCP SDK 验证 UI 专用工具、资源读取、零参数启动及多余参数拒绝。独立 Chromium 验证 390 px 视口、沙箱消息、固定版本检索、文本处理及 Studio 链接请求。构建后的 macOS arm64 包 SHA-256 为 `08fa81409654e72b03445f9842e2a4835351a1387e7e426d112b8f882be58d34`。
- rc.3 的 macOS arm64 原生包通过官方 SDK 冒烟。隔离 Codex App Server 会话加载左栏资源并读取真实保存版本。独立 Chromium 将搜索结果中的 Mermaid 流程图绘制为图片。已安装连接器将 Studio 首页和原文章节交给本机浏览器打开，章节回执为「已定位到指定章节」。合成回归覆盖章节分页、粗体、列表、表格、授权相对路径图片、外部图片阻断及启动失败后显示地址。
- rc.3 的 `make check` 通过。Python 1639 项通过、18 项跳过，前端 88 项通过，Web 和 Studio 构建成功。隔离资料包经原生连接器验证 `knowledge:read` 只读令牌可以读取授权保存版本的 PNG，并拒绝其他项目的同名图片。
- rc.4 的 macOS arm64 冻结包通过已安装的 Codex App Server 枚举成品库入口，通过官方 SDK 读取实际成品库及其保存关联。独立 Chromium 完成检索和原文展示。离线浏览器加载原文链接后定位成功，期间 HTTP 请求为 0。以命令行设置隔离环境中的所选文件，另行编译检查 macOS 文件选择器。
- rc.5 的 macOS arm64 原生包通过官方 MCP SDK、实际嵌入端点和模型维度匹配的既有索引完成混合检索，回读命中章节和保留的已保存关联。
- 将补充需求导入隔离副本后，重用原有未变化章节的向量，新增需求走关键词分支。真实嵌入端点与无 Key 配置均能检索到对应原文。独立浏览器验证两个原文链接准确定位。
- rc.5 的 MCP、契约与导出回归 75 项通过、2 项跳过。冻结包通过官方 SDK 冒烟及 macOS 签名和 SHA-256 校验。Windows 实机未验证。
- rc.6 的实际项目离线试点逐项比对输入和输出中的原文对象、原章节、保存关联、旧索引、原型、快照及补充 Markdown 字节。新增本地向量索引后上述数据一致。
- 使用原生 macOS arm64 包及无凭据配置，通过真实 MCP SDK 和独立 Chromium 验证左栏默认使用本地向量。补充需求中的数量边界与联动规则可检索并核对原文。改写问法的排序随检索方式和入库范围变化。
- 独立浏览器用四个不同的试点章节链接打开相应正文。原文打开期间没有外部 HTTP 请求。Windows 和 Intel Mac 实机待验。
- 新启动的原生 Codex 宿主从已安装插件调用默认本地检索，四条关键词和语义查询均在首条命中目标。原生发行包的 SDK 冒烟验证随包模型可实际执行推理。
- 隔离数据目录下的 `make check` 通过。Python 1,647 项通过、18 项跳过，前端测试和 Web、Studio 构建通过。
- 从日常 Studio 的「导出当前成品库」创建新保存版本并下载 HTML。下载文件与服务端保存对象的指纹一致。原文、有效关联与保存版本核对一致，本地向量覆盖全部导出章节。导出、契约及连接器专项 57 项通过、1 项跳过，前端测试 88 项及 Studio 构建通过。
- 增量导出用同机的实际项目隔离副本对照。重复导出同一保存版本从 173.85 秒降至 6.26 秒，文件 SHA-256 一致且向量计算为 0。仅修改一章的新保存版本从 171.10 秒降至 11.87 秒，复用 13,678 个向量并新算 1 个。新旧实现对同一新版本输出的 245 份文档、6,690 节原文、257 条关联及其他非向量字段逐项一致。浏览器回归验证建库过程中显示阶段和非零进度，并在完成后提供下载。
- rc.6 左栏以本地向量检索新导出的真实保存版本，两个主题均命中对应原文，并核对了引用中的文件和章节。后续日常点击发现系统文件打开可能丢失章节锚点。rc.7 让浏览器从一个临时本地 HTML 导航到完整原文地址，保留原成品库。新增独立 Chromium 回归覆盖从两条启动链接打开同一文件的不同章节。

```sh
VKS_CODEX_HOST_PACKAGE="$PWD/output/vks-connect/vks-plugin-3.1.0-rc.1-macos-arm64.tar.gz" \
  VKS_DATA_DIR=$(mktemp -d) uv run pytest -q tests/mcp/test_codex_host_acceptance.py
```

## M5 · 预览与合成需求对照

```sh
VKS_DATA_DIR=$(mktemp -d) uv run pytest -q -s tests/mcp/test_prototype_previews.py tests/mcp/test_vks_31_pilot.py
```

- macOS arm64 真实隔离 Chromium 生成默认页及已核验弹窗状态两张 PNG。通过官方 MCP SDK 的 stdio 与回环 Streamable HTTP 从只读 profile 取回，检查像素、摘要、尺寸、包归属、定位器和单一图片块。测试还覆盖无图、篡改摘要、不可读来源、普通文件下载入口、幂等、取消及禁止机器令牌创建预览。合成原型的资源及脚本尝试访问回环验证服务器，服务器收到零请求。
- 旧栅格按同一保存版本的原型修订与默认状态读取已登记 PNG。以另一条真实读取路径验证授权、摘要和缩放。栅格图与 HTML 采集采用不同的来源记录。
- 浏览器采集只提供当前生成器与相同来源的 PNG。更早的生成器可保留旧对象，读取该目标时结果为无图。合成测试修改生成器身份并核查回包。
- 额外的隔离 Chromium 回归覆盖截图期间换页、重新打开同一页面及同页弹窗变化。原先的复核对这三种情况均接受截图。改用截图前后只观察后，三种情况均拒绝。另核对透明祖先内的控件、透明对话框和 `visibility: collapse` 控件不会干扰可见目标核验。本次预览模块共 6 项通过。
- 合成对照固定一个保存版本及五条需求，内容包括滞纳金豁免、VAT 分摊、发票冻结、日终重复记账和文末公式表格。每条请求经官方 stdio SDK 搜索，命中后三个以内候选中查找预登记章节，再逐一读回原文核验规则编号或公式。无 VKS 对照中可以核验的 VKS 引用为 0。Legacy 核验 2 条准确原文、1 条摘要直接显示依据，使用 7 次工具调用。Keyword 核验 5 条准确原文、4 条摘要直接显示依据，使用 10 次工具调用。一次本机样本中，从调用搜索到核验原文的成功请求耗时约 32–46 ms。
- 本轮按用户确定的范围完成合成对照，采用程序化核验。人工找资料用时、模型回答质量、token 用量及真实历史需求试点单列待验。
- 本机原生 Codex 通过隔离 App Server 会话收到来自已登记旧栅格的单一 PNG 图片块和准确保存范围。模型是否读取图片、聊天界面内联显示以及从各客户端点击链接，分别待验。M3 已从无会话浏览器实际打开合成链接。

## 环境验收

- PostgreSQL 专项没有配置 `VKS_TEST_POSTGRES_URL`，本机 Docker 守护进程未启动，数据库实测待验。
- 真实业务只读试点按本轮范围单列待验。各平台发布以实际接入、安装和签名记录为准。

## 独立审计与修复

| 复现的问题 | 修复和验证 |
| --- | --- |
| OAuth 令牌在外联期间过期或签名密钥轮换，查询仍可返回资料 | 出站查询阶段和回包前重新确认凭据及授权。测试撤权、令牌过期、密钥轮换和权限变化。 |
| MCP 客户端断开后，嵌入请求仍在运行 | Streamable HTTP 断开信号传播到上游。用实际套接字验证嵌入连接在预算耗尽前关闭。 |
| 最新版本选择命中全不可读或已清除内容，目录暴露隐藏主包标识 | 仅选择至少含一项可读实质内容的版本，移除不可读的主包标识。另验旧版及部分授权。 |
| 大量短章节分页、长章节绑定返回超过 MCP 回包预算 | 按 JSON 字节预算分页原文。关联查询保留准确绑定、Studio URL 和原文章节身份，可按引用单独读取章节。 |
| 中文摘要取最早分散命中，错过后段集中规则 | 在多个命中区间中选择密集原文窗口。用同一章节中早段分散词及后段规则验证。 |
| 外部链接切换时，已失效会话的 Studio 内容仍可见 | 复核会话时隐藏旧视图，重新登录沿用链接定位，较早会话的迟到响应失效。 |
| 截图期间页面切换或弹窗变化仍被报告为准确画面 | 截图前后对比只读观察回执、文档身份和状态。实际 Chromium 验证换页、同页重载、弹窗变化以及不可见控件。 |
| macOS mode 0600 仍可能带开放 ACL，卷可能忽略所有权 | 在同一已打开描述符复核 ACL 与卷所有权。系统卷和独立外接卷临时文件分别验证读写通过与拒绝。 |

## 最终回归方式

开发期间启动的 `make check` 在 Python 阶段报告 1,607 项通过、17 项跳过、9 项失败。后续专项覆盖了并发章节索引、关联列表后台刷新、章节返回、并行预扫描、Markdown 内链、选中文档缓存、关联助手及连接诊断。最终按仓库 CI 分片在稳定工作树上各运行一次完整集合，共收集 1,651 项。

| 分片 | 通过 | 环境跳过 |
| --- | ---: | ---: |
| `core` | 821 | 18 |
| `interactive-a` | 374 | 0 |
| `interactive-b` | 297 | 0 |
| `ui` | 141 | 0 |
| **合计** | **1,633** | **18** |

```sh
VKS_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  VKS_DATA_DIR=$(mktemp -d) uv run pytest -q -p scripts.pytest_ci_shards --vks-ci-shard core
```

其余三组使用 `interactive-a`、`interactive-b` 和 `ui`。四组 Python 回归、`npm test` 的 88 项及 `npm run build` 共同覆盖 `make check` 的测试与生产构建。`make contracts` 已通过。原生 Codex 宿主专项另行启用并通过 1 项。冻结包与 wheel 的 SHA-256 清单校验通过。
