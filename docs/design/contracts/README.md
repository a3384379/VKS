# Visual Knowledge Studio 1.0 契约参考

这是 1.0 开发方案的可机读设计附件，不是已经实现或部署的软件，也不是未经评审即可用于生产的最终 API。源码、API、MCP、导入器与前端应共同以经评审冻结后的契约为依据。

## 文件用途

| 文件 | 用途 |
| --- | --- |
| `import_manifest.schema.json` | JSON Schema 2020-12；导入包结构、版本来源、正式 SHA-256、制品引用与可选 UI/绑定 sidecar |
| `scenario.schema.json` | JSON Schema 2020-12；线性浏览器场景、独立讲解状态/分支图及一个合成 Mock 示例 |
| `binding_manifest.schema.json` | JSON Schema 2020-12；外部 AI/人工随 HTML 和 Markdown 导入的明确关联关系 |
| `mcp_tools.json` | 18 个工具的 inputSchema/outputSchema、scope、提示性 annotations，以及 Resources/Prompts |
| `openapi_1.0.yaml` | OpenAPI 3.1；28 组核心路径、请求与响应 DTO、错误、幂等、乐观锁和发布流程 |

所有契约位于同一目录。MCP 的 outputSchema 引用 OpenAPI 中的 JSON Schema；实际注册 SDK 前必须解析外部引用并打包为自包含 Schema，不应把带相对路径的文件直接复制给客户端。`uiManifest` 文件内容按 `import_manifest.schema.json#/$defs/uiManifestDocument` 验证，解析时保留原始文档作为 `$ref` 基址。

## 最关键的导入模式

1.0 同时支持两条入口，均不要求先启用平台 LLM：

1. **已有显式绑定包**：外部 AI 已分析源码、生成 HTML 原型和 Markdown，同时提供稳定 logical key、UI manifest 与 bindings。平台按 key 做确定的实体映射，并验证引用与证据，不需要重新依赖相似度猜测绑定。
2. **历史素材补关联**：只有旧 HTML/Axure/截图/Markdown 时，规则解析、人工选择及可选语义模型生成关联候选。低证据候选不自动确认。

有来源明确的“确定映射”与“业务内容已经正确”是两回事。平台可自动找到 `ui.action.invoice.submit` 对应 `doc.section.invoice.rules`，但不能仅据这一声明认定开票规则正确或某次接口具有运行因果。导入关系仍进入候选/审核流程。

### 外部 AI 约定

HTML 使用稳定的逻辑键：

```html
<main data-vks-page="ui.page.invoice">
  <button data-vks-action="ui.action.invoice.submit">提交开票</button>
</main>
```

这些属性只提供定位和知识关联，不授予脚本执行或真实开票权限。平台的“探索”模式拦截选择手势并展示知识，不把点击透传为业务操作。

Markdown 原文件可不改动，sidecar 为章节分配 `sectionKey`。`headingPath` 与行号只定位某个文件修订；稳定身份来自 `sectionKey`，不是标题、行号或内容 hash。

导入根清单中的 `uiManifest` 和 `bindings` 分别是包内相对路径与真实 SHA-256。侧车清单的 `projectKey`、`baselineKey` 必须分别等于主清单的 `project_key`、`baseline_key`。字段大小写按 Schema 精确使用；主清单/API 采用 snake_case，跨素材绑定 sidecar 使用文档中明确的 camelCase 字段，不自行改写键名。

示意绑定项（省略了外层清单及完整证据记录）：

```json
{
  "fromLogicalKey": "ui.action.invoice.submit",
  "toLogicalKey": "doc.section.invoice.rules",
  "relationType": "documents",
  "evidenceRefs": ["evidence.invoice-dom", "evidence.invoice-doc"],
  "reason": "UI 操作对应此章节；业务正确性需审核"
}
```

在这份契约中，`documents` 的方向约定为“主体实体 → 描述该主体的文档章节”，例如 action → section；前端反向导航通过反向边查询提供，不另造一条语义冲突的重复边。

### 服务端必须执行的交叉校验

