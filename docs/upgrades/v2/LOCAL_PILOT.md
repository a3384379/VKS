# VKS 2.0 本机 RuoYi / FlowTest 验证

2026-10-29。承接原开发对话及用户最新范围：不用云收费材料，先用本机 RuoYi 和 FlowTest；由当前 Codex 通过 MCP 创建任务和候选，不配置额外模型提供方。本轮约定的工程与本机验证已完成，独立人员测评和正式发行分别记录。

运行源码 `df2e3f77a109d0c178d2d8534ccb2cc6da244eba`，335 个工程输入聚合 SHA-256 `0bd5ce6f03f854a3f38813000f213a4cc96fb7ae4ee9de51fe67e1caa5980805`。完整 [源码清单](../../validation/results/v2-local-pilot-source-manifest-20261029.json) 与 [机器记录](../../validation/results/v2-local-pilot-validation-20261029.json) 固定本轮结果；历史报告不覆盖，远端检查另计。

## 固定资料与工程补齐

| 项目 | 固定 Git 修订 | 实际导入分母 |
| --- | --- | --- |
| RuoYi | `3b3941abeb5402297e5b4de82a30e6471c2239f3` | 六个管理模块；52 制品、6 页、25 个模板动作、89 API、553 符号、74 SQL、6 表、6 章节；初始 1,216 候选关系 |
| FlowTest | `cfe3594d055e1feb988df18041ed9dbedda8bc6b` | 82 制品、1 页、30 动作、912 API 声明、408 源码锚点、118 章节、443 候选关系 |

新增 [RuoYi 适配器](../../../scripts/build_ruoyi_pilot.py)，只读取用户、角色、菜单、部门、岗位、参数模块的固定 Git blob；保留源码、Mapper、模板和 README 原始字节。派生预览只有转义文本和被动按钮，不带源脚本、业务表单或外联。原模板作为源码证据保存，不执行。25 是模板实际动作数，不补造成 30 个 Gold 动作。

动作到 Java API 的 23 条候选在导入后通过 MCP 提交，引用准确修订及模板/Controller 证据；不在 sidecar 中引用尚未建立的 Java 实体。当前 RuoYi 共 1,239 条候选关系。动态编辑 ID、歧义或缺失目标保留未知。

部分范围解析保留 RuoYi 645 条、FlowTest 76 条诊断，包含范围外依赖和能力边界；预检有效不等于全部调用已解析。FlowTest 的源码 API 与运行 OpenAPI 单列，912 不是去重业务接口数。两项目运行镜像均不能证明与固定源码一致，来源对齐保持 `unknown`，无 Trace 不生成完整运行因果。

## 六条实际观察

只操作本任务带 `io.vks.validation` 标签的临时容器。FlowTest/MySQL 为独立 tmpfs 数据库，RuoYi 使用合成账号；没有用户目录或业务数据库 bind mount。未执行两个业务仓库的构建，已有未提交内容保持不变。

| 项目 / 路径 | 实际断言 | 脱敏快照 / 请求 |
| --- | --- | --- |
| RuoYi 页面可读 | 管理员打开新增用户表单，唯一“基本信息”标题可见 | 1 / 29 |
| RuoYi 校验失败 | 空表单保存，`userName` 必填错误可见，无保存请求 | 1 / 29 |
| RuoYi 无权限 | 无角色用户打开同一路径，显示“您没有访问权限” | 1 / 4 |
| FlowTest 页面可读 | 管理员进入合成项目，“接口管理”可见 | 1 / 57 |
| FlowTest 校验失败 | 新建接口空提交显示“请输入接口名称”，无创建请求 | 1 / 57 |
| FlowTest 无权限 | viewer 新建按钮禁用，独立创建 API 尝试返回 403 | 1 / 57 |

六条正式任务全部成功，请求 ID 各自唯一，越权业务写请求 0。FlowTest 每条仅一次准确 `/api/v1/auth/refresh` POST，均 200；RuoYi 无 POST。使用产品 Runner 在保存前脱敏；扫描本次随机凭据、Cookie、会话头均无明文，截图另做视觉核验。“页面可读”没有冒充新增用户、创建接口或执行工作流的业务成功路径。

