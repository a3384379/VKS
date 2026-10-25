# 部署与恢复

默认Compose包含PostgreSQL17.6、RabbitMQ4.1、API、独立index/model Worker和Nginx工作台；`runner`与`mcp`为可选profile。数据库/队列不暴露主机端口，Web与MCP默认仅绑定本机。

## Compose

```bash
cp deploy/.env.example deploy/.env
# 在私有.env中替换两个独立长密码，建议单引号包围值。
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
curl --fail http://127.0.0.1:8765/api/v1/ready
```

数据库和队列密码通过分离的`VKS_DB_*`/`VKS_BROKER_*`构造连接URL，保留字符被正确编码。RabbitMQ自身的密码须使用足够长的ASCII随机值；`.env`单引号避免Compose展开`$`。不要提交`.env`。

打开[工作台](http://127.0.0.1:8765)，管理员在自己的受控终端读取初始化凭证：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml exec api cat /data/vks/bootstrap-token
```

API启动先执行Alembic，再启动Uvicorn。API和Worker必须共用数据库、对象卷及`owner-id`；不能把同一数据库与不同实例的对象/身份目录混用。开发SQLite可自动新增表，正式升级采用迁移并先备份。

可选服务：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile mcp up -d mcp
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile runner up --build -d runner
```

启用采集前，在私有 `.env` 设置 `VKS_RUNNER_ENABLED=1` 并重新创建API/Runner；关闭时页面如实显示待启用。本机模式也需要该变量及已安装的Playwright Chromium，可用 `VKS_CHROMIUM_EXECUTABLE` 指向独立的Chrome for Testing，不能使用个人Chrome配置。

Runner镜像从同一API镜像构建，安装独立Chromium及系统依赖。`vks-browser`仅由Runner消费；index/model队列不执行浏览器。登记的环境必须能从Runner所在网络访问，私网环境需在该网络内部部署。运行Worker属于受信服务进程，浏览器子进程的网络代理按环境允许清单限制访问。

三种`VKS_TASK_MODE`：`local`为单机持久化兼容模式；`celery`为API outbox投递；`external`只执行指定Worker消息。Celery采用JSON、有限重试、确认投递和迟确认；数据库是任务状态的依据。生产反向代理需单独配置TLS、可信主机、网络范围和实例资源URL。

## 本机 Worker

```bash
export PYTHONPATH="$PWD/services/api:$PWD"
# 连接参数经环境注入；不在命令行中粘贴秘密。
uv run celery -A services.worker.celery_app:celery worker -Q vks-index
uv run celery -A services.worker.celery_app:celery worker -Q vks-model --concurrency 1
uv run celery -A services.worker.celery_app:celery worker -P solo -Q vks-browser --concurrency 1
```

如果单独运行dispatcher：`uv run python -m services.worker.dispatcher`。不要在同一实例同时启动两套不同数据库/对象配置的API和Worker。

## 可选模型

在实例环境设置`VKS_MODEL_ALLOWED_ORIGINS`、必要的`VKS_MODEL_ALLOWED_CIDRS`和专用`VKS_MODEL_API_KEY`；Web只保存`model-default`引用。项目管理员再配置协议、地址、模型ID、资料出口、token/费用限额及计价。支持Responses和Chat Completions结构化JSON。登记远端使用HTTPS；测试回环额外要求`VKS_MODEL_ALLOW_LOOPBACK=1`，不能用于绕过生产网络策略。

费用计价由运营者明确配置，币种/微单位保持一致。默认计价0表示未配置收费费率，token预算仍强制执行；不能把0当作供应商免费承诺。失败/超时且用量不明会保守扣除预留额度，账本保留状态。模型策略默认关闭，`allow_platform_llm=false`的导入资料永不发送。

官方接口结构参考：[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)。本仓库用合成本地HTTP模型验证真实请求、结构化结果、证据拒绝和预算，未使用实际供应商密钥调用。

## 备份与还原

备份包含私有数据库和源码/文档对象，不是公开分享包。目标目录必须受控，并将输出的SHA-256保存在独立受信位置。以下命令既支持SQLite，也支持环境中配置的PostgreSQL：

```bash
uv run python scripts/backup_restore.py backup \
  --data-dir /path/to/instance --destination /private/path/vks-backup.zip
uv run python scripts/backup_restore.py restore \
  --archive /private/path/vks-backup.zip --destination /empty/new-instance \
  --expected-sha256 TRUSTED_ARCHIVE_SHA256
```

PostgreSQL需匹配版本的`pg_dump`/`pg_restore`，或`--pg-tools-container`指向明确的工具容器。还原目标数据库和目录都必须为空；命令不会覆盖在线实例。SQLite采用在线backup，PostgreSQL使用导出事务快照，对象逐个核验。缺对象、摘要不符、路径越界等立即拒绝。

还原保留owner身份但撤销旧会话、机器令牌和OAuth grant，生成新的bootstrap；管理员必须重新登录并授权。数据库与对象两个卷都属于备份范围。`docker compose down`保留卷，不能对有用数据运行`down -v`。

## 验证边界

已实际执行完整隔离Compose启动、worker/API重启恢复、发布、PDF与回导包，以及真实PG备份恢复。用例用随机测试stack/端口/卷，结束清理，不复用日常服务。Docker Desktop若因中文路径的BuildKit session header失败，验收脚本会复制白名单构建输入到私有ASCII目录；不会把`.vks`或环境文件带入镜像。

容器烟测不等于企业网络、真实IdP/模型或指定容量门禁。生产发行还需环境配置、外部验收、镜像漏洞/SBOM策略与监控。执行结果见[验证记录](../docs/VERIFICATION.md)。