- `projectKey`、`baselineKey`、快照来源和调用者授权项目一致。
- 同一导入基线中 logical key 唯一，所有 action.pageKey、fromLogicalKey、toLogicalKey、evidenceRefs 可解析；重复或多义必须阻断。
- HTML 里 `data-vks-page`、`data-vks-action` 与 sidecar 一致；同一 action 的多个合法渲染实例显式归属同一语义动作，不因 DOM 重复而随机选择。
- 证据文件存在、SHA-256 重算一致、行范围有效、章节/符号/请求 locator 能在相同制品修订中定位。
- 不能因为 `evidenceKind=static/observed` 是模型填写，就直接信任为静态或运行事实。
- 不接受模型输入的 `confirmed` 或 `published` 状态。显式绑定中的 `confirmed_trigger` 不允许由包直接声明；其建立要求服务器核实足够的因果证据。
- `allow_platform_llm=false` 时不会调用平台模型；外部 AI 已整理好的知识依然能够导入、关联、审核和浏览。

## 版本与唯一约束修正

主数据模型必须使用以下语义，避免草稿覆盖历史：

1. `entity` 保存长期逻辑身份。UI action 的稳定唯一性放在此层，例如 `(project_id, entity_type, stable_key)`，其中 action 的 stable_key 包含所属页面语义键与动作键。
2. `entity_revision` 内容不可变，按 `(entity_id, content_hash, schema_version)` 去重，可以跨分析集合复用。`content_hash` 必须是完整规范修订载荷的摘要，包含 artifact 来源身份、locator、metadata、正文及 Schema 版本，不能只哈希正文；方法移行或切换来源快照即使正文不变，也可能需要新修订。`semantic_content_hash` 另存，仅用于移动识别及结合解析器版本的解析缓存，不用于复用携带旧来源定位的修订。同一文档编辑生成新修订，而不是原地覆盖。
3. `analysis_set_entity(analysis_set_id, entity_id, entity_revision_id)` 保存基线选择；唯一 `(analysis_set_id, entity_id)`。草稿编辑切换选择并递增 selection_version；封闭发布版的选择不可变。
4. `ui_action` 如作为修订扩展表，唯一键是 `entity_revision_id`。不要把 `(project_id,page_entity_id,semantic_key)` 强加在修订表上，否则不同分析集合的合法修订无法共存。
5. 显式设置 `scenario_revision`，记录 scenario_id、entity_revision_id、revision_no、不可变 DSL、content_hash。`scenario_step` 指向 `scenario_revision_id`；录制会话绑定准确修订。
6. API 路由身份含 `service + method + normalized_route + condition_fingerprint`，条件指纹规范化并包含 Spring `params/headers/consumes/produces` 等限制。多个相同路径的合法 Handler 不得合并。`api_endpoint` 是 entity_revision 扩展表，不另存 analysis_set_id；通过 analysis_set_entity 选择映射得出当前基线中的端点。
7. 用户身份、全局模型配置等实例级表不强制套用 project_id。只对项目所属实体执行项目外键/授权约束。

`condition_fingerprint` 推荐以排序后、保留逻辑含义的规范 JSON 计算 SHA-256；不能删除 `!header`、`param=value` 之类有语义的表达式。路径、条件或网关服务映射不明确时保持 ambiguous。

## 审核、有效性、证据与因果

| 字段 | 值 | 含义 |
| --- | --- | --- |
| review_status | candidate / confirmed / rejected | 人员审核结论 |
| validity_status | valid / stale / broken / obsolete | 当前基线下是否仍可用 |
| evidence_kind | manual / semantic / static / observed | 证据的获取类型 |
| causal_basis | not_established / temporal_association / static_potential / correlated_runtime | 关系具有什么因果依据 |

`confirmed + stale` 完全合法，表示曾确认的关系现在需要复核。人工确认不自动成为 `correlated_runtime`；时间相邻只能支持 `temporal_association`。ReviewRequest 不接收有效性和因果字段，防止一次点击审核无依据地清理过期状态。

`Job.state` 仅为 queued、running、retry_wait、succeeded、failed、cancelling、cancelled。输出不完整用 `completeness=partial`，不是额外的任务状态；生成候选后任务可 succeeded，后续人工审核属于发布工作流。

