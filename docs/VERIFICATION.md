# VKS 1.0 候选版验证记录

日期：2026-10-25，版本1.0.0-rc.1。原包QUALITY_CHECK.md是设计交付检查；本文件记录代码与运行验证。以下本地结果及机器记录均绑定各自的源码快照，远端结果以 [GitHub Actions](https://github.com/a3384379/VKS/actions) 对应提交为准；自动检查不代表生产发行或真实业务验收。

## 最终自动检查

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
