# VKS 3.1 只读接入开发记录

## M0 · 接入基线

2026-11-21 开发基线为 `b26994b`，开始时 `main` 与本地 `origin/main` 一致，工作树为空。相较方案所用的 `e57d025`，已合入 PR #8，涉及批量关联、讲解与成品导出、任务用量、资料侧车及 Studio 定位。已有具体实现影响后续阶段：

- Studio 的书签定位已通过 `target.reveal`，等待指定包的运行实例就绪。后续应以现有代码和回归为起点，补准确目标失败、冷启动与状态验证。
- 私有备份已独立保存向量库中提交的配置和用量，并在恢复时重建缓存。跨平台发行阶段仍需验证目标系统路径、凭据与恢复。
- 业务读取已经过现有 REST 路由。V1 的项目上下文、搜索和证据契约取自 `mcp_tools.json` 及 `openapi_1.0.yaml`。V3 工作区与原型工具取自 `freeze_v3_contracts.py` 与运行模型。构建器合并 V1/V2/V3 再解析引用，网关根据输出 Schema 投影公开字段。
- stdio 和 Streamable HTTP 由 `python -m vks_api.mcp_gateway` 进入同一个 SDK 服务。stdio 的身份来自专用环境令牌，HTTP 每次请求把当前 Bearer 传给 API，API 同时校验项目授权与 scope。`VKS_API_URL` 是网关到 API 的地址，默认 8000。Studio 开发脚本使用 Studio 5193、API 8014、runtime 5195。原型执行由 runtime 的 localhost 及派生 `*.localhost` 来源隔离。
- 保存快照只在项目首次有版本时初始化 `current_release_id`。后续独立查询版本目录，按保存时间及 ID 稳定排序。范围明确后继续走现有冻结清单校验。

### 基线 30 个 MCP 工具

| 实际操作 | 工具 |
| --- | --- |
| 发现和读取项目、图谱、场景或证据 | `list_projects`、`get_project_context`、`search_knowledge`、`get_action_context`、`get_scenario`、`get_evidence`（记读审计）、`trace_business_flow` |
| 读取处理中的任务或已有报告 | `get_job`、`get_knowledge_task`、`get_knowledge_coverage`、`get_change_impact_report` |
| 读取 Studio/原型知识 | `get_interactive_context`（关联与本地候选）、`get_workspace_document`、`get_prototype_action_context` |
| 创建导入任务 | `import_bundle`、`import_documents`、`import_prototype` |
| 创建分析、变更检查、知识生成或采集任务 | `analyze_snapshot`、`detect_drift`、`create_knowledge_task`、`request_capture` |
| 保存关系、流程、文档或版本 | `propose_links`、`propose_journey`、`save_knowledge_snapshot`、`save_interactive_binding`、`save_workspace_document`、`save_business_point_binding` |
| 启动导出或取消执行 | `export_knowledge`、`export_workspace_documents`、`cancel_job` |

### 后续估算与依赖

按同机 Studio 接入开发。M2 检索和质量对照约 2–4 个工作日，需有可验证的已建索引和获授权的嵌入端点。M3 精确链接、定位回执与独立浏览器约 2–4 个工作日。M4 构建、安装、凭据及升级约 4–8 个工作日，另需原生 Mac Intel、Windows 10 与 11 设备及目标客户端，签名与公证时长需单独确认。M5 图片与业务试点约 3–6 个工作日，需 5–10 条获授权、能人工核对的真实历史需求。估算按一名开发人员与可及时获得测试环境计算。

## M1 · 固定保存版本的 MCP 读取

