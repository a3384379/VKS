# 知境 · Visual Knowledge Studio

VKS 从业务操作连接到文档章节、接口、源码与证据。当前实现推进到 **VKS 1.0 候选版**：功能与本地验证持续记录在 [实施与验收跟踪](docs/ROADMAP_V1.md)，真实试点、独立人工 Gold 和指定基准机门禁单独验收。

当前分支增加 **2.0 开发预览**：五主导航、冰蓝白项目驾驶舱、智能接入向导、不可变业务流程画布、被动状态对比、同版双向知识定位、受限自然语言知识任务、Windows 本机 Agent/授权同步、来源对齐诊断、章节差异和有界知识地图。实际范围、兼容及剩余工作见 [2.0 实施记录](docs/upgrades/v2/IMPLEMENTATION.md)，检查及未验收项见 [2.0 验证记录](docs/upgrades/v2/VERIFICATION.md)。本地合成验证不代表完整 2.0 或生产发行。

React/TypeScript 工作台按项目空间、业务探索、知识构建、变更与版本、项目设置五组导航组织；1.0 的八种旧 hash 地址仍可恢复。所有页面使用持久化 API；模型默认关闭，普通导入、检索、编辑和阅读不依赖模型。

2026-10-31 起采用[自用知识工作流](docs/PERSONAL_WORKFLOW.md)：审批与内容脱敏完整移除，知识直接使用，历史快照仍不可变。

## 启动

