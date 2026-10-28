# VKS 2.0 开发预览：实现与验收边界

日期：2026-10-26 至 2026-10-28。主干基线 `b8926b0d2665034f24d85a534ec258d544398990`；工作分支 `codex/vks-2-upgrade`。

本轮补齐 2.0 首发范围的剩余工程，真实业务验收与正式发行仍需独立完成。三份交付资料原样存于 `design/`，原 1.0 `docs/design/` 不改。任务提示词文档中的只读分析/逐阶段批准段落是示例，不替代用户本次实施授权。开发前已有 R01/R02 的三个服务修改、审计文档和回归文件，保留并纳入兼容验证；不将其冒充本批新增修复。

## 工程与兼容决策

- 延续现有 React/FastAPI/SQLAlchemy/Playwright/官方 MCP SDK，无新增依赖或基础设施。
- Journey 使用 `entity(type=journey)`、`entity_revision`、`artifact` 和 `analysis_set_entity`。原子保存准确引用，发布冻结进入现有 manifest。没有新表、新列或 DDL；不为已有 JSON 章节映射重复建表。
- 流程修订包含规范化 DSL 和摘要。保存用准确修订 ID 的 `If-Match` 和服务器幂等记录；冲突保留本地编辑，可导出后重新载入。流程批准/拒绝保存为独立、绑定准确修订和摘要的审计事件；不改写修订，机器不能批准。编辑后重新审核，发布预检与最终发布均复查。
- 流程及任务报告继承输入的源码/Trace 限制；读取时重新计算引用制品的当前分类。混合观察状态的场景整体要求 `trace:read`，避免名称、状态和截图引用从索引泄漏。权限先于列表、搜索、指标、反查和导出。
- 知识任务复用 job/outbox/attempt/lease/取消和官方 Worker。新任务种类 `knowledge_task` 路由至现有 model 队列。固定输入修订、版本和授权上限，阶段及最终提交重新检查。模型结果检查点保持私有；一次调用额度在请求前持久保留，失败且结果不确定时不自动再次消费额度。
- `/api/v1` 保留；新产品接口在 `/api/v2`。MCP 原 18 工具源契约不变，额外 5 工具在 `mcp_tools.v2.json`；构建器为新实体、任务和结果引用追加兼容枚举。SDK 服务协议版本仍沿用 1.0 RC 标识，UI 明确显示 2.0 开发预览，尚未切换发行版本。

## 本批功能与入口

