# AT-35 合成任务 PoC 剖面与边界

冻结日期：2026-10-25。运行硬件见 [AT34_PROFILE.md](AT34_PROFILE.md)，本机结果不替代指定基准机、PostgreSQL 和实际企业项目的验收。

- 5,000 个合法 Java 类，每类一个方法，确定性十六进制注释填充到恰好 100 MiB。该数据验证文件数、字节、解析调度与持久化负载，不代表 100 MiB 复杂业务语法。调用数量为零，不能据此宣称调用解析准确率。
- 源材料恰为 104,857,600 字节。容器另含一个 1,525,432 字节 manifest，故 ZIP 条目为 5,001，展开合计为 106,383,032 字节。压缩传输为 61,959,867 字节，未通过少算一个文件或扣减材料来满足配额。
- 独立临时 SQLite/对象目录、真实 HTTP 上传和 20 并发 HTTP 任务提交；独立 API 进程禁用自动派发，由真实 durable worker 处理一个样本。其余 19 个仅用于提交延迟采样的任务在派发前取消。
- 记录生成、ZIP 校验、schema/解码/静态解析、完整任务持久化分段耗时。无 Maven、Java、SQL 或外部模型调用。
- 使用生产 0.1 秒节流的取消检查，在第 10 个 Java 文件开始时发出取消信号；报告实际退出延迟及此窗口中已开始的额外文件数。真实 job 的取消/撤权/lease 检查另由 durable dispatcher 回归覆盖，不只依赖最终提交时丢弃结果。

配额分母：5,000 个材料文件 / 100 MiB 材料；manifest 单独限 4 MiB；ZIP 传输限 128 MiB；单材料文件仍限 32 MiB。路径、重复项、链接、嵌套 ZIP、压缩比、密钥及完整性检查保留。该剖面不完成真实外部模型的费用/速率验收；模型调用数明确为零。

复现：

```sh
PYTHONPATH=services/api:. uv run python scripts/benchmark_analysis.py --persist-job --http --output /tmp/vks-at35-final-http-5000-100mib.json
```
