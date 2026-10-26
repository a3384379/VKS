# VKS 审计分批修复记录

基线：`c5c93eec69f25bfc22b0ec9f947c8c7dc94baf64`。开始时 HEAD 与基线一致，工作区干净。初始审计阶段仅本地修改，不提交、推送、创建 PR 或发布；不用真实 IdP、模型、业务系统或客户资料。新测试使用临时 SQLite、合成材料及独立 Chromium。后续用户另行授权GitHub提交与合并，其状态以对应PR/提交检查为准，以下结果仍是审计时的本地验证快照。

编码核对：仓库274个可解码文本文件均为UTF-8、无BOM、LF；遵循 `.gitattributes` 和原设计编码说明。未进行全仓转码或无关格式化。`docs/design/` 原件保持不变。

## 开始时的逐项清单

| 编号 | 状态 | 定位 |
| --- | --- | --- |
| A01 | 仍存在 | OAuth `actor_for_job` 未保存提交时scope上限 |
| A02 | 仍存在 | `finalize_document` 只按模型引用继承权限 |
| A03 | 仍存在 | `persist_bundle` 绕过锁及文档工作流 |
| A04 | 仍存在 | `persist_bundle` 只upsert，不撤销缺失章节 |
| A05 | 仍存在 | Workbench未消费action context，遍历所有关系 |
| A06 | 仍存在 | `_document_key` 只按路径，跨来源可覆盖 |
| A07 | 仍存在 | `visible_release` 保留无可见证据的关系 |
| A08 | 仍存在 | 有限转发策略无业务CSRF头，响应丢弃Set-Cookie |
| A09 | 仍存在 | Workbench旧pageId优先于动作所属页面 |
| A10 | 仍存在 | releases引用变化重置比较目标 |
| A11 | 仍存在 | loadProjects只加载首页且丢弃metadata |
| G01 | 尚未闭环 | 场景与工作台独立导航状态 |
| G02 | 部分基础已有 | 采集查询与场景修订存在，查看/绑定入口缺失 |

## 第一批 A01–A04

### A01：任务授权上限

- 原因：后台仅凭principal ID恢复当前完整grant，丢失JWT原有scope。
- 修改：`service.py::_new_job` 在现有任务JSON的保留字段中写入服务器授权快照，客户端同名字段会被覆盖；`jobs.py::current_actor`、`oauth.py::actor_for_job` 及所有后台身份恢复调用点执行提交权限、当前身份/grant、当前项目权限的交集。快照不含token明文，重试保留原快照，提交结果前再次恢复身份。
- 回归：`tests/audit/test_batch_one.py` 中窄JWT、grant扩大及重试、旧任务无快照、排队撤销和提交前撤销；覆盖source及trace上下文。
- 兼容：无数据库结构变更。旧OAuth任务无快照时失败关闭，不能自动补成完整grant；需要原用户以合适scope重新提交。旧人员/平台令牌任务沿用原有实时权限检查；新任务均保存上限。

### A02：生成文档输入来源限制

- 原因：模型输出仅引用普通MD时，曾发送的受限输入未计入权限。
- 修改：`ai_gateway.py::run/validate_inputs/finalize_document` 记录实际发送的至多20项输入证据、制品、摘要和source/trace标签；发送、响应验证与落库均复核。生成制品继承所有已发送材料的标签，模型引用只负责追溯，无法覆盖服务器provenance。
- 回归：人类管理员输入含源码/Trace、输出只引用普通MD，普通令牌在人工批准前后均无法从workspace或制品下载读取生成内容；未发送的源码不扩大限制；输入分类在提交前变化时丢弃结果。
- 兼容/边界：无迁移。`reading.py::artifact_policy` 对缺少可信provenance的历史生成品在读取时保守要求source和trace权限，并沿原制品指针保留限制；不能从输出引用推断全部输入，应重新生成以恢复准确标签。本次未改写现有数据库或历史制品。

### A03：文档替换共用锁与审核