| 功能 | 实现位置与真实行为 | 边界 |
| --- | --- | --- |
| 五主导航和冰蓝白组件 | `App.tsx`、`workspace-navigation.ts`、`design-system/`、`workspace-v2.css`；项目空间/业务探索/知识构建/变更与审核/项目设置 | 八种旧 hash 保留；概念原型别名可恢复。旧页面渐进复用 |
| 项目驾驶舱 | `ProjectCockpit.tsx`；当前身份可见页面、流程、动作、确认覆盖及待审统计 | 页面分组不冒充已验收业务域；确认覆盖只计算 confirmed+valid |
| 五步接入 | `OnboardingWizard.tsx` → 现有来源登记、上传、文件安全预检、导入任务；末步可勾选动作、调整顺序、直接生成独立流程草稿 | 创建绑定准确基线选择版本，无需手写引用 JSON；无模型调用，顺序是人员声明。原生 Windows Agent 与会话内可选调度已实现；不安装无人值守系统服务 |
| 六类节点流程画布 | `packages/journey_dsl`、`journeys.py`、`JourneyCanvas.tsx`；页面/动作/判断/分支/终止/引用，拖动、方向键、连接、撤销重做、DSL 导入导出 | 限 120 节点/240 边、DAG、可达性、分支标签、精确同版引用。导入先经服务器验证再进入画布；窄屏只读 |
| 多状态探索 | `StateReplay.tsx`、`workbench_v2.py`；同版声明/模拟/观察时间轴、两状态对比，复用已脱敏快照 | 缺图明确提示；不重新执行录制或业务请求。采集审批继续使用 1.0 服务；不把状态绑定升级为运行因果 |
| 同版技术路径与返回动作 | `ActionChain.tsx`、`BusinessReferences.tsx`；动作到知识、源码到动作，3 跳/100 节点，关系路径上限 120，前端先显示 8 条 | 默认 confirmed+valid；有界知识关联路径，不保证完整执行链；未知 Trace/运行版本独立提示 |
| 知识任务 | `knowledge_tasks.py`、`task_checkpoints.py`、`AgentWorkspace.tsx`；Plan→Collect→Analyze→Propose→Validate→Review，私有检查点、阶段边界暂停/继续。自然语言 Plan 后先暂停，人员核对准确输入与计划摘要后复用同一次模型结果执行 | 普通继续不能绕过计划确认；每次恢复重查当前权限、固定基线、模型策略和 attempt。五种固定应用工具，不接受自由工具执行，真实提供方效果待验收 |
| 模块发现与采用 | 按准确页面动作、显式服务内路由前缀、源码/文档目录提出可重叠分组；报告理由、范围和修订；人员采用或批量拒绝绑定报告摘要 | 不按名称合并、不自动确认业务知识；独立 Gold 和复杂跨服务自动发现未验收 |
| 增量维护 | `collector/sync.py` 输出完整验证包及 delta；`incremental.py` 建当前类型/Mapper/适配器依赖摘要，复用同项目语法及语义分片，只分析变化依赖上下文；流程绑定纳入影响报告 | 完整包校验、解码和摘要索引仍执行；保守依赖闭包可能扩大，报告实际重算文件数。冲突或不闭合缓存回退冷分析；指定硬件容量结论仍待验收 |
| 录制与状态转流程 | `capture_proposals.py`、`CaptureJourneyBuilder.tsx`；从保存的状态图或最多 10 条成功录制生成独立 DAG 候选，节点打开准确状态/脱敏快照 | 录制边只代表快照顺序；多录制的选择分支不代表真实业务条件。循环状态需先拆为有限轨迹；不修改场景、审批或因果 |
| 审核与发布 | `review_inbox.py`、`ReviewInbox.tsx` 统一关系/文档/迁移/流程的权限过滤、分页、筛选、精确修订令牌及原子混合批次；复用同一应用服务并逐项留审计 | 文档先提交再批准；业务解释/运行因果或失效关系逐条核对；迁移及其目标关联分批审核。拒绝不自动重开，批准不升级有效性或因果；现有发布门禁保留 |
| MCP 增量 | `propose_journey`、`create_knowledge_task`、`get_knowledge_task`、`get_knowledge_coverage`、`get_change_impact_report` | 23 个工具；无 approve/publish。普通 get_job/cancel_job 继续复用 |
| 导出 | 已发布流程进入 Markdown ZIP、PDF 和可回导资料包的数据 sidecar | 回导不迁移审批；跨实例准确修订需要重新绑定，不能把原 UUID 当作新项目有效引用 |

## 新 API 与契约

冻结文件：`packages/contracts/journey.schema.json`、`openapi_2.0.json`、`mcp_tools.v2.json`、`mcp_tools.resolved.json`。OpenAPI 用 `VKS_DATA_DIR=<隔离目录> uv run python scripts/freeze_v2_contracts.py` 重建；MCP 用 `uv run python scripts/build_contracts.py` 重建，测试检查冻结与运行一致。

