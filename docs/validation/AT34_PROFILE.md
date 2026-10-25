# AT-34 本机 PoC 冻结剖面

冻结日期：2026-10-25。此处是本机 PoC，不替代设计指定的 8 vCPU、16 GiB、SSD、固定网络基准机验收。当前机器实际为 macOS 26.6.2 arm64，10 CPU，16 GiB。Docker 仅约 7.75 GiB，不符合指定容器资源门禁。

- 完全独立的合成 SQLite 数据库，目录 `/tmp/vks-at34-poc-20261025`，不读取用户 `.vks`、凭据或业务资料。
- 固定已发布 manifest：50,000 关系、1,000 动作、200 Markdown 文档；所有资料均为 `Synthetic` 短文档与短证据，具体长度见生成脚本；不表示真实业务已验证，也不覆盖大型正文或企业图谱复杂度。
- 20 并发，持续发起动作知识、检索、局部图谱各约三分之一的读取，10 分钟。动作/图谱 depth=2，max_nodes=50；检索词 `action-1`，limit=10。
- 独立 Uvicorn 进程、真实 HTTP loopback、机器令牌鉴权及响应序列化；没有外部模型。正式基准机的网络与 PostgreSQL 门禁仍待执行。
- 冷启动发布清单缓存，单列首波 20 请求和随后热读取分布。缓存仅保存不可变发布事实；每次事务重新验证当前身份、授权及制品权限。没有跨事务权限缓存。
- 每 10 秒采样被测 API 的 RSS 与 CPU。报告 p50/p95/p99、所有错误分类、实际持续时间、数据量及源码 SHA-256；如测量期间代码变更，显式标记。

复现：

```sh
PYTHONPATH=services/api:. uv run python scripts/benchmark_reads.py --store /tmp/vks-at34-poc-20261025 --seconds 600 --concurrency 20 --operation mixed --http --output /tmp/vks-at34-http-600s.json
```

优化前同数据的应用服务方法测试：20 并发各一次动作读取，p50 26.780s、p95/p99 34.234s，215,240 SQL，20 请求无错误。此结果不含 HTTP，不能直接当成网络端到端对比。已定位整工作区重复加载、授权 N+1、读取全局锁及连接池容量不足。