保留原始准备失败：RuoYi 映射 8080 而应用监听 80；定位器命中两个标题；合成 viewer 密码超过 RuoYi 20 字符上限；FlowTest 随机前端 Origin 未登记而发生 CSRF 403。分别修正临时容器端口、唯一定位、合成账号和精确 Origin。未放宽 Runner 网络/定位策略、关闭 CSRF、增加断言超时，失败不计入上述六条结果。

## Codex / MCP 与被动回看

当前工具目录未直接挂载 VKS，使用官方 Python MCP SDK 启动真实 stdio 子进程，再访问当前源码的独立 HTTP API。两个专用项目令牌发现 23 工具，无 approve/publish，原 18 工具契约未改。

- MCP 导入两项目、读取动作/覆盖和准确模板/Controller 证据；Codex 提交 23 条关联候选、两条动作/接口流程、两条引用六份实际快照的三分支流程。分支为人工组织的测试状态选择，不冒充业务条件。
- 两项目各运行 `project_overview`、`module_analysis`、`evidence_gaps`、`business_flow`，共 **8 任务**，均完成 Plan→Collect→Analyze→Propose→Validate→Review，普通模板另生成两条流程候选。
- 机器批准流程返回 **403**，跨项目访问返回隐藏存在性的 **404**；撤销临时令牌后下一次请求均 **401**。知识仍为候选，没有代业务人员批准或发布。
- 两项目实际 HTTP 对比均返回准确快照、角色差异及 `mode=passive`。真实 Chromium 在 2.0 UI 中选择 success/forbidden 轨迹并显示两幅快照，每项目 **7 次 VKS GET、0 业务站点请求、0 写请求**。

平台模型和额外提供方调用均为 **0**，符合用户选择。这证明当前 Codex 的证据读取/MCP 提案及无模型模板编排；不记作平台自然语言模型成本/效果验收。

## 完整检查与交付边界

| 检查 | 结果 |
| --- | --- |
| 完整 `make check` | Python **449 passed / 14 skipped**，249.28s；前端 **42 passed**；TypeScript/Vite 成功 16.49s；总计 271.942s |
| 实际仓库专项 | `tests/pilot` **4 passed / 5.88s**；两项目 Git 字节与工作区不变、无脚本/正确转义、缺失模板拒绝；已含在全量，不累加 |
| 契约/SDK | 原有官方 stdio/Streamable HTTP、同版/权限契约包含在完整套件；另执行本次真实 stdio→HTTP API 闭环 |
| 静态与原件 | Ruff E9/F63/F7/F82、diff/UTF-8/LF；44 份 1.0 与 3 份 2.0 原件字节/摘要通过 |

14 项跳过为需显式环境的 PG、队列、SVN、Compose、恢复及容量专项，不计本轮通过。此前绑定各自源码的 Windows、PG/RabbitMQ、Compose、600 秒容量、5000 文件/1% 增量和升级回滚证据继续保留，未冒充本轮重测。原 Starlette 弃用及按需图表分块提示保留，未提高告警阈值。

临时容器、网络、两个测试浏览器会话和 VKS 进程均清理，用户日常服务未重启。私有 VKS 数据、原始失败、截图与网络记录保存在忽略目录 `.vks/local-pilot-20261028/`、`output/playwright/`，没有进入 Git/镜像。

用户已替换云收费试点并取消额外模型要求，两项不再阻塞本轮。独立冻结 Gold、五名人员配对测评、指定硬件/企业目标机验收和生产发行签署仍未执行；本机检查不代填这些记录，未宣布 GA、合并 PR 或切换现有实例。

```sh
PYTHONPATH=services/api:. uv run python scripts/build_ruoyi_pilot.py \
  --root /已授权的/RuoYi --revision 固定提交 --output .vks/ruoyi-pilot.zip
VKS_RUOYI_SOURCE_ROOT=/已授权的/RuoYi \
VKS_FLOWTEST_SOURCE_ROOT=/已授权的/FlowTest uv run pytest -q tests/pilot
```

重新采集须新建隔离账号和有效审批；已撤销令牌、已清理容器及旧审批不能复用。