- `GET/POST /api/v2/projects/{pid}/journeys`、`POST .../journeys/validate`
- `POST /api/v2/projects/{pid}/journey-proposals`（固定场景修订，分析集合选择版本 If-Match）
- `POST /api/v2/journeys/{jid}/revisions`、`GET .../revisions/{rid}`、`POST .../review`
- `POST /api/v2/projects/{pid}/onboarding/preflight`
- `GET /api/v2/projects/{pid}/state-replay`、`knowledge-coverage`、`review-inbox`
- `GET /api/v2/projects/{pid}/review-items`、`POST .../review-batches`（准确选择版本 If-Match、每项 review_token；整个批次原子提交）
- `GET /api/v2/actions/{eid}/explanation`、`entities/{eid}/business-references`
- `POST /api/v2/projects/{pid}/knowledge-tasks`、`GET /api/v2/knowledge-tasks/{jid}`
- `POST /api/v2/knowledge-tasks/{jid}/control`、`POST .../confirm-plan`（准确 attempt If-Match；计划确认仅人员会话）
- `POST /api/v2/knowledge-tasks/{jid}/module-proposals/{key}/accept`
- `POST /api/v2/projects/{pid}/change-impacts`、`GET .../change-impacts/{jid}`

流程写入、人员决策、知识任务、影响任务均要求幂等键；流程保存/审核要求准确 If-Match。预检/DSL 校验只有读取和验证，不改变业务知识。

## 本机显式同步

在 VKS 自身环境运行。`--root` 和 `--consent-root` 必须为本次明确授权的同一目录；SVN 使用既有只读允许根及固定数字修订，Git 显式固定 ref。以下为使用格式，不是已执行的业务采集记录：

```sh
uv run python -m packages.collector.sync --provider directory \
  --root /已授权的目录 --consent-root /已授权的目录 \
  --project project-key --baseline fixed-baseline --source-key source-key \
  --state .vks/sync/source.json --output .vks/sync/source.vksbundle.zip
```

首次及后续报告包含变化分母、未变化数、移动/新增/修改/删除。缺失至少占旧清单 25% 且旧分母 ≥10 时仅写私有诊断，不生成新包、不推进游标。服务端大规模实体缺失也拒绝直接生成漂移提案。没有执行任意仓库构建、hooks 或材料中的脚本。

## 阶段与剩余工作

| 单元 | 当前实现/证据 | 未完成项 |
| --- | --- | --- |
| T00 / S0 | R01/R02 回归；1.0 固定二进制建 PG 库→V2 读取/任务→备份恢复→旧二进制回滚读取一致，无 DDL | 无唯一历史映射时保持明确 409；未迁移生产库 |
| T01 / S1 | 五入口、真实数据驾驶舱、设计 token/Surface、响应式和键盘回归 | 独立人工 UX/对比度测评；旧页后续拆分属于重构，不是功能缺口 |
| T02 / S1 | 五步向导、原生 Agent、显式调度、回执恢复、DOM/模块候选、接入报告、分发包 | Windows 原生执行以 CI 结果为准；企业目录/SVN 授权试点待提供 |
| T03 / S1 | Journey DSL/CAS/人员批准/冻结/导出，自动排布、字段级三方合并及显式保存 | 真实复杂长流程和独立 UX 测评 |
| T04 / S2 | 保存状态/录制转候选、准确快照回看；角色/前置条件/双轨迹范围比较；十步 Mock 脱敏保存剖面 | 真实业务三分支采集和分支条件 Gold |
| T05 / S2 | 同版双向引用、有界路径、Kafka 字面发送/监听、XXL handler 及歧义缺口 | 跨集群投递和有效运行 Trace 必须由真实环境另证 |
| T06 / S3 | 有界自然语言计划、人员范围确认、阶段暂停/继续、固定工具步骤与恢复，独立候选及证据/预算/权限复核 | 真实模型成本、延迟与效果未验收；不支持自由工具执行 |
| T07 / S3 | 本机调度/事件、语法与依赖分片缓存、流程影响、章节 diff、统一原子审核、提案 CAS 与人员决定保留 | 真实影响 Gold 和指定硬件性能门禁待验收 |
| T08 / S3 | 5 新工具及官方 SDK 验证，原 18 保留 | 外部实际 Codex Host/IdP 供应商验收 |
| T09 / S4 | 验收矩阵和现有合成素材 | 需授权云收费业务材料、冻结 30 动作独立 Gold、5 人任务、指定 PG/硬件容量与升级回滚签署；不能用合成数据替代 |