- 原因：导入直接切换Selection，既不验证已有锁，也能用imported类型绕过工作流。
- 修改：`documents.py::match_sections/assert_section_locks` 由保存与导入共用；`knowledge.py::persist_bundle` 复用章节身份、保留锁，受治理替换重置为editing；`assert_publishable` 独立检查治理标记及工作流，不依赖可变化的来源类型。Markdown导入改用CommonMark AST，兼容Setext标题。原始上传、旧修订和发布版不覆盖。
- 回归：同一机器身份直接改写及重导锁定章节均409；重导新增修订阻断发布；原样重导Setext章节保持锁身份。
- 兼容：无迁移。首次导入仍按原流程产生候选；替换已有内容需要新的修订审核。

### A04：精确撤销消失章节

- 原因：导入只添加/替换出现的实体，缺失章节继续参与草稿读取及diff。
- 修改：`persist_bundle` 仅对本次明确出现的文档计算章节差集、撤销缺失Selection；保留历史实体/修订，相关关系标为broken，其余变更标为stale，并清除失效的运行因果依据。增量包缺席不视为整个来源删除。
- 回归：发布→克隆→重导删减，当前章节、搜索、removed差异一致；其他文档和原发布版保持不变。
- 兼容：无迁移。已有残留草稿需要重新导入相应文档才能修正，未批量改写私有实例。

第一批实际验证：新增首组用例先出现8失败/3通过，随后补充CommonMark边界；修复后运行
`VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/audit/test_batch_one.py tests/documents tests/ingest tests/models tests/oauth tests/jobs tests/knowledge tests/versions`
得到 **101 passed / 1 skipped**（RabbitMQ专项未提供隔离broker）。基础Ruff错误检查通过。均为合成验证，未进行真实业务验收。

## 第二批 A05–A07

### A05：一致的讲解关系策略

- 原因：前端忽略action context返回值，自行沿所有关系取卡片。
- 修改：`reading.py::explanation_relation/_graph/action_context` 默认只遍历confirmed+valid；候选与失效关系通过独立策略读取，rejected不进入讲解。`pages.tsx::Workbench` 使用服务端返回的实体/关系/证据，所有标签页共用同一结果，异步返回按上下文校验并取消过时请求；失效视图独立展示审核、有效性和因果字段。
- 回归：`test_batch_two.py`覆盖confirmed/candidate/rejected/stale/broken；真实浏览器覆盖候选独立展示和审核拒绝后的接口列表；`test_audit_navigation.py::test_a05_all_knowledge_tabs_follow_retarget_hotspot_and_version_migration`通过真实应用服务改选目标、修改热点、生成版本迁移，逐次核验接口/实现/文档/证据四个标签页与当前服务策略一致。
- 兼容：HTTP action context新增可选relation_policy，默认trusted；知识地图及审核页仍保留原有完整关系语义。设计原件未改，运行契约同步。

### A06：逻辑来源冲突保护

- 原因：文档key仅依赖路径，Entity查找未核对逻辑来源。
- 修改：`knowledge.py::document_origin/persist_bundle` 保存逻辑source key；历史数据沿原制品指针推导，不以快照ID作为来源身份。跨来源同key返回409并提示来源路径前缀。`exports.py::reimport_bundle` 保留逻辑来源以兼容合法回导。
- 回归：同路径backend/frontend明确冲突，带来源前缀的两个README独立保留；跨文档复用显式章节key同样409；原有回导与原字节测试通过。
- 兼容：不修改已有stable key或发布历史，无迁移。曾经被静默覆盖的历史无法凭当前状态自动恢复，需检查原材料后显式重导。

### A07：读取与导出关系过滤一致

- 原因：导出过滤证据后仍保留空证据关系。
- 修改：`reading.py::relation_is_visible` 供读取、讲解图和`exports.py::visible_release`共用。端点不可见或可见证据为空时，关系、理由及相关计数不进入后续导出。
- 回归：两端可见而全部证据受限，workspace、Markdown bundle、PDF渲染输入、可回导VKS包均排除该关系。
- 兼容：无迁移；只收紧受限读取结果。

