# VKS 3.0 安装、升级与回退

可先用[合成资料包](IMPLEMENTATION.md#试用)验证流程，再升级业务实例。Studio 和 Runtime 的本机启动配置见[工作台说明](STUDIO.md)。

1. 将服务和工作目录指向明确的实例。执行 `make install` 安装锁定依赖。`make dev` 启动经典 Web，`npm run dev:studio` 启动本机 Studio、API 和 Runtime。现有 Compose 交付经典 Web、API 和 Worker。数据库与对象卷必须来自同一实例。
2. 升级已有实例前，使用 `scripts/backup_restore.py backup` 保存数据库、对象、RAG 配置和用量账本，独立记录归档 SHA-256。在空目录演练恢复，核对旧快照摘要、文档修订、原有链接和 RAG 选择，按需重建向量缓存。
3. PostgreSQL 使用本实例的 `VKS_DATABASE_URL` 执行 `uv run alembic upgrade head`。`k04dba683021` 保存导览入口和菜单顺序，`l15ecb794132` 增加菜单分组。SQLite 启动时创建或补齐设置表。页面、动作、业务点、关系和制品仍使用既有存储结构。
4. 启动 3.0 API、前端和同实例 Worker。使用草稿预检原型包，核对画面、业务点、文档反查、快照和 MCP 权限。希望默认进入原型时，在项目设置选择一个已有画面的入口页。
5. 回退使用升级前的应用版本与同一份备份。通过 `scripts/backup_restore.py restore` 校验归档 SHA-256 后恢复到空目录。恢复会撤销原会话和机器凭据。回退前可另存有权限的 3.0 可回导资料包。

含 Studio 侧车的 VKSBundle 应由支持该侧车契约的 VKS 版本回导。含 RAG 状态的新备份使用当前版本的恢复脚本。

`make check` 运行工程门禁，`make contracts` 刷新运行契约。PostgreSQL 专项需要独立的 `VKS_TEST_POSTGRES_URL`。真实业务验收和发行以[验证记录](VERIFICATION.md)的实际范围为准。