- stdio 与 HTTP 网关接受 `--profile requirements-readonly`。该模式只公开七个读取工具并在调用时强制执行同一白名单，项目发现、首次版本解析和后续按保存版本读取分别校验。全功能入口默认使用 `full`。
- `get_project_context` 支持 `release_selector: "latest_saved"` 或明确 `release_id`，返回含保存时间和冻结清单摘要的 `scope`。版本目录依据当前项目读权限提供版本，正文和关系继续逐项应用来源、证据与当前权限。相同保存时间按版本 ID 降序保序。
- 搜索结果中可核验的章节带准确父文档与章节修订，`get_workspace_document` 接受同一文档和章节修订列表。只读模式要求每次提供 `max_chars`，返回 `sections` 文本片段和可选 `next_offset`。续读沿用同一范围和章节集合，偏移以按文档顺序拼接当前可读章节的 Unicode 字符计算。历史章节映射不明确时标记 `version_only`，有基础地址时附上对应版本的文档目录。
- `get_evidence` 在只读模式要求命中指定清单中当前可见、未移除关系的证据。无范围的旧调用依旧使用既有规则。证据读取仍写读审计。
- 文档及现有 HTML 绑定按准确选中版本生成定位器。绑定分别返回物理包 ID、逻辑包 ID 和包根修订 ID，旧包缺少根身份时省略对应值。定位器摘要沿用现有修订或制品摘要。服务器设置 `VKS_STUDIO_BASE_URL` 后生成 Studio 链接，开发脚本按实际 Studio 端口设置。允许 HTTPS 来源和回环 HTTP 来源。链接本身不承担授权。
- 原样文档帧在接收到 Escape 时通知宿主关闭所选文字的菜单，保留原文档自身的键盘事件。
- REST 和 MCP 的原有字段仍可使用。新投影与参数在运行契约中冻结，本阶段无数据库迁移。

stdio 示例使用本次进程环境中的专用 `VKS_TOKEN` 和 `VKS_API_URL`。开发 Studio 的 API 默认在本机 8014 端口。

```sh
PYTHONPATH=services/api uv run python -m vks_api.mcp_gateway --profile requirements-readonly
```

HTTP 可用相同 profile 启动独立网关及端口。为 OAuth 配置该实例准确的 `VKS_MCP_RESOURCE`、API 侧 audience 与受保护资源元数据。

```sh
PYTHONPATH=services/api uv run python -m vks_api.mcp_gateway --profile requirements-readonly --http --port 8788
```

## M2 · 共享知识检索

- `search_knowledge` 缺省沿用原有字面检索。显式 `keyword` 使用现有章节 BM25，`hybrid` 和 `auto` 在项目已启用嵌入且存在可用版本向量时融合关键词和语义排名。Studio 手册搜索调用同一服务的关键词路径。
- 分词和向量排序只读取当前用户可见的章节。检索固定保存清单或授权草稿，当前过滤项先于召回，章节归属由准确文档修订校验。向量空间按项目、端点、模型和维度隔离。向量行需对应准确章节的分片 ID、文本和偏移。结果包含保存范围、实际模式、降级原因、准确引用、摘要截断状态。
- 摘要来自关键词或向量命中的原文窗口。增强模式默认 8 项、最多 20 项、每文档最多 3 个章节命中。查询最多 1,000 字符，单项摘要至多 1,200 字符，合计至多 6,000 字符，data 数组另受 56 KiB JSON 上限。需要完整公式、表格或条件时按定位器续读原章。
- 查询外联仅发送查询文字。冷查询可能写查询向量缓存和用量记录。外联前后重验权限、版本和模型设置，已连接的嵌入请求在超时或客户端断开时停止。请求总等待上限为 15 秒，嵌入阶段最多等待 5 秒。嵌入不可用时回退关键词；可用的部分索引保留混合召回并标记范围未齐。检索沿用现有索引，资料变更和建库由既有维护入口负责。

## M3 · Studio 精确导航

