# VKS 1.0 实施与验收跟踪

2026-10-25，1.0.0-rc.1。原设计47个功能点与35个AT是范围和验收要求，文档本身不是通过证据。实现细节见 [IMPLEMENTATION.md](IMPLEMENTATION.md)，实际命令见 [VERIFICATION.md](VERIFICATION.md)，真实项目见 [FLOWTEST_PILOT.md](FLOWTEST_PILOT.md)。

## 47功能对应

| 设计ID | 已交付行为 | 支持边界与验证 |
| --- | --- | --- |
| PRJ-01 | 项目资料、唯一key、业务域/负责人/分析语言、CAS | 成员可见；`tests/identity` |
| PRJ-02 | 模块树、来源/实体多对多映射、权限内计数 | `tests/modules` |
| PRJ-03 | 多来源快照、组合草稿、不可变发布manifest | `tests/backend`、`tests/versions` |
| PRJ-04 | ZIP/独立材料、目录/Git/SVN客户端、登记测试Web环境 | 不提供任意URL或服务器路径读取；`tests/collector` |
| PRJ-05 | 预检、编码/哈希/秘密/配额与引用闭合 | 原始字节保留；`tests/ingest` |
| PRJ-06 | 阶段/问题、独立文件部分成功、重试/取消/租约/检查点 | `tests/jobs`、`tests/knowledge` |
| PRJ-07 | 操作者/资源/动作/结果/日期/分页审计 | `tests/identity/test_audit_filters.py` |
| DOC-01 | Markdown AST章节、稳定身份、原文与授权附件 | `tests/documents` |
| DOC-02 | 目录/高亮、Mermaid/PlantUML/数学公式、保存冲突、人工锁、对比、提交/审核 | 来源引用只读；`tests/documents` |
| DOC-03 | 主体→章节、反向导航、同版精确定位 | `tests/backend`、浏览器 |
| DOC-04 | 可选模型草稿、逐段证据、预算/出口/发布审核 | 合成HTTP服务验证，真实提供方待验收；`tests/models` |
| DOC-05 | 原始/衍生MD、附件、中文PDF、可回导包 | 按当前下载权限裁剪；`tests/exports` |
| COD-01 | 项目/服务/method/路由身份、独立规范与源码来源 | `tests/analyzers`、`tests/ingest` |
| COD-02 | Java声明/参数/返回/重载/位置/快照 | Tree-sitter保守解析；`tests/analyzers` |
| COD-03 | MyBatis namespace/id/include、明确SQL与表 | 动态表达式保留缺口，不编造字段 |
| COD-04 | 可解调用、接口候选、条件路由/Feign、独立网络观察 | 不提供完整类型推断或Trace/OTel因果证明 |
| COD-05 | 源码字节/位置/摘要、移动与变化复核 | 唯一重定位失败则待复核/断链；`tests/versions` |
| UI-01 | 场景名称/模块/角色/条件/视口/数据集/结果 | `tests/scenarios` |
| UI-02 | 测试/Mock Runner、凭据引用、来源/IP/写路径审批 | 部署在有访问权限的本机/私网，不绕过VPN |
| UI-03 | 有限DSL、DOM/截图、路由/请求摘要/时间 | 采集前脱敏，不保存原始trace或报文正文 |
| UI-04 | 步骤快照、角色/视口/fixture、来源对齐 | 一次录制不代表全状态覆盖 |
| UI-05 | DOM/区域热点、重复/隐藏/冲突处理、新修订 | `tests/hotspots`、浏览器 |
| UI-06 | 被动讲解，点击只选择知识对象 | 浏览器网络断言 |
| UI-07 | 已录状态/声明的有限沙盒转移、模式标识 | 不执行来源脚本或真实业务写操作 |
| UI-08 | 成功/失败/校验等有限分支、声明/模拟/观察分类 | 观察状态绑定准确采集/场景修订 |
| UI-09 | HTML/Axure被动降级、图片区域热点、来源/日期 | 不运行Axure原脚本，剥离外联 |
| UI-10 | 场景预检/审核/随基线发布、旧版保持 | `tests/scenarios` |
| LNK-01 | 显式锚点、精确API/MD、Java、网络观察候选 | 每条有依据；语义结果仍待审核 |
| LNK-02 | 端点类型约束、documents方向和反向查询 | `tests/contracts`、`tests/review` |
| LNK-03 | 确认/拒绝/改选/拆分/合并、理由与CAS | `tests/review` |
| LNK-04 | 重跑保留拒绝/人工锁、变化待复核 | `tests/versions` |
| LNK-05 | 同版入边/出边、深度/节点上限 | `tests/performance`、SDK/浏览器 |
| LNK-06 | stale/broken、热点墓碑、历史证据 | `tests/versions`、`tests/hotspots` |
| LNK-07 | 纠错反馈和建议目标留存 | 不自动训练/上传或代替独立人工Gold |
| SRH-01 | 权限内全文/路径/符号、模块/类型/服务/版本筛选 | `tests/modules`、`tests/performance` |
| SRH-02 | 明确上下文的证据阅读及可选模型问答 | 默认不调用模型；`tests/models` |
| SRH-03 | 当前基线缺口与来源建议 | 不跨项目/版本补证；`tests/review` |
| VER-01 | 来源/符号/API/章节/锚点变化、纯注释区分 | `tests/versions` |
| VER-02 | 有限影响传播、动作/章节/场景路径 | `tests/versions` |
| VER-03 | 章节diff、迁移候选接受/拒绝/复核 | 克隆新草稿，不改历史版 |
| VER-04 | 预检、人员确认、冻结、默认版本回退 | `tests/backend`、`tests/versions` |
| MCP-01 | 18工具、资源/模板、复用Web服务和权限 | 官方SDK stdio/HTTP；`tests/mcp` |
| MCP-02 | 上传/manifest导入与预检、幂等任务ID | `tests/mcp/test_workflow.py` |
| MCP-03 | 提案/影响，机器不能确认/发布 | `tests/mcp`、`tests/review` |
| MCP-04 | 状态/取消/重试、断线查询原job | `tests/jobs` |
| MCP-05 | 令牌/scope/过期/撤销、外部RS256 OAuth资源服务器 | Web本地团队登录；不是企业授权服务器 |
| MCP-06 | 限量上下文、证据URI、分页/资源读取 | `tests/mcp`；不输出整个仓库 |