第二批先复现6失败/1通过，修复后相关合成回归 **64 passed**；真实浏览器专项 **1 passed**；前端30项及构建通过。执行命令：
`VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/audit tests/review tests/exports tests/mcp tests/backend/test_application.py tests/performance/test_reading.py tests/contracts`；
`VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/ui/test_frontend_browser.py -k a05`；`npm test`；`npm run build`。

## 第三批 A08–A11

### A08：有限业务头和隔离Cookie轮换

- 原因：请求头允许列表漏掉业务CSRF头；普通响应字典丢失多个Set-Cookie。
- 修改：`network.py::NetworkPolicy/ResponseHeaders`、`auth_registry.py::rotated_cookies`、`runner.py::run_capture`；环境的`allowed_request_headers`只能按准确origin登记四种有限业务头，策略纳入审批摘要。私有认证引用不下放给前端。独立保存多个Set-Cookie并校验host/path/Secure/SameSite/有效期，只更新同源隔离会话；认证及Cookie不转发其他origin。有限CORS响应头保留，不把合法跨来源请求一律阻断。
- 脱敏：新增Cookie和业务头秘密加入DOM/截图/路径脱敏，覆盖HTML及URL编码形式，不保存原始凭据、Cookie头或响应报文；保持网络隔离与Chromium沙箱。
- 回归：`tests/runner/test_cookie_rotation.py`中实际Chromium访问两个合成服务，服务器校验CSRF、返回多个Cookie，第二次请求验证新会话；跨origin有正常CORS读取且凭据为空；越界Cookie、非法头和HTML转义秘密覆盖。先红后绿。
- 兼容/风险：无迁移。新环境字段默认空；更改允许头使原审批绑定失效，必须重新审批。尚未对真实业务认证协议验收；不允许任意头透传。

### A09：搜索动作使用所属页面

- 原因：旧pageId覆盖新选动作的页面。
- 修改：`App.tsx::SearchDialog`、`pages.tsx::Workbench`通过共享`navigation.ts::resolveExplanation`更新页面和动作；显式动作选择优先解析所属页面，无动作页面仍可独立选择。
- 回归：`test_audit_navigation.py::test_a09_search_b_action_overrides_previously_selected_a_page`在真实浏览器先选A，再搜索B动作，断言URL、页面、热点及技术侧栏一致。
- 兼容/风险：无迁移；无页面映射的动作不猜造页面。真实业务的映射质量尚需验收。

### A10：固定比较输入与结果标签

- 原因：releases数组引用变化重置目标，结果未记录比较输入。
- 修改：`pages.tsx::Versions`仅在选择失效时设置默认值；结果保存请求时的from/to，标题显示准确基线；改变选择后隐藏旧结果，刷新不会重置合法选择。
- 回归：`test_audit_navigation.py::test_a10_comparison_keeps_requested_target_through_refresh`在工作区A执行R→B，比较完成及后台刷新后均维持R→B；更换选择不冒充旧结果。
- 兼容/风险：无迁移；差异结果仍是一次请求的快照，草稿继续编辑后应重新比较。

### A11：项目分页、搜索及深链接

- 原因：前端只读前20个项目并丢弃分页metadata，深链接落在后页时回退首项目。
- 修改：`api.ts::apiPage`保留完整metadata，原`api`调用语义不变；`App.tsx::loadProjects`增加搜索、加载更多及过时请求保护。深链接项目独立GET鉴权并固定当前选择。`service.py::list_projects`和`app.py`提供授权后的key/name搜索及cursor/limit。
- 回归：`test_project_pagination.py`、`api-pagination.test.mjs`及浏览器`test_a11_later_project_deep_link_and_project_pagination`；26个合成项目，后页选择、搜索、深链接刷新均保持正确项目。
- 兼容/风险：无迁移；查询参数是可选扩展。项目选择只显示当前身份有权限的项目，未扩大授权。