- Studio 将链接解析、导航代次和结果回执用于外部链接与站内导航。外部保存版本目标经登录和当前权限读取，章节使用准确修订 ID。无法读取目标时保留链接，提供重试与版本目录入口。可复制已定位的保存版本链接。
- Studio 跟随外部保存版本链接时复核当前浏览器会话，鉴权请求返回 401 时同样复核。确认期间隐藏工作区，失效后释放旧会话的文档和原型视图。重新登录沿用当前链接重新读取，旧会话响应不能用于新工作区。
- 原型绑定先在该版本中匹配可读关系的物理包，再等待该包的独立 runtime。定位结果核对请求、runtime 实例、页面、frame 和控件。可观察的弹窗状态相符时才报告状态已核验。无法还原状态时展示实际可见级别，可显式仅打开页面。运行时回执仅描述浏览器所观察的 DOM。
- 文档读取以章节进入视口为准。原样文档与原型的慢回执随新导航失效。已登记的 HTML `page_ref` 由服务端解析为该包的默认页面，Studio 无需关联即可打开。站内原型包与文档的草稿导航继续沿用既有入口。原型包的保存字节和版本不变。

## M4 · 薄连接器、插件和原生启动器

- `packages/vks-connect` 使用 Python 3.12，从 MCP SDK 调用现有 VKS API。独立 wheel 内的契约由同一份 resolved JSON 生成。旧 `python -m vks_api.mcp_gateway` 加载仓库实时契约并调用同一套网关实现，保留全功能和只读 profile。现有 Compose 仅通过显式选项保持容器私有网络内的 API HTTP 跳转。
- `plugins/vks` 保存一份 portable manifest 和需求分析 Skill。冻结包由原生机器生成平台对应的 `mcp.json`，通过插件目录内的 `./bin/vks-connect` 或 `./bin/vks-connect.exe` 启动连接器。Skill 默认明确使用关键词模式，符合用户授权和实例配置时可使用混合检索。
- 插件的 `--codex-sidebar` 注册全局 MCP App 入口，点击左栏打开全屏知识检索页。面板通过现有工具读取可访问的项目，解析并固定保存版本，用关键词检索章节。结果进入视口后按原文定位器分页读取章节，复用离线 Markdown 和 Mermaid 渲染器。图片只经连接器读取当前保存版本的授权栅格附件。点击 Studio 链接时连接器把已配置的 Studio 首页或章节定位交给系统浏览器。启动和附件只在左栏模式注册，界面资源没有网络白名单，凭据由原生连接器持有。
- rc.4 的插件默认启动 `mcp --codex-library`，通过系统文件选择器或 `library use` 持久化用户选择的成品库路径。连接器解析 HTML 中的惰性 gzip 数据，按文件 SHA-256 固定快照并提供本地关键词搜索、章节续读、包内图片和已有原文关联。文件切换后，旧指纹拒绝继续查询。章节链接使用成品阅读器已有的 `#section=` 路由，由系统浏览器打开用户选定的本地文件。
- rc.5 读取成品库现成的 float32 向量，排除缺失、维度错误及不属于该成品正文的片段。显式在成品库面板配置的独立嵌入连接只发送查询，按文档向量的模型和维度验证响应。语义与关键词候选通过倒数排名合并，沿用保存章节的原文、关联和文件书签。查询返回实际使用的方式及降级原因。模型工具用 `retrieval_mode=keyword` 可强制单次本地查询。
- rc.6 默认在本机使用 `local_rag` 和关键词混合检索，旧成品或模型不可用时回到关键词。已设置的云端检索需要单次显式选择。原生包携带固定版本及 SHA-256 校验的中文 BGE 模型、分词器和 CPU ONNX Runtime。离线导出转换已有 HTML 的数据区，沿用原标题、原文、章节和关联，允许按已有 CommonMark 分段在独立栏目添加补充 PRD。导出的原快照 ID、清单和历史云端索引原样保留，新本地索引标识原成品的 SHA-256。
- Studio 的资料管理默认提交 `local_vectors` HTML 导出。导出任务使用与插件和离线转换脚本相同的模型和分片方式，仅为当前保存版本中获准打包的章节计算本地向量。生成前后复用冻结版本和权限复核。原文、原型和已有关系仍沿用同一导出投影。Studio 的这条路径无需启用云端嵌入设置；其他既有调用保持原有默认。安装步骤和 API／Worker 镜像预置经过摘要校验的模型文件，工作进程在本机推理。
- 同一保存版本再次导出时，当前可读内容、依赖、权限和阅读器一致即可复用已校验的成品文件。新版本从近期导出中匹配相同文档标题、章节标题和正文的向量，给新版本写入当前章节引用。备选成品缺失、损坏或模型不匹配时重新计算所需片段。两条路径均复核当前权限，并通过任务阶段显示读取、检查成品、建库、打包和校验进度。
- `configure` 保存非秘密地址和默认项目；系统密钥库按连接、端点和用途区分凭据。无法使用系统密钥库时可明确选择当前会话方式。连接器拒绝远程明文地址、带凭据的 URL 和 API 重定向。HTTP MCP 的浏览器侧 OAuth 或会话 bearer 由直接连接的客户端处理。
- `doctor` 核对 API、Studio 代理和已选 HTTP MCP 的实例标识，并检查已配置项目的准确保存版。Runtime 就绪与页面定位由独立步骤验证。纯 MCP 配置可生成 TOML 或可移植 JSON。对比现有 TOML 时可检查 VKS 服务器及已启用插件冲突，只输出待合并片段。
- 原生开发启动器 `scripts/dev_studio.py` 通过 macOS Shell 和 Windows PowerShell 薄脚本启动三个 loopback 服务，以本次启动标识检查就绪，退出时收尾自己创建的进程。Runtime 来源密钥和模型凭据文件通过同一私有文件读写接口校验。POSIX 校验所有者、文件类型和权限。macOS 还通过文件描述符检查扩展 ACL 和所在卷的所有权设置。Windows 使用原生 ACL 与重解析点检查。
- 原生构建配置包括 macOS arm64、macOS x64 和 Windows x64。发行压缩包带版本、契约摘要、依赖许可证和 SHA-256 文件。当前本机包是工作树验证快照。安装、诊断、升级、卸载和 RAG 恢复说明见[连接器安装与诊断](连接器安装与诊断.md)。