## 2026-10-28 继续开发的实现细节

- 解析单元以原始字节 SHA-256、解析器依赖版本和处理代码摘要寻址，同项目复用。只保存有界 JSON 数据，语义分片为有界 zlib JSON（解压上限 32 MiB），缓存损坏回退原始材料，不反序列化可执行对象。项目缓存最多 16384 个单元、128 MiB，项目锁下分批写入与淘汰；每次访问检查当前任务身份、授权、attempt、租约和取消。语义键额外绑定路径、来源、服务、原始/解码摘要及当前有序依赖闭包；新增/删除目标与适配器冲突使读者失效。完整包校验与输入解码仍执行。
- Collect/Analyze/Propose/Validate 检查点绑定固定任务计划、可见输入修订、基线选择版本、授权上限、代码版本及模型策略摘要。输出写入私有内容寻址对象，损坏检查点重新计算；重试与最终原子提交仍复查权限、取消和版本。中断阶段不会冒充完成；Web/MCP 返回已复用阶段标记。模型禁用或策略变更后必须新建任务；不自动重发结果不确定的一次模型调用。
- 场景编辑器新增“从已保存状态构建流程候选”。状态图只接受可转为有限 DAG 的已保存状态，保留 state_ref；录制保留 capture_ref（逻辑场景、当前场景实体修订、录制 ID、准确快照下标）。原录制自带的执行场景修订独立保留，不把当前编辑版冒充执行版。后台复核当前来源基线及 DOM/PNG 制品归属、trace/source 权限。流程候选需要人员审核，节点支持被动回看。
- 克隆基线时，新 Journey 修订改绑新场景修订，保留原 capture_id；冻结发行版与旧场景/Journey 字节保持不变。新修订重新等待流程人员审核；场景变化也进入节点影响报告。没有转移旧采集审批或推断后端因果。

## 数据升级与回滚

本批不连接、迁移或重启现有工作实例；默认开发 SQLite 仍复用现有表。新类型只在创建流程/任务后写入，既有原始对象与历史 manifest 保持不变。旧二进制不理解新任务种类，回滚前应停止接收新任务、等待或取消 knowledge_task，停止 Worker，并使用升级前一致备份恢复 DB/对象；不能把“无 DDL”当作允许带新任务直接运行旧 Worker 的依据。旧版导出程序不认识 Journey 时可能省略它，使用本批导出或完整备份保全流程。

实际检查结果和 AT01–AT24 边界见 `VERIFICATION.md`，机器结果与源码摘要记录在 `docs/validation/results/`。真实模型、真实业务、PG/队列环境专项和生产发行单独计量，不把本地合成通过写成 2.0 GA。

## 2026-10-28 完整开发轮次补充

