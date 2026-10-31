# 运行契约

2026-10-31 起以[自用知识工作流](../../docs/PERSONAL_WORKFLOW.md)为准。本目录是可更新的运行契约；`docs/design/contracts` 原件保持字节完整。审批、提交、签署、计划确认和采集审批接口已移除，内容不再脱敏。

V2 HTTP 使用 `v2_contracts.py` 模型；通过 `VKS_DATA_DIR=<隔离目录> uv run python scripts/freeze_v2_contracts.py` 冻结到 `openapi_2.0.json`。当前完整 HTTP 定义可从运行实例 `/openapi.json` 获取。

MCP 构建器合并 18 个基础工具与 5 个 V2 工具，执行 `uv run python scripts/build_contracts.py` 生成自包含 `mcp_tools.resolved.json`。`save_knowledge_snapshot` 直接保存固定版本；`request_capture` 直接使用分析版本、场景修订和环境引用；关系策略为 `valid` / `outdated` / `all`。Web/MCP 共用服务与权限，输入、输出和引用继续严格校验。

`scenario.schema.json` 定义有限声明式浏览器步骤和讲解图，保留 role、expected_results、disabled 断言及 wait_until。导入材料仅作为数据；原始字节、逻辑身份、不可变修订、证据和来源范围分别保存，不执行来源脚本。历史不可变定义中的审批注记仅在读取时兼容，不产生新审批状态。