## M5 · 已授权原型预览

- 项目上下文返回最多 20 项可读原型页面。可用 `prototype_page_query` 筛选标题，续页使用 `prototype_pages_next_offset` 并保持原保存版本。HTML 页面由已验证包内清单登记，引用使用不透明的 `page_ref`。具备已登记快照的旧版栅格页面从同一页面修订的默认状态取图。
- 「更新当前页预览」在 Studio 确认当前页面后可主动发起。服务验证当前账号的 `capture:run` 权限、选定包与页面归属，采用现有持久化任务、幂等键和取消机制。每个项目同时生成一张图。已有绑定的状态由运行时回执验证。状态无法准确复现时任务返回失败。无绑定时从独立会话打开页面的默认状态。
- 采集复用 Studio 运行时和既有 Chromium 沙箱，使用每次任务独立的浏览器上下文、虚拟来源和 1280×800 视口。浏览器路由只供应固定运行时资源与经过哈希校验的原型字节，阻断外部请求和 WebSocket。截图前后只观察页面文档身份和状态。期间切换页面、重载同页或改变可观察状态时拒绝本次截图。任务执行和写回均复核身份与来源。
- 生成的 PNG 是带来源元组、视口、生成器和摘要的衍生制品，使用与导入资料分离的制品命名空间。新来源的查找条件包含物理包、根修订、摘要、登记页和已验证绑定状态。衍生对象随既有私有备份纳入校验，直接文件下载需要走预览读取的版本与权限校验。保存预览与知识修订、关联、版本清单分别持久化。
- `get_prototype_preview` 接收保存版本的页面或绑定定位器。它读取已生成图，输出文本和至多一个标准 MCP ImageContent。缩略图最多 1280×960、二进制最多 160 KiB。无图时返回原因和可用 Studio 链接。旧栅格提供已登记画面，HTML Studio 链接仅在准确运行包可用时生成。
