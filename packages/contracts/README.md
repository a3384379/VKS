# 运行契约

2026-10-31 起以[自用知识工作流](../../docs/PERSONAL_WORKFLOW.md)为准。本目录是可更新的运行契约；`docs/design/contracts` 原件保持字节完整。审批、提交、签署、计划确认和采集审批接口已移除，内容不再脱敏。

V2 HTTP 使用 `v2_contracts.py` 模型；通过 `VKS_DATA_DIR=<隔离目录> uv run python scripts/freeze_v2_contracts.py` 冻结到 `openapi_2.0.json`。当前完整 HTTP 定义可从运行实例 `/openapi.json` 获取。

MCP 构建器合并 18 个基础工具、5 个 V2 工具与 2 个 V3 工具。执行 `make contracts` 冻结 HTTP 定义并生成自包含的 `mcp_tools.resolved.json`。项目知识的读取和直接保存沿用现有权限。`get_prototype_action_context` 和 `save_business_point_binding` 使用 V3 HTTP 端点，分别读取精确版本的动作说明与保存带草稿版本的关联。

`scenario.schema.json` 定义有限声明式浏览器步骤和讲解图，保留 role、expected_results、disabled 断言及 wait_until。导入材料仅作为数据；原始字节、逻辑身份、不可变修订、证据和来源范围分别保存，不执行来源脚本。历史不可变定义中的审批注记仅在读取时兼容，不产生新审批状态。

3.0 资料包可通过 `prototypeManifest` 或 `prototypeManifests` 引用 `prototype_manifest.schema.json` 描述的侧车。侧车中的 PNG 必须属于同来源，且原字节、像素尺寸、视口和状态引用均可校验。可回导包可包含 `businessPointsManifest`，以同包的原文、动作和状态重建证据。

V3 HTTP 定义见 `openapi_3.0.json`。读取选择准确 `analysis_set_id` 或 `release_id`。图像接口还需要 `page_revision_id`。导入确认、热区及业务点保存使用 `If-Match` 和 `Idempotency-Key`，以当前草稿的 `selection_version` 作为并发条件。

混合导入预检传 `artifact_ids`，返回 `202` 和任务 ID。预检任务结果包含 `preview_digest`、`selection_version` 及按资料划分的映射、问题和可选范围。确认传 `preview_id`、`preview_digest`、`analysis_set_id`、`selected_artifact_ids`。原单件预检及确认仍接受 `artifact_id`。项目菜单设置可提交 `menu_groups`，菜单分页返回当前可见页面的 `group_name`。

D2 P0 的 Studio HTTP 增量使用 `/api/v3/projects/{pid}/interactive`，请求与响应由 `interactive.py` 的 Pydantic 模型冻结。新入口复用当前业务点及章节保存服务；原始包按准确版本及完整资料权限读取。当前 MCP 工具目录保持既有工具，新 Studio 增量工具随 D2 后续批次交付。

模型连接配置通过 `/api/v1/projects/{pid}/model-connections` 统一保存与切换，`/models` 查询当前输入或已保存连接的模型目录，`/test` 发起固定消息的预算内测试。连接输入由 `ConnectionForm` 校验，模型策略增加所选连接 ID 和隔离凭据引用；旧 `model-default` 继续兼容，密钥不进入连接 DTO、数据库策略或审计响应。