第三批首轮浏览器3项均复现失败，修复后3通过；后端/Runner/任务相关回归34通过、1跳过，前端31项及构建通过。执行：
`VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/runner/test_cookie_rotation.py tests/audit/test_project_pagination.py tests/runner tests/jobs`；
`VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/ui/test_audit_navigation.py -k 'a09 or a10 or a11'`；`npm test`；`npm run build`。

## 第四批 G01–G02

### G01：统一被动讲解导航

- 原因：工作台page/action与场景scenario/state各自维护，热点目标、分支和URL不能恢复同一上下文。
- 修改：`navigation.ts::resolveExplanation`和`App.tsx`集中处理analysis/release/scenario/state/page/action；`Workbench`、`ScenarioStudio`、页面树、搜索及hash/前后退均调用同一导航。热点target_state_key只选择已声明或已采集的状态；歧义或缺失目标明确报错，不生成内容、不请求业务系统。观察状态由`CaptureResults.tsx::ObservedStateSurface`按当前基线鉴权读取。
- 回归：`navigation.test.mjs`三项纯状态测试；真实浏览器`test_g01_hotspot_branch_page_tree_and_url_use_one_passive_context`覆盖热点目标、场景分支、页面树、URL恢复/刷新/前后退、无动作页面及外部请求为零。后端畸形状态图先复现错误，再补Schema优先验证，返回受控输入错误。
- 兼容/风险：无迁移；新增URL键可选，原链接继续归一化。无法唯一定位的跨场景目标要求显式选择，不猜测业务状态；真实项目映射未验收。

### G02：查看采集结果与不可变绑定

- 原因：已有采集产物和场景修订，但缺少产物查看、加入状态及绑定已有动作的闭环。
- 修改：`CaptureResults.tsx`展示脱敏截图、被动DOM和请求摘要；`ScenarioStudio.tsx`提供“查看采集结果”“加入讲解状态”“关联已有动作”。`scenarios.py::bind_capture`及新增capture详情接口校验同项目、成功采集、相同scenario、合法知识基线、准确当前修订/If-Match、合法截图索引和产物归属；幂等创建新场景修订，保留旧修订。手工页面/动作绑定标记declared，截图状态标记observed；不覆盖既有declared/simulated状态，不提升因果字段。审批绑定原修订，修改后不得沿用旧采集审批。
- 回归：`test_capture_bindings.py`覆盖新修订/旧修订不变、三种来源保留、因果不升级、跨项目/基线/截图/场景/修订冲突与幂等；真实浏览器`test_g02_view_capture_bind_action_and_restore_observed_state`实际查看合成采集、绑定已有动作、切换观察状态及刷新恢复。采集所用合成服务在阅读测试前停止，阅读不触发业务请求。
- 兼容/风险：无迁移，使用现有不可变JSON修订；运行Schema增加可选pageKey/actionKey、绑定来源和snapshotIndex，展开MCP Schema同步生成。老采集无法证明当前基线时失败关闭；真实页面采集布局和业务验收未执行。

第四批后端绑定首轮7失败，修复后7通过；G01浏览器实际复现状态不跳转后修复通过，G02浏览器通过。随后补充畸形图、转义Cookie和旧生成品边界；准确最终结果见下节。

## 最终验证与交付边界

本节最终结果以完整重跑和`validation/results/audit-remediation-20261025.json`为准，分批数字是阶段记录，不累加为总数。初次全量检查曾有1失败：自举测试依赖旧的“默认返回候选”语义；现补充候选专用读取及明确人工确认，再用真实MCP SDK核验confirmed+valid结果，没有放宽服务策略。

