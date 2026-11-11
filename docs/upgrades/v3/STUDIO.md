# VKS 3.0 Studio 首个交互闭环

2026-11-11 · 对应 [D2 方案](VKS_3.0_交互工作台重构开发方案.md) 的 P0 及部分 P1。当前入口为新建的 `apps/studio`；浏览器原始资料运行页位于 `apps/runtime`。

## 现在可以体验

- 在 Studio 中操作原 HTML 的菜单、业务页、表单、页签和弹窗。知识小窗的开合保留原型实例与已输入内容。
- 点击“关联知识”，再点击一个原型控件。在小窗填写业务说明，选择文档及准确章节，预览后直接保存。
- 普通点击原控件时，小窗显示已关联说明；可以编辑、窗内预览原文或打开全文。
- 独立文档阅读器提供 204 篇资料的标题搜索、目录、完整章节和关联操作入口。从章节可跳回页面及控件；有已记录打开步骤的弹窗可自动重开。
- “原手册”使用另一运行实例打开用户提供的原始手册，保留其自身目录、搜索和原有展示。当前关联维护位于工作区文档阅读器。
- 在关联管理中打开原型、查看原文、编辑说明、移除或恢复关联。保存进入数据库；浏览器刷新及重新登录后仍能读取。

## 启动

先安装锁定依赖：

```sh
uv sync --frozen
npm ci
```

已有导入实例可通过 `VKS_DATA_DIR` 指定数据目录。首次准备云收费试点时，使用用户选定的两份资料：

```sh
PYTHONPATH="$PWD/services/api:$PWD" uv run python scripts/prepare_studio.py \
  --data .vks/studio-preview/data \
  --prototype '/所选目录/云收费页面原型合集(1).html' \
  --handbook '/所选目录/云收费系统项目知识手册(1).html'
npm run dev:studio
```

Studio 默认为 `http://127.0.0.1:5193`，API 使用 8014，Runtime 使用 5195。启动脚本检查端口，支持 `VKS_STUDIO_PORT`、`VKS_API_PORT`、`VKS_RUNTIME_PORT` 和 `VKS_DATA_DIR`。资料准备脚本拒绝覆盖已有数据库，并在数据目录旁保存 `preview.json`，记录项目和草稿身份。

本地首次创建实例的管理员为 `owner / owner`。打开项目后选择“原型”；新实例尚未创建关联时，可以直接在 UI 中开始维护。导入生成的原始资料、数据库和试用记录位于忽略目录。

## 实现与数据

`InteractiveKnowledgeService` 复用原有草稿权限、不可变修订、业务点、准确章节、出处继承和保存事务。新 HTTP 契约位于 `/api/v3/projects/{pid}/interactive`，提供目录、包内容、准确文档、关联读取、保存、移除与恢复；使用当前草稿选择版本和对象版本控制并发，关联保存支持幂等重放。

P0 使用不可变原始 `Artifact` 的 ID 和 SHA-256 固定运行包；DOM 定位存入 `Action` 的不可变修订 `interactive_anchor` 元数据，说明与出处沿用 `has_business_point` 和 `supported_by_section`。定位包含包身份、业务页路径、嵌套 frame、状态容器、选择器、标签和打开步骤。每次定位要求唯一可见目标及相同标签，失败时提示用户打开状态或重选。首次试点中的三条关联为人工维护说明，原型点击不改变其证据类别或因果依据。

Studio 从 API 读取获授权原始字节，核对摘要后通过专用 MessageChannel 转移到独立 `*.localhost` 运行实例。原型与手册具有不同来源。API 凭据保留在 Studio；Runtime 仅服务固定的公开启动文件，不能读取数据目录。HTTP CSP 禁止运行材料联网和表单外发；原 HTML 的内部 Blob 资源及原交互保留。导入、索引与后端任务继续只处理数据。

## 验证方式

`make check` 包含新增 Studio 的 TypeScript 和生产构建。`tests/interactive` 使用可提交的合成资料覆盖实际浏览器闭环、持久化、双向导航、读取隔离、幂等、并发冲突、跨项目引用、准确章节、冻结版、原包授权闭包、移除及恢复。契约通过 `make contracts` 更新，并运行原有官方 MCP HTTP 与 stdio SDK 回归。

用户提供资料的本地浏览器验证另行记录，不将这些原始文件纳入测试仓库。其结果见 [验证记录](VERIFICATION.md)。

## 后续批次

D2 的完整 3.0 仍按 P1–P5 推进：完整文档渲染共用、段落与文档互引、业务点复用、多章节维护、个人阅读位置持久化、专用包与锚点模型、资料更新后的定位迁移、全部业务动作的适配、Studio 增量 MCP 工具和服务化部署。P0 当前使用本机运行服务；通用任意 HTML 格式与所有 102 条路由的逐动作验收尚未完成。

旧 Web 和截图导览保留已有版本能力，历史结果见实施及验证记录的 D1 部分。此交付验证原 HTML 与知识关联的可体验闭环，尚未宣称完整 3.0 或真实收费业务验收完成。

## 手册式 AI 连接设置

Studio 顶部“AI 设置”按原手册的交互重做：选择服务商和回答模型、查询当前 Key 可见模型、选择回答长度、测试所选模型，最后统一“保存并使用”。

- 服务商预设包含 OpenAI、DeepSeek、阿里云百炼和自定义兼容接口。预设自动填写地址、协议与模型；具体账户权限以服务返回和测试结果为准。
- 模型使用可搜索的下拉框。可获取账户模型、保留当前选择，或手动填写服务提供的精确模型 ID。百炼的工作区域名使用其原生目录接口；其他兼容服务使用 `/models`。
- “我的连接”保存多个连接与模型选择。切换连接恢复原配置；不同连接按准确端点保存独立凭据。原有 OpenAI Key 继续保留，不向其他服务商自动转发。
- 回答长度提供 1,500、2,500、4,000、8,000 token 预设。更多设置内维护每日预算及内容发送范围。
- “测试连接”只发送固定的测试消息，会产生少量模型用量，不发送项目文档。它校验基础文本生成，结构化输出等能力仍需功能任务验证。
- 地址、协议、连接名称和预算按需展开。Key 在本机后端保存，返回值和审计不含密钥。保存新连接时，本机管理员可在页面登记 HTTPS 服务地址，仍沿用固定 IP、重定向及保留地址限制。

连接的非秘密设置存入新增的 `model_connection` 表；当前项目模型策略引用所选连接。凭据在实例的 `model-credentials.json` 中以不可变引用保存，文件为 0600。SQLite 自动新增表；PostgreSQL 使用 Alembic 迁移 `m26fdb805243`。

实现参考用户手册中的模型下拉、账户模型查询和模型测试，并核对 [OpenAI 模型目录](https://developers.openai.com/api/docs/models)、[OpenAI 模型查询](https://developers.openai.com/api/reference/resources/models/methods/list)、[DeepSeek 接口](https://api-docs.deepseek.com/api/list-models/) 及 [百炼兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[百炼模型目录](https://help.aliyun.com/zh/model-studio/list-models)。预设不表示已对每一家服务完成真实验收。

模型接入已完成；控件的 AI 自动关联、AI 功能讲解、右键菜单及对应快捷键继续作为当前开发任务推进。