需要 Node.js 22.12+、npm 和 [uv](https://docs.astral.sh/uv/)，Python 版本由 `.python-version` 固定。

```bash
make dev
```

打开 [本地工作台](http://127.0.0.1:5173)。初始管理员账号和密码均为 `owner`，使用“团队账户”登录；已有密码不会在重启时被覆盖。也可使用 `.vks/bootstrap-token` 进入初始化管理会话，再在“团队与账户”修改密码、创建用户并加入项目。普通人员使用团队账户登录；机器客户端使用独立项目令牌。

API 位于 [127.0.0.1:8000](http://127.0.0.1:8000/docs)。本地默认 SQLite，数据保存在忽略目录 `.vks/`。`Ctrl+C` 停止开发服务。完整 PostgreSQL/RabbitMQ/Worker 部署见 [部署说明](deploy/README.md)。

开始使用：创建项目 → 导入样例或资料 → 查看动作及证据 → 直接编辑与纠错 → 按需保存知识版本。已发布基线保持不可变，继续工作时先克隆草稿。

文档图表与数学公式的写法、阅读操作和支持边界见 [Markdown 格式说明](docs/MARKDOWN_FORMATS.md)。可将 [图表与公式样例](fixtures/markdown-visuals.md) 导入自己的项目；本地验收实例另有独立的“文档图表与公式示例”项目。

## 已实现范围

| 范围 | 行为 |
| --- | --- |
| 项目与团队 | 项目资料、业务模块树及多对多成员映射，本地团队账户、成员角色、独立敏感权限、禁用/撤销、审计筛选 |
| 资料接入 | ZIP、独立 Markdown/OpenAPI、HTML/图片原型，上传预检、编码/哈希/来源版本和原始字节保留；目录/Git/SVN 只读客户端采集 |
| 静态分析 | Tree-sitter Java 声明/保守调用候选，Spring 条件路由/Feign，MyBatis namespace/id、片段、SQL与表；歧义和动态部分明确报告 |
| 场景与热点 | 不可变 DSL、有限状态/分支、静态安全讲解、DOM/区域热点新修订；已有发布版和原始 HTML 保持不变 |
| 受控采集 | 登记测试/Mock 环境、数据集和固定场景修订，真实 Chromium、出站来源/IP限制、原始页面与截图、来源对齐状态 |
| 文档 | CommonMark/GFM、Mermaid/PlantUML图表、LaTeX公式、稳定章节、原始附件、代码高亮、草稿冲突与人工锁、直接保存并保留来源 |
| 关系与版本 | 独立用户移除/有效性/因果字段，改选/拆分/合并、纠错反馈、移除与恢复，变更传播与来源有效性核对和默认版本回退 |
| 检索与模型 | 按版本/模块/类型/服务过滤、权限内全文/路径/符号检索、局部图；可选带证据引用的模型问答/草稿及项目预算账本 |
| 任务与恢复 | 数据库 outbox、独立 Celery/RabbitMQ 队列、本地兼容调度、租约/检查点/幂等、取消、有限重试、一致备份恢复 |
| 导出与 MCP | 原始/衍生 Markdown、中文 PDF、可回导 VKS 包；官方 MCP SDK stdio/HTTP、原 18 工具及 2.0 新增 5 工具、资源/提示模板、OAuth资源服务器 |

框架支持范围是有边界的。Java 动态分派、运行因果、复杂 React/Vue 映射不会被猜成事实；前端支持显式构建/热点源码锚点。Axure 降级为无脚本、无外联的被动原型。Web 使用本地团队登录，MCP OAuth 对接外部授权服务器；本项目不实现通用企业 SSO 授权服务器。

## 接入自己的材料

在“接入与任务”选择类型并上传，先预检再确认。独立文档和原型写入选定草稿；正式 bundle 的 `project_key` 必须匹配项目，引用与哈希必须闭合。[契约](packages/contracts/README.md) 和 [原始设计样例](docs/design/contracts/examples/README.md) 保持可查。

在仓库可访问的本机采集，服务器不会把客户端路径当作服务器目录：

```bash
PYTHONPATH=. uv run python -m packages.collector --provider git \
  --root /path/to/repository --revision COMMIT \
  --project your-project --baseline baseline-v1 --output /path/to/snapshot.vksbundle.zip
```

`--provider directory` 固定目录内容摘要；SVN 使用数字 revision，并要求 `--allow-svn-root` 的明确范围。采集不 checkout、不 commit、不执行 Maven/项目脚本，排除链接、凭据和常见构建目录。

较大的合成验证基线可在本地明确加载：

```bash
PYTHONPATH=services/api:. uv run python -m scripts.seed_validation --data-dir .vks
```

该项目含30动作、15规范化API、30章节、3类**声明**场景，保持草稿。`scripts/build_selfhosted.py` 从当前源码/OpenAPI重建只读自举包；候选不等于运行事实或人工 Gold。

已接入用户指定的 **FlowTest真实试点**：固定Git原始源码/使用手册、30动作、118章节，源码与运行OpenAPI分别保存。独立合成实例完成页面可读、表单校验、只读权限三条真实采集，并通过MCP SDK读取。源码与运行镜像版本不同，保持未对齐；详见 [FlowTest试点记录](docs/FLOWTEST_PILOT.md)。

## MCP 与可选模型

在“MCP与设置”创建专用令牌，默认只读。读取草稿还需 `knowledge:edit`，源码/采集分别需 `source:read` / `trace:read`。秘密只展示一次，不把浏览器管理员凭证交给客户端。

```bash
export VKS_TOKEN='专用项目令牌'
export PYTHONPATH="$PWD/services/api:$PWD"
uv run python -m vks_api.mcp_gateway
# 可选 Streamable HTTP
uv run python -m vks_api.mcp_gateway --http
```

HTTP 地址为 [127.0.0.1:8788/mcp](http://127.0.0.1:8788/mcp)，同时支持本平台 Bearer 令牌和登记的外部 OAuth JWT。每次请求重新验证发行方、audience、scope、项目授权与撤销；授权客户端可直接编辑知识、保存固定版本和发起采集。详见 [MCP 说明](docs/MCP.md)。

模型支持 Responses/Chat Completions 结构化输出。实例先配置允许的出站来源及专用密钥引用，项目再开启策略、资料出口和预算。导入 manifest 的 `allow_platform_llm=false` 始终禁止发送该材料；无充分且获准证据时不调用模型。所有模型段落必须引用当前可见证据，结果直接保存在当前知识版本。参见 [模型配置](deploy/README.md#可选模型)。

## 验证

```bash
make install
uv run playwright install chromium
make check
npm audit --audit-level=high
```

CI 会安装独立 Chromium，并另跑 PostgreSQL 与 RabbitMQ 专项。Docker/SVN/恢复/容量用例通过显式开关运行，命令与实际结果见 [验证记录](docs/VERIFICATION.md)。原设计44个文件的 SHA-256、OpenAPI/JSON Schema、真实 MCP SDK 同版闭环均在测试中检查。

基于`c5c93ee`的A01–A11修复及G01–G02闭环见[逐项审计交付](docs/AUDIT_REMEDIATION.md)，包含授权快照、生成品来源限制、重导治理、可信讲解、Runner会话轮换、分页与导航、采集查看及绑定。验证记录绑定审计时的本地源码摘要，GitHub交付状态以对应PR及提交检查为准；未重启或验收现有运行实例，未执行真实业务验收。

[实施记录](docs/IMPLEMENTATION.md) 说明模型、身份、版本和任务边界；[设计原件](docs/design/README.md) 与运行实现分开保留。源码托管于 [GitHub · VKS](https://github.com/a3384379/VKS)，远端检查见 [GitHub Actions](https://github.com/a3384379/VKS/actions)。本地数据库、实例凭据和私有试点原始资料不随仓库分发；自动检查与生产部署、真实业务验收分别记录。
