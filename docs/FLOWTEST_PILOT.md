# FlowTest 真实项目试点

2026-10-25，用户指定使用FlowTest。试点项目在 [本地VKS业务工作台](http://127.0.0.1:5173/#view=workbench&project=5f817156-8c17-4d6d-a7ca-e7ee563cfd9d)，保持草稿；没有替业务人员确认或发布候选。

## 固定资料与版本

- 原仓库使用Git对象只读采集：`cfe3594d055e1feb988df18041ed9dbedda8bc6b`。选择76个原始源码/使用手册文件，原字节逐项与Git核对；不导入环境文件、数据库或个人浏览器配置，不执行FlowTest构建/脚本。
- 从实际运行后端读取OpenAPI，原字节SHA-256为`a8709ac1e9d430c597bb4b63ec1e02eefac43046ab1c9696adbe101ec4c32345`。运行镜像标识`c4e2a5f58fb2f13e4295f2bd790830f5ed8ef073+working-tree`，与源码不同，因此来源对齐保持`unknown`。
- 原始资料与6个派生索引组成82材料文件。导入形成30个导航/操作锚点、118章节、912个API声明及443条静态候选。912包含独立的源码声明目录和运行OpenAPI目录，不是912个去重业务接口；相同method/path不自动合并两个来源。
- Python/FastAPI使用标准AST定位明确声明，React/TypeScript使用显式源码锚点；不宣称已交付Python/TypeScript完整静态调用图。来源manifest禁止平台模型出口，模型调用数为零。

构建适配器：[build_flowtest_pilot.py](../scripts/build_flowtest_pilot.py)；导入脚本：[seed_flowtest_pilot.py](../scripts/seed_flowtest_pilot.py)。原始包和运行契约可能含私有项目资料，仅保存在忽略目录`.vks/pilot/`，不加入Git。

## 独立运行验证

使用已有FlowTest镜像创建临时容器，独立tmpfs数据库、合成管理员/只读用户/项目，前端文件只读。未共享日常FlowTest数据库、账号或数据卷，也未重启日常服务。每个采集使用新会话与独立Chromium上下文，只有准确的会话刷新路径获准POST，其他业务写请求阻断。

| 路径 | 真实断言 | 保存结果 |
| --- | --- | --- |
| 页面可读 | 登录刷新200；主区域显示“接口管理” | 1脱敏快照、57请求摘要 |
| 输入校验 | 打开“新建接口”，空表单显示“请输入接口名称” | 1脱敏快照、57请求摘要；未创建接口 |
| 只读限制 | 只读用户的新建按钮禁用；独立API创建尝试返回403 | 1脱敏快照、57请求摘要；未创建接口 |

三条均成功。每条请求ID唯一、业务写请求为0；只有合成会话refresh。截图/DOM/网络摘要入库前脱敏，扫描未发现本次实际测试密码或Cookie。运行源码对齐仍未知，网络时间关联不会变成确定触发或生产调用链。

实际试点发现并修复：合法浏览器Origin在来源白名单内原值转发，缺省/无效Origin不伪造；并发请求ID及步骤/风险窗口在Playwright回调入口冻结；SPA可显式使用有超时的networkidle初始化窗口，仍只允许准确写路径。相关Runner回归23项通过。

官方MCP SDK通过实际HTTP API读取同一FlowTest草稿的动作、章节和5条证据，18工具发现及临时令牌撤销均通过。30个被动导航热点的浏览器网络结果单列在[试点机器记录](validation/results/flowtest-pilot.json)。其他调试失败任务及启动诊断保留为历史，不能计入三条正式路径。

## 复现和后续验收

```bash
PYTHONPATH=services/api:. VKS_FLOWTEST_SOURCE_ROOT=/path/to/FlowTest \
  VKS_FLOWTEST_PILOT_REVISION=COMMIT uv run pytest -q tests/pilot
PYTHONPATH=services/api:. uv run python scripts/build_flowtest_pilot.py --help
PYTHONPATH=services/api:. uv run python scripts/seed_flowtest_pilot.py --help
```

测试会校验选定原文件的Git字节、导入分母、来源/模型边界，以及原仓库status前后不变。此次真实仓库专项1项通过；原有`AGENTS.md`、`README.md`和`.playwright/`变动均未改动。

后续业务验收需要人员确认动作映射、与源码一致的测试构建、独立人工Gold及用户任务；本次三条仅证明隔离实例内的页面/校验/权限状态，不代表创建/执行工作流等全部业务成功路径。临时容器与测试浏览器在验证后清理，VKS保存证据用于被动阅读；重新录制需登记新的有效测试认证引用并审批。
