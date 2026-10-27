# VKS 2.0 外部验收入口

工程验证与真实业务验收分别记录。自动测试、合成模型、Windows CI 和本机容量数据不能替代下列外部记录。原始材料、账号及人员记录保存在私有 `.vks/` 或企业认可的位置，不进入 Git。

## 业务试点与独立 Gold

由企业授权提供一个云收费业务域的两次固定源码修订、原型、OpenAPI、原始 Markdown 及允许的 Test/Mock 操作范围。登记代码 revision、规范 build id 和测试部署版本；未知或不一致继续显示 unknown/mismatched。

在测量前，由未参与生成候选答案的人员冻结 30 个准确 Action ID、正确关系稳定键和预期影响实体 ID。记录项目、发布版及 manifest SHA-256、冻结时间、独立复核人，并在独立位置保存 Gold 文件 SHA-256。Gold 不能从待评价模型结果反向生成。

按原设计 13.3 剧本执行成功、校验失败、无权限三条有限轨迹，用合成账务数据和有效人员审批。保留动作到 API/源码/章节的准确定位、未知项、第二修订的影响报告、审核签署及新旧发布版一致性。普通讲解仍不得触发业务请求。

## 五人任务测量

至少五名未参与 VKS 实现的人员，各独立完成至少五次定位任务。每个 participant/task 对记录原方式和 VKS 的用时、显式成功与否、是否独立/无辅助、困惑点及评价。人员身份对应表保存在私有目录；不能由开发者或自动浏览器代填独立验收记录。

`scripts/evaluate_v2_pilot.py` 的 Gold 输入字段：`project_id`、`release_id`、`manifest_sha256`、`frozen_at`（含时区）、`action_ids`（准确 30 项）、`relation_keys`、`impact_entity_ids`、`independently_reviewed`、`material_kind`。

测量输入字段：相同三个版本身份、`started_at`、`actions`（action_id/has_page_or_step/relation_keys）、`relations`（key/review_status/validity_status/evidence）、`impact_entity_ids`、`user_tasks`（participant_id/task_id/independent/unassisted/baseline_seconds/vks_seconds/success）、`recorded_branch_kinds`、`forbidden_side_effects`、`history_consistent`、`unresolved`。每项 evidence 包含准确 project/release、artifact_id、artifact_sha256、locator 与 kind。字段约束以脚本及测试为准。

```sh
uv run python scripts/evaluate_v2_pilot.py \
  --gold .vks/acceptance/gold.json \
  --expected-gold-sha256 <独立保存的准确摘要> \
  --run .vks/acceptance/measurements.json \
  --output .vks/acceptance/result.json
```

脚本输出 `reported_pass` 只说明所提交记录的计算结果。零分母为未评价；业务授权、独立性和发行签署仍需人员核对。

## 模型与容量环境

真实提供方验收需在实例内配置获准的模型端点、密钥引用、资料出口策略和费用上限。使用固定输入分别记录成功、拒绝、超时/取消、策略/权限变化、用量和成本。不得把本机合成 HTTP 模型或零模型调用记成真实效果验收。

指定基准机需记录 CPU/内存、PostgreSQL 参数、镜像、网络、源代码摘要、数据分母和后台负载。按原设计执行 20 并发/600 秒真实 HTTP，分别报告 2.0 explanation、局部图和搜索的冷启动/热读取 p95、错误与资源。`benchmark_reads.py --http --v2` 才测量 2.0 动作解释；未加 `--v2` 的历史结果属于原 v1 context。

5,000 文件/100MiB 与 1% 增量分别报告采集、上传、提交、异步任务、语法复用和依赖重算数量。当前实现复用未变语法单元，跨文件依赖仍按完整当前来源重算；这是已知工程边界。

## Windows 与发行

Windows 分发包已设原生 CI、32 项清单/摘要核对、独立解压、干净 venv 哈希锁依赖安装及 Tk 启动门禁。企业目标机仍需检查允许目录、Git/SVN 客户端、网络/证书策略、预览/授权/取消、断网恢复和退出后停止调度。下载后核对随包 SHA-256，解压到个人安装目录并运行 `start-windows.cmd`。

发布前由人员核对上述记录、备份恢复、默认版回退和升级签署。旧 Worker 回滚使用升级前数据库/对象一致备份；不能让旧 Worker 直接消费新任务种类。当前用户实例的切换、生产部署和 2.0 GA 不包含在已有自动验证结果中。
