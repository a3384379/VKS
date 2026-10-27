# VKS 1.0 候选版验证记录

2026-10-27：2.0 开发预览当前全量检查为 Python 334 passed / 11 skipped、前端 36 passed、构建成功；源码摘要、阶段失败与修复、环境及真实验收边界独立记录在 [2.0 验证记录](upgrades/v2/VERIFICATION.md)。下文 1.0 快照仍绑定各自的源码，未被改写为 2.0 验收。

日期：2026-10-25，版本1.0.0-rc.1。原包QUALITY_CHECK.md是设计交付检查；本文件记录代码与运行验证。以下本地结果及机器记录均绑定各自的源码快照，远端结果以 [GitHub Actions](https://github.com/a3384379/VKS/actions) 对应提交为准；自动检查不代表生产发行或真实业务验收。

## R01 / R02 复审当前结果（2026-10-26，本地未提交）

基线`b8926b0d2665034f24d85a534ec258d544398990`。已独立复现固定文档修订混入后续章节、合法显式章节key被随机化；首轮2失败、1通过。修复记录、旧数据缺口和兼容说明见[R01/R02复审记录](AUDIT_REAUDIT_R01_R02.md)。

清除继承VKS配置，使用隔离数据目录及local任务模式执行`make check`：Python **314 passed / 11 skipped**，113.21s；前端**34 passed**；TypeScript/Vite构建成功，16.26s。新增13项专项（13 passed，1.47s），覆盖冻结历史、旧数据恢复/歧义、发布一致性、生成/编辑、显式身份/换位/证据历史、跨来源/文档，以及实际MCP SDK和HTTP固定修订读取。阶段相关模块100项通过；Prettier、基础Ruff、差异/编码/原设计完整性检查通过。

11个环境专项跳过，未计作通过。当前修改仅本地，未提交/推送/运行远端CI、部署或迁移现有数据库；真实业务、模型、IdP与容量验收未执行。无新增DDL；旧数据无法唯一恢复时明确409，不以最新章节替代。原设计44文件保持字节完整。机器记录和当前源码摘要见[本次结果](validation/results/document-reaudit-20261026.json)、[本次摘要](validation/results/document-reaudit-source-manifest-20261026.json)。下列历史结果仍绑定各自源码，不是当前修复的远端验收。

## PR审查补充当前结果（2026-10-26）

本节保留上一轮合并时的快照；本次R01/R02修复结果以上方章节为准。

审查补充修复：重导保留继承章节key，为冲突的未匹配章节分配新身份；失效项目深链接明确403/404时保留成功列表并清理旧基线/实体/场景，合法后页项目仍单独鉴权。新增一个章节身份回归及两个403/404浏览器回归，先复现失败再修复。

另复现了合成Cookie用例的异步写窗口问题：刷新可能在click步骤结束后才发起，此时Runner按既定规则拒绝写请求。测试改用已有受批`goto + networkidle`等待合成初始化刷新完成，后续断言保持read_only；加入250ms合成延迟验证该边界。没有修改Runner授权、DSL契约、网络隔离、沙箱、超时值或添加重试。Runner文件5项通过，2.22s。

清除继承VKS配置后最终运行`make check`：**301 passed / 11 skipped，111.61s**；前端**34 passed**；TypeScript/Vite构建成功，16.57s。包含13项Web UI浏览器回归。Prettier、基础Ruff及`git diff --check`通过。11项跳过的环境专项、上游弃用告警和既有大型按需图表分块告警仍如实保留。

当前源码摘要与机器记录见[audit-review-source-manifest-20261026.json](validation/results/audit-review-source-manifest-20261026.json)、[audit-review-followup-20261026.json](validation/results/audit-review-followup-20261026.json)。此前各摘要与失败/成功记录均保留；当前head的GitHub门禁、合并及main检查以[PR #1](https://github.com/a3384379/VKS/pull/1)及对应Actions为准。无新增数据库结构迁移、真实业务验收或部署。

## 审计修复当前结果（2026-10-26，本地验证快照）

审计基线`c5c93eec69f25bfc22b0ec9f947c8c7dc94baf64`，本轮从2026-10-25开始。A01–A11、G01–G02逐项原因、文件/函数、回归、兼容和剩余风险见[AUDIT_REMEDIATION.md](AUDIT_REMEDIATION.md)。以下是当前本地源码的结果；后续历史章节不作为本次证据。

| 实际执行 | 结果 |
| --- | --- |
| 清除继承的`VKS_*`配置，以隔离数据目录和local任务模式运行`make check` | Python **298 passed / 11 skipped**，109.24s；前端 **34 passed**；TypeScript/Vite构建成功，16.92s |
| 其中Web UI真实浏览器回归 | 共11项通过，含新增7项：关系拒绝/改选/热点/迁移后的四标签页、跨页面搜索、比较刷新、26项目分页/深链接、统一被动状态导航、采集查看/绑定/恢复；Runner采集用例另计在Python总数中 |
| `VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/runner/test_cookie_rotation.py tests/audit/test_capture_bindings.py tests/selfhosted` | 16 passed，12.15s；轮换及脱敏、绑定/畸形图、人工确认后的SDK自举闭环 |
| `VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/audit/test_batch_one.py -k human_output` | 2 passed，13 deselected，0.78s；人工批准前后均保留source/trace限制 |
| `VKS_DATA_DIR=.vks/audit/runtime uv run pytest -q tests/ui/test_audit_navigation.py -k all_knowledge_tabs` | 1 passed，5 deselected，6.70s；随后纳入最终全量检查 |
| `npm run format:check` | Prettier通过 |
| `uv run ruff check --select E9,F63,F7,F82 services packages tests` | 通过 |
| `npm audit --audit-level=high` | 0 vulnerabilities |
| `uv run python scripts/build_contracts.py`及全量契约/SDK测试 | 运行Schema和展开MCP契约一致；原设计44文件大小及SHA-256一致 |
| 编码与差异 | 修改文件保持UTF-8、无BOM、LF；新文件同约定；`git diff --check`通过 |

11个跳过项为PG6、RabbitMQ1、SVN容器1、Compose1、PG恢复1、真实FlowTest1，当前未启用隔离环境或真实项目读取；没有计作通过。没有使用真实凭据、IdP、外部模型、业务网络或客户材料；未运行Maven，未迁移现有数据库。全量仍有上游Starlette弃用告警及既有图表分块体积告警。未重跑600秒容量测试，也没有远端CI或真实业务验收结果。

源码内容摘要（不含文档）及机器记录分别为[audit-source-manifest-20261025.json](validation/results/audit-source-manifest-20261025.json)、[audit-remediation-20261025.json](validation/results/audit-remediation-20261025.json)。私有红/绿日志在忽略目录`.vks/audit/`。机器记录保留审计验证时尚未提交的事实；后续经用户另行授权的GitHub提交、PR与合并以远端记录为准，不改写本地验证快照。未发布或重启现有服务；开发前端HMR不代表API/Worker已同步加载修复。无新增数据库迁移；旧OAuth任务、历史生成品和来源冲突处理见逐项报告。

## GitHub交付补充检查（2026-10-26）

[PR #1](https://github.com/a3384379/VKS/pull/1)的首个head `b2f77ad`：push检查通过；PR检查中297项通过、11项跳过，原有图表超时浏览器用例1项失败。测试用含六张图表的文档，却以任意iframe的出现作为第一张图表15秒超时的起点；第一张图表可能仍在两任务队列后等待。

现通过真实草稿API将该隔离测试文档保存为单图并刷新准确修订，确认仅有一个图表任务，再断言原15秒超时、重试与导航取消。没有修改产品代码、超时值、浏览器沙箱或添加自动重试。清除继承VKS配置后运行`uv run pytest -q tests/ui/test_frontend_browser.py`，**5 passed，25.67s**；基础Ruff及`git diff --check`通过。新head的远端结果以PR当前检查为准。

补充结果见[audit-ci-followup-20261026.json](validation/results/audit-ci-followup-20261026.json)，完整源码摘要见[audit-ci-source-manifest-20261026.json](validation/results/audit-ci-source-manifest-20261026.json)。此前298项本地全量结果仍绑定原摘要，不改写为本次测试修订的验证结果。

## 审计前自动检查（历史快照）

| 项目 | 实际结果 |
| --- | --- |
| `make check` | Python **253 passed / 11 skipped**，76.70s；前端 **30 passed**；TypeScript/Vite构建成功 |
| 默认跳过项 | PG6、RabbitMQ1、SVN容器1、Compose1、PG恢复1、真实FlowTest1；需要显式隔离环境，不作为默认通过项 |
| 真实FlowTest只读专项 | 另行 **1 passed**，5.14s；固定Git字节、导入和原工作区status不变 |
| 最新Runner | **23 passed**，21.91s；包括Origin/Cookie-CSRF、并发24资产唯一ID、SPA授权窗口 |
| PostgreSQL17.6 | 另行 **6 passed**；发布/冲突/幂等/重复抢占/读取权限等；最新h71 migration升→check→降base→升→check通过 |
| RabbitMQ4.1/Celery | 真实队列 **1 passed**；数据库outbox、重复投递/可见结果幂等 |
| SVN | 客户端及真实隔离svnserve；真实容器专项 **1 passed**，固定数字revision与只读边界 |
| 备份恢复 | SQLite/CLI回归；真实PG dump/snapshot/restore专项 **1 passed**，空目标、引用对象与凭据失效 |
| 完整Compose | **1 passed**，72.53s；PG/broker/API/worker/web健康、真实HTTP、发布/PDF/回导、API与worker重启恢复；只清理临时测试栈 |
| 格式与基础静态检查 | Prettier通过；Ruff E9/F63/F7/F82通过 |
| 依赖检查 | `npm audit --audit-level=high`：0漏洞；uv/requirements与npm锁文件固定 |
| 原设计包 | 44文件大小及SHA-256逐项一致 |
| 契约与SDK | OpenAPI3.1 / JSON Schema2020-12、18个MCP输出Schema；真实官方SDK stdio/Streamable HTTP、资源/模板、同版闭环、撤销 |

默认Python结果只有上游Starlette TestClient/httpx弃用告警，不影响测试通过。本次容器验收记录固定版本及实际镜像ID；CI包含独立Chromium安装和PG/消息队列专项。上表及此前JSON是本地验证快照，远端运行单独记录，计数不叠加。

## 行为与故障覆盖

- 导入：ZIP路径/链接/炸弹/配额、内容摘要、编码/秘密、来源向量与引用闭合；独立坏文件仅在无绑定依赖时隔离为受限原始文件，成功材料仍可读；哈希/安全破坏不降为普通警告。
- 身份与权限：本地团队角色/敏感授权、禁用/撤权、CAS、最后管理员保护；按项目/版本/源码/Trace/模块过滤名称、计数、附件、导出和模型上下文。权限不跨请求缓存。
- 文档与版本：AST章节稳定迁移、原始图片、锁/冲突/提交审核、不可变发布、克隆/diff/影响、stale/broken及迁移复核、默认版回退。自动确认静态候选不会创造运行因果。
- 任务：真实队列/worker；杀进程、重复投递、lease过期/接管、解析检查点、对象失败、取消与迟到输出丢弃；检查点处理器摘要包含所有解析入口。
- Runner：真实Chromium，严格定位/环境/认证作用域、IP固定/SNI/跳转/元数据/写路径、采集前脱敏、有限DSL/分支。没有Trace/OTel则网络只产生时间关联候选。
- 模型/OAuth：实际本地合成HTTP服务验证结构化输出、精确引用/无证据拒绝、材料出口禁止、预算预留/失败计费、撤权/取消后丢弃；RS256 issuer/audience/client/scope/登记授权及撤销用真实JWT/SDK验证。没有真实模型供应商或企业IdP联调。
- 导出/恢复：原始与衍生材料分开；下载时重新校验权限；中文PDF、回导包的状态降级、历史版哈希；一致快照及对象校验，恢复旧会话/令牌/OAuth授权失效并生成新bootstrap。

中文PDF已生成并逐页渲染检查。当前捆绑Poppler缺少中文CID CMap，使用本机Quick Look核验字体布局；未修改全局字体/安装。私有运行产物保存在忽略目录`.vks/acceptance/`和`output/playwright/`。

## 浏览器与真实项目

独立Chrome for Testing会话完成了导入/预检/任务、动作↔章节、人工审核/发布、导出/哈希、搜索/局部图、热点新增/拖画/编辑、授权图片/高亮、团队/项目资料/审计筛选。原合成发布版4热点只触发VKS读取。

用户选定FlowTest后，固定真实Git源码及原使用手册，运行OpenAPI来源单列。30动作、118章节和两套独立API声明形成草稿。临时FlowTest副本使用tmpfs数据库和合成账号，完成页面可读、空表单校验、只读限制三条真实Runner采集；每条1脱敏快照/57请求、ID唯一，登录refresh200，业务写请求0；只读API创建独立403。官方SDK又通过实际VKS HTTP读取同版动作/证据并撤销临时令牌。[完整试点记录](FLOWTEST_PILOT.md)。

实际VKS源码/OpenAPI自举包含8动作/2源码文件/32静态候选，通过官方SDK逐动作读取实现及文档证据。自举关系仍为候选，另一名开发者的独立用户任务未验收。

## 性能分母

- 50,000关系 / 1,000动作 / 200MD，20并发真实HTTP连续600秒，34,197请求0错误；动作/搜索/图谱p95分别496/338/497ms。最终元数据修订另跑20秒1,021请求0错误，源码摘要匹配。
- 5,000合法Java文件恰好100MiB，manifest另计1.455MiB、5,001条目、ZIP约59.09MiB；真实HTTP20并发提交p95=0.563s，完整durable job=11.519s、0issues；取消发出后104.416ms退出。
- 以上为10CPU/16GiB Mac、SQLite的PoC；与设计8vCPU/16GiB、SSD、固定网络、正式部署PG剖面不同。没有以此签署正式容量门禁，也没有把零模型调用当作真实模型验收。

完整分位数/资源曲线、冷暖分布、材料/源码SHA与复现命令见[性能记录](validation/PERFORMANCE_POC.md)。合成Java Gold为39TP/0FP/1FN、调用6/11的保守支持结果，不是独立人工盲审或真实业务总体准确率。

## 尚需外部验收

代码完成及自动检查之外，仍需：与选定源码一致的业务测试构建、FlowTest业务人员审核与独立Gold/用户任务、指定基准机容量、实际模型供应商/IdP联调，以及生产部署发行。当前来源版本不一致保持unknown；试点不自动批准或发布。设计AT逐项对应见[ROADMAP_V1.md](ROADMAP_V1.md)。

## 2026-10-25 前端故障修复

真实复现了文档中心白屏：延迟加载的lowlight收到Vite过期预构建依赖504，未处理的模块加载失败使React根树退出。现已预构建该依赖、给代码高亮添加纯文本降级、为工作区页面设置错误边界，并重启当前开发服务。

同时修复：URL hash/前后退与页面状态不同步；编辑后立即跳转时未保存状态传递滞后；文档目录未按原始行号排序。全套回归还发现改选静态关系目标会继承旧推断依据，已改为新候选待建立，并分别覆盖动作/API来源，避免依赖随机实体顺序。

实际当前工作区通过owner密码登录，三个项目八入口共24页无页面异常或失败HTTP，FlowTest正文及18个代码块高亮正常。新增隔离API/Vite缓存/Chromium回归主动阻断高亮模块，验证正文仍可读、八入口、空文档、深链、前后退及立即跳转时的草稿保护。最后路由增强后专项2项通过；目录修复后前端26项与构建通过。Python全量251项通过、11个环境专项跳过，不重复计入前次外部容器验收。

初始管理员现为owner/owner；使用scrypt存储，只对新建实例设置默认值，重启不覆盖已改密码。当前实例按用户请求重设并撤销旧凭据；普通用户及密码修改仍保留原校验。修复前源码摘要另存candidate-source-manifest-before-ui-hotfix.json；当前源码摘要与[本次结果](validation/results/ui-hotfix-20261025.json)对应。性能测量仍绑定其JSON记录的原源码快照，本次没有重新执行600秒容量门禁。

## 2026-10-25 Markdown 图表与公式升级

文档阅读层新增Mermaid流程/时序图、PlantUML原生JavaScript时序/类图，以及KaTeX行内/块级/矩阵公式；math/latex/tex代码块别名可用。图表按需在opaque-origin iframe生成，消息来源/ID核验，清理SVG后以Blob图片展示。无Java、无第三方画图请求，不修改原始Markdown。

真实Chromium四项专项覆盖：正常图表与输出文字、公式、两类语法错误降级、尺寸/样式/事件清理、实际缩放与下载、15秒超时后重试、导航取消与隔离。单测30项覆盖GFM、数学及资源/配额策略。全量Python253通过/11跳过；最后自适应图表宽度调整后又执行四项浏览器回归及前端测试/构建，具体时间保存在本次结果。

生产构建经临时Nginx实际加载四个图表，配置检查通过，外部请求0、失败请求0，容器已移除。只给公开静态资源提供opaque-frame所需CORS，API及私有目录不开放；开发服务显式拒绝.vks/环境文件。KaTeX统一固定0.19.1以替换插件旧依赖，npm audit为0漏洞。

另外隔离了四个前端单测的Vite缓存，实际比较开发服务依赖摘要：运行单测前后完全一致；避免测试覆盖正在运行的依赖预构建，导致React/高亮模块504。当前开发服务已重启，示例项目可直接阅读。

构建保留大型按需分块告警：PlantUML约3.06MB（gzip1.04MB）、Mermaid核心约641KB。它们只在图表需要绘制时加载，未通过提高告警阈值掩盖体积；本次未重新执行600秒读取门禁。图表/公式语法及PDF等范围见[格式说明](MARKDOWN_FORMATS.md)，结果见[本次机器记录](validation/results/markdown-rendering-20261025.json)。

## GitHub 首次交付

源码仓库为 [a3384379/VKS](https://github.com/a3384379/VKS)，默认分支main、私有可见。本地数据库、凭据、原始FlowTest试点资料与运行产物不进入Git；原设计文件在Git中保持原始字节。

首次远端运行暴露Ubuntu AppArmor对下载版Chromium的用户命名空间限制：PG/消息队列通过，14项真实采集测试因浏览器沙箱无法启动而失败。CI按 [Chromium官方说明](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md) 仅为准确的测试浏览器路径加载临时机器内的AppArmor配置；未关闭系统级限制或采集浏览器沙箱。每次提交的最终结果见对应GitHub Actions运行；此前JSON仍记录其原始本地快照。