## 35 AT对应

“回归”指本地自动断言与记录的操作，不等同独立业务人员签字。

| AT | 当前证据与边界 |
| --- | --- |
| 01 | 真实目录/Git/SVN只读、哈希/快照回归；FlowTest固定Git原始字节试点 |
| 02 | BOM/CRLF/重复章节/授权图片/原始导出回归，中文附件浏览器 |
| 03 | 越界/炸弹/链接/摘要/秘密负例；脚本不执行 |
| 04 | 跨项目/服务/方法/条件及独立规范来源回归 |
| 05 | Java/MyBatis动态缺口与合成Gold；独立人工Gold未签署 |
| 06 | 章节移动/改名/歧义迁移和历史版回归 |
| 07 | 单独文档/原型/源码接入、能力降级回归 |
| 08 | 被动热点只读浏览器验证，真实FlowTest导航另留证 |
| 09 | 有限状态/模拟标记/目标校验、未审批写与外域阻断 |
| 10 | Chromium合成敏感数据入库前过滤；FlowTest独立合成账号 |
| 11 | 重复/隐藏/失效严格拒绝、编辑冲突/墓碑回归 |
| 12 | 来源脚本/事件/外联剥离、DOM净化与授权附件回归 |
| 13 | 精确MD/API、静态/网络观察分层及轮询负例；无Trace不声明确定触发 |
| 14 | 本地合成模型HTTP、引用/无证据/待审核阻断；真实提供方待验收 |
| 15 | 同版幂等/拒绝/人工锁、变化复核回归 |
| 16 | CAS/角色/撤权/人员确认及真实PG并发专项 |
| 17 | 动作↔章节同版浏览器导航、SDK同实体验证 |
| 18 | 局部图权限/上限/隐藏计数及50,000边PoC |
| 19 | 官方SDK导入→候选→人员发布→读取/资源/撤销；合成业务 |
| 20 | 同键原job/异载荷409、重复Worker/真实RabbitMQ |
| 21 | 协作取消、迟到丢弃、检查点/重试/租约回归 |
| 22 | 机器确认/发布、伪造状态、撤销负例 |
| 23 | 跨项目/源码/Trace/版本/模块/附件/导出/模型过滤负例 |
| 24 | 材料指令作为数据、模型无工具、精确引用和权限负例 |
| 25 | 固定IP/SNI、跳转/元数据/编码路径/链接、认证作用域负例 |
| 26 | 跨基线端点/采集拒绝，旧版不混最新证据 |
| 27 | 符号/API/SQL/章节/热点变化、传播/diff/复核 |
| 28 | 发布哈希冻结、克隆、默认回退、MCP显式版本 |
| 29 | 权限/秘密/附件、原始与衍生声明、PDF/回导 |
| 30 | PG/RabbitMQ重复投递、kill/restart/checkpoint/对象失败、完整Compose；独立坏文件部分成功 |
| 31 | SQLite/真实PG一致备份、空目标恢复、对象哈希、旧凭据失效；实际计时，不宣传未测RTO/RPO |
| 32 | 当前VKS源码/OpenAPI自举、8动作及实现证据通过SDK；另一名开发者任务验收待完成 |
| 33 | 合成解析Gold及状态分母；100关系独立盲审、5用户任务和真实准确率未签署 |
| 34 | 50,000边/1,000动作/200MD、20并发600秒HTTP零错误；正式硬件/PG门禁待验收 |
| 35 | 5,000文件/100MiB、HTTP/durable job/取消/预算；真实模型费用与指定环境另测 |

## 发行前外部验收

1. 业务人员确认FlowTest映射、冻结人工Gold并做盲审/用户任务；试点草稿不代替人工批准。
2. 采用与源码一致的授权测试构建补充业务路径；当前源码/运行版本分别保存为unknown。
3. 在指定8vCPU/16GiB、SSD、固定网络和部署数据库上复跑正式容量门禁；按实际供应商验收OAuth/模型速率、费用与超时。
4. 配置远端、CI、部署域名/TLS及备份保管，再完成发行。本地候选版不表示已推送、上线或全量业务验收。