最终于2026-10-26执行`make check`：**298 passed / 11 skipped**（109.24s），前端**34 passed**，TypeScript/Vite构建成功（16.92s）。Python包含11项Web UI真实隔离浏览器回归，其中7项为本轮新增；Runner采集用例另计在Python总数中。相比审计基线新增45项Python用例、4项前端用例。Prettier、基础Ruff、`git diff --check`通过，依赖审计0漏洞；原设计44文件字节一致。详细命令及阶段专项结果见[VERIFICATION.md](VERIFICATION.md#审计修复当前结果2026-10-26本地验证快照)。

| 范围 | 最终执行入口 |
| --- | --- |
| A01–A04 | `uv run pytest -q tests/audit/test_batch_one.py`（15项，包含source/trace参数化） |
| A05–A07 | `uv run pytest -q tests/audit/test_batch_two.py`（8项）；`tests/ui/test_frontend_browser.py`中的A05浏览器用例 |
| A08 | `uv run pytest -q tests/runner/test_cookie_rotation.py`（5项） |
| A05、A09–A11、G01–G02浏览器 | `uv run pytest -q tests/ui/test_audit_navigation.py`（6项） |
| A11服务/前端 | `tests/audit/test_project_pagination.py`；`apps/web/tests/api-pagination.test.mjs` |
| G01–G02后端 | `uv run pytest -q tests/audit/test_capture_bindings.py`（9项） |
| 全量（包含上述用例、契约和实际MCP SDK） | 清除继承的VKS配置后，以隔离数据目录运行`make check` |

所有Python测试命令都使用隔离数据目录`.vks/audit/runtime`，实际业务/数据库/模型配置不继承。原44个设计文件的大小与SHA-256由契约测试逐项核验。编码、`git diff --check`、前端Prettier、基础Ruff及依赖审计另行检查。私有红/绿日志保存在忽略目录`.vks/audit/`，可共享的源码摘要与结果JSON在`docs/validation/results/`。

**未执行项及原因（适用于所有编号）：** 没有运行Maven、真实业务/客户资料、真实模型或IdP、生产/现有实例迁移、PG/RabbitMQ/SVN/Compose等环境专项、远端CI、提交/推送/PR或发布；本次仅授权本地合成验证，环境专项未启用。此前文档中的容器/试点/性能结果仍绑定原快照，不能充当本次通过证据。保留大型图表按需分块的既有构建告警，本次未重跑容量门禁。

环境专项仅提供命令，不表示已执行：

```bash
# 使用另外建立的隔离测试资源，不能填写现有业务数据库或消息队列地址。
VKS_TEST_POSTGRES_URL='<隔离PostgreSQL测试URL>' uv run pytest -q tests/backend/test_postgres.py
VKS_TEST_BROKER_URL='<隔离RabbitMQ测试URL>' uv run pytest -q tests/jobs/test_rabbitmq.py
VKS_TEST_SVN_DOCKER=1 uv run pytest -q tests/collector/test_svn_container.py
VKS_TEST_COMPOSE=1 uv run pytest -q tests/deployment/test_compose.py
VKS_TEST_RECOVERY_SOURCE_URL='<隔离恢复演练URL>' uv run pytest -q tests/recovery/test_postgres_backup.py
```

**数据库与回滚：** 本次无新增DDL/Alembic迁移，也未连接/修改正在使用的实例数据库；自动回归仅在临时数据库检查已有迁移。不需数据库回滚脚本。授权快照、provenance、逻辑来源及场景绑定使用现有JSON字段。未来部署须同步更新API/Worker/Web；旧OAuth任务重新提交，未知provenance生成品只在受限读取下保留。回退到旧代码会重新引入本次权限缺陷，不能作为安全回滚方案；如需撤销功能，应停止新增生成/绑定入口并保留新读取边界，不自动删除历史修订。

**完成状态：** A01–A11及G01–G02均已完成本地代码修复/闭环并有合成回归。真实业务验收尚未执行；本次未重启或验收现有API/Worker/Web。开发前端HMR可能读取本地文件，不能据此宣称现有实例已完整加载修复。

后续GitHub交付中修正了原图表超时回归的测试隔离，产品代码不变；补充源码摘要、实际命令和CI状态边界见[验证记录](VERIFICATION.md#github交付补充检查2026-10-26)。原审计验证快照保留，不以重跑覆盖失败记录。