## 安全与实现责任

- JSON Schema 不是完整安全机制。安全解压、文件真实类型、大小/数量、相对路径规范化、ZIP Slip、符号链接、压缩炸弹、重复路径、权限、证据存在性和跨版本一致性均须服务端验证。
- 主清单中的 `source.content_sha256` 表示规范化来源文件清单的摘要：按包内相对路径排序，记录每项 path、sha256、size_bytes，然后以团队冻结的规范 JSON 序列化（推荐 RFC 8785）后计算。禁止凭拼接字符串或任意文件顺序产生不稳定摘要。
- 场景 DSL 没有 arbitrary JS、eval、shell、外部 URL 或秘密明文字段。`goto.path` 仅相对登记环境；服务端仍检查 DNS、所有重定向、子资源和 WebSocket 的网络边界。
- `risk_level=read_only` 是声明，不是安全证明。服务端根据环境和动作策略判断真实风险；测试写入还需有效审批记录、测试数据集和幂等业务策略，禁止生产写入。
- `approval_ref`／`human_confirmation_ref` 必须对应服务器签发并绑定身份、对象、版本/manifest hash 和有效期的记录，客户端提供任意 UUID 不生效。
- MCP annotations 只是客户端风险提示，不能替代 token scope、项目 grant、对象归属、任务所有者或人类审批校验。request_release 不发布；普通 AI 令牌无 links:review／release:publish。
- cancel_job 和 HTTP cancel 的基础 scope 为 knowledge:read；拥有者还须具有该 job.kind 的原发起 scope（导入 source:import、采集 capture:run、分析 analysis:run、预检 links:propose、导出 knowledge:export）。项目管理员走 project:manage 授权。不能硬编码 analysis:run，导致只有导入或采集权限的任务拥有者无法取消自己的任务。
- HTTP bearer token 只用于本平台；不得原样转发给 SCM、模型供应商或业务系统。UI 原型独立域名隔离，Markdown 不执行脚本。
- 数据库任务状态与 outbox 为权威，采用幂等阶段与至少一次投递；推荐默认 Celery + RabbitMQ。契约不依赖特定 Broker，Redis 非必需。

## 例子与验证说明

`scenario.schema.json` 内含只操作 Mock 页面、不调用真实开票接口的合成示例。Schema 中的正式 SHA-256 必须是 64 位小写十六进制，不能用 `<hash>` 或短摘要代替；运行时仍须重新计算，格式正确不证明内容真实。

`scenario.steps` 每次仍为线性录制；可选 `explanation_graph` 是多次录制及模拟分支的讲解图，不是流程执行引擎。状态包含 stateKey、label、evidenceOrigin，可绑定 captureRef；转移包含 fromState、actionKey、conditionLabel、toState、scenarioRef 等。conditionLabel 只展示、不求值。observed 必须带经核验的 captureRef/scenarioRevisionRef（转移还需 evidenceRefs）；simulated/declared 与真实录制始终区分。多次录制合并前校验同 project/baseline，状态键和动作键可解析；数据库 `scenario_state`、`scenario_transition` 都归属准确的 scenario_revision。成功/失败分支可展示，但不增加循环、任意分支脚本或复杂控制流执行能力。

`examples/minimal-explicit-binding/` 提供可直接阅读的 HTML、Markdown、UI sidecar、bindings 和根 manifest；真实摘要/大小及来源规范化规则见 `examples/README.md`。它仅说明 action → section 显式绑定，不包含运行证据、真实业务调用或平台 LLM。

本附件交付前检查 JSON/YAML 可解析性、重复键、全部本地 `$ref` 可解析、OpenAPI 路径参数和 operationId 唯一性、18 个 MCP 工具名称/输入/输出引用，以及场景示例和若干拒绝样本。当前环境未安装专门的 OpenAPI/JSON Schema 元规范验证器，因此未宣称通过其完整兼容性测试；开发仓库应在 CI 加入 OpenAPI 3.1 lint、JSON Schema 2020-12 验证与客户端契约测试。

这些文件没有真实服务器地址、访问令牌或公司数据。真实环境与身份提供方在部署配置中指定。