- `collector/agent.py`、`agent_gui.py`、`windows_files.py`：本机显式授权、Win32 无重解析点读取、RAM 凭据、上传前预检、确定性回执/失败游标、可选同步事件；`distribution.py` 和构建脚本提供含哈希锁依赖的 Windows 启动分发包。调度只在窗口打开且本次授权有效时运行。
- `ui_discovery.py`：没有 UI sidecar 时读取可见静态 DOM。显式逻辑 key 优先，唯一 DOM ID/来源路径可形成候选；重复 key、隐藏元素、没有稳定锚点和超限材料留下缺口。不执行页面脚本，不按相同名称合并。原始 HTML 字节不变。
- `source_alignment.py`：来源 `version_labels` 加性保留，当前选择内分别比较代码 revision/build id。声明一致与运行已核对分开；不借相同 topic/路由合并 API 身份。
- `async_topology.py`：KafkaTemplate/KafkaListener 字面静态候选、XXL handler 与歧义/动态缺口；同名 topic 不证明共用集群或成功投递。接口依据见 [Spring Kafka](https://docs.spring.io/spring-kafka/reference/kafka/sending-messages.html)、[XXL-Job 官方文档](https://github.com/xuxueli/xxl-job/blob/master/doc/XXL-JOB%E5%AE%98%E6%96%B9%E6%96%87%E6%A1%A3.md)。
- `task_planning.py`：一次结构化模型计划、最多八步、五种固定工具，ID/证据/工具/预算均由服务器复核；只创建独立待审产物。工具步骤有检查点，已被人员拒绝的关系不重置。阶段读取校验当前权限和内容摘要，损坏缓存可从准确原始输入重建。
- `document_diff.py`：按 R01 精确有序章节映射，对固定发布版与准确草稿修订比较新增/删除/正文/标题/顺序/元数据；人工锁和历史正文不改。提案保存目标关系行版本，旧提案不能覆盖较新人员决定。
- `trajectory_comparison.py`：比较当前可见同版场景与固定采集的角色、前置条件、保存状态和已声明动作；有限观察范围显式，当前来源分类变化后重新过滤。
- `journey-editing.ts`、`JourneyMerge.tsx`：自动 DAG 排布、三方合并、手选冲突、服务器验证和独立保存；没有自动覆盖。知识图谱改为服务器上限 100 节点和分层可滚动布局。运行实线还需 confirmed/valid/matched；审核、有效性和因果分别显示。
- 静态 DOM 的页面原始内容/制品/定位未改变时，新增人工热点不使其他未修改动作的原静态成员事实失效；其原证据和审核状态保留。其他受影响关系仍失效待复核，静态来源不会被改称人工或运行证据。
- 发布事实缓存增加按审核/有效性分类的邻接索引，仍逐事务重读权限；PG 搜索使用保守 SQL 候选过滤，Unicode/转义 JSON 仍回到原有 casefold 语义。
- `evaluate_v2_pilot.py` 核对独立冻结的 Gold 摘要、准确项目/发布/manifest、30 动作、五人配对任务及阈值；输出只是所提供记录的计算结果，不替代真实业务授权、独立性证明或发行签署。

复现结果与所有未满足的真实环境门禁见 `VERIFICATION.md`；逐项推进见 `COMPLETION.md`。本机现有用户实例未切换或重启。

## CI 回归与动作读取收尾

- `route-sync.ts` 为 hash 导航维护显式用户操作与原生历史事件的版本。浏览器已切换地址时，旧 render 的被动 effect 不得回写旧地址；Back/Forward 的 popstate/hashchange 去重，自动补全使用 replace，用户导航使用 push。两个纯状态时序回归和真实 Chromium 历史/文档深链接回归已覆盖。
- 动作解释在同一只读一致事务内读取当前有权图、来源和流程，不再加载整个工作区。关系按邻接索引展开，最多三跳、100 节点、120 条路径，只返回实际路径涉及的节点/关系/证据；正文通过原有准确定位接口读取。被省略的循环、其他分支或超限关联显式报告截断，不能据此宣称完整运行链。
- Journey 列表先按固定选择筛选流程类型，再检查其输入制品的当前分类，只载入可见流程正文。不会为列出流程先加载其他实体正文。动作解释与流程列表保持同一数据库快照。
- 容器专项补充 V2 知识任务的真实 model 队列消费、检查点、候选流程、未审核发布阻断、人员审核、冻结读取与导出。最新完整执行结果以验证记录和后续源码清单为准。
- `StateReplay` 保持轨迹比较组件的稳定位置，状态列表迟到不会重新挂载并清空人员选择/比较；取消的状态响应不回写。真实浏览器门闩固定为比较先完成、状态后到达，验证选择/摘要/快照及请求次数，并保留原权限撤销回归。
