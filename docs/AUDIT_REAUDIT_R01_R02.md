# R01 / R02 复审修复记录

基线：`b8926b0d2665034f24d85a534ec258d544398990`。开始时HEAD一致、工作区干净。用户提供的`c5c93ee`对照是复审证据，本轮没有重新运行旧版本；本轮已在`b8926b0`独立复现两项问题，首轮新增测试为2失败、1通过（跨文档冒用key仍被拒绝）。

本轮仅本地修改，不提交、推送、创建PR、发布或操作现有实例数据库。不执行Maven，不访问真实业务、IdP、模型或客户材料。所有测试使用临时SQLite、合成包/模型返回、独立浏览器及实际官方MCP SDK的本地传输。原设计包保持不变。

## R01：固定文档修订混入后续章节修订

- 原因：`DocumentService._sections`无基线时按artifact查询全部章节；同一原始字节可以产生不同元数据的章节修订，旧详情将它们合并返回。冻结发布版的workspace仍按原manifest选择，不能将该结果当成固定修订详情的通过证据。
- 文件/函数：`services/api/vks_api/documents.py::_sections/_mapped_sections/with_sections/save_draft/assert_publishable`；`knowledge.py::persist_bundle`；`ai_gateway.py::finalize_document`。
- 修复：文档修订的服务器metadata保存有序`section_revision_ids`，与正文、定位等一起参与完整修订摘要。先生成准确章节修订，再生成文档修订，避免循环摘要；章节映射改变时不能复用同一个文档修订。导入、编辑、生成都写入此映射，读取严格按ID列表，不按最新修订或DISTINCT去重。校验章节身份唯一、类型、项目、制品和所属文档。发布预检额外核对当前选择与固定映射，防止新发布版冻结不一致数据。
- 回归：发布→克隆→相同字节重导→修改新草稿，旧详情的章节数量、修订ID、正文、metadata、定位保持完全一致，旧发布manifest不变；生成/编辑均记录准确映射；损坏、缺失、重复或跨项目引用明确失败；旧数据恢复及冲突缺口另测。

## R02：合法显式章节key被随机替换

- 原因：重导将显式章节和普通Markdown章节一起做标题/正文匹配；重复内容产生歧义后，冲突保护又分配随机key。该保护避免了另一种身份碰撞，却破坏了已经声明的稳定身份。
- 文件/函数：`knowledge.py::persist_bundle`的显式匹配、实体别名、证据复用及现有关联更新分支；复用`documents.py::match_sections/assert_section_locks`，保留原有锁和治理边界。
- 修复：项目范围内校验逻辑来源及所属文档后，`identity_origin=explicit`直接按声明key复用；仅未声明身份的章节使用标题/正文启发式。跨文档、跨逻辑来源冲突继续409，不能通过随机key绕开。普通章节改名后复用旧标题的前轮保护仍保留。
- 证据与历史：证据复用比较完整定位、类别、摘录、声明和版本信息，不再只按制品及字节hash复用。章节交换位置时生成新的准确证据，当前关联保留身份/审核结论但标为stale并使用新证据；旧证据与review记录不改写，不把旧行号当作新章节的证据，也不自动恢复有效性/运行因果。
- 回归：合法包中`doc.repeat.first`、`doc.repeat.second`同名同正文，重复导入和交换位置后key、实体ID不变；既有关联继续指向正确身份，新证据行号匹配目标，原review及旧证据定位不变。跨文档冒用、跨来源换名仍被拒绝。实际MCP SDK另覆盖三个合成基线导入和动作→章节读取，HTTP持续读取最初文档修订保持一致。

## 旧数据恢复与兼容

1. 新文档修订已有准确映射时只读该映射；缺失目标、重复身份或跨项目/文档/制品引用不会降级为按artifact扫描。
2. 旧文档修订没有该字段时，优先读取所有选择过它的冻结发布manifest，并验证摘要、章节完整性、正文及定位。多个发布清单必须给出同一组准确章节修订，否则报告冲突，不能挑最早或最新的一组。
3. 没有发布清单时，可使用旧的准确`document_revision_id`关联，或在章节数量、顺序、身份、正文和定位全部唯一完整时恢复。不能唯一恢复时返回HTTP409、`VKS_DOCUMENT_SECTIONS_UNRESOLVED`及安全原因码；不把当前草稿或最新章节冒充旧历史。
4. 明确编辑/导入某个当前草稿时，可以利用该草稿的准确选择构建**新的**有映射文档修订；这不会反向宣称修复了无法证明的旧历史。已经随机化的旧章节身份不批量改写，在来源、当前选择及锁检查通过后，重新导入原合法显式包可复用原显式实体；存在历史冲突、缺口或人工锁时先报告，由人员核对。

无新增数据库结构或Alembic迁移：映射使用现有JSON字段，旧revision、原始字节及冻结manifest均不回写；本轮没有执行现有数据库的数据回填。不需要DDL回滚脚本。后续部署应协调API/Worker版本；回退到旧读取/导入逻辑会重新引入这两项缺陷，删除映射字段也不能作为安全回滚。

无法证明的旧映射仍需人工核对冻结manifest、原始材料和sidecar；没有足够证据时保持缺口，不承诺自动恢复。读取时恢复是只读推导，不是偷偷修改历史记录。

## 实际验证

所有Python命令均先移除继承的`VKS_*`配置，仅设置独立的`.vks/reaudit/runtime`及`VKS_TASK_MODE=local`。

| 命令 | 实际结果 |
| --- | --- |
| 首轮`uv run pytest -q tests/audit/test_document_reaudit.py` | 修复前2 failed / 1 passed，独立复现R01/R02 |
| `uv run pytest -q tests/audit/test_document_reaudit.py tests/documents tests/ingest tests/models tests/audit/test_batch_one.py tests/exports tests/mcp tests/knowledge tests/versions` | 阶段相关回归100 passed，15.81s |
| `uv run pytest -q tests/audit/test_document_reaudit.py`（补齐旧数据/SDK/HTTP后） | 13 passed，1.47s |
| `make check` | Python **314 passed / 11 skipped**，113.21s；前端**34 passed**；TypeScript/Vite构建成功，16.26s |
| `npm run format:check` | 通过，没有无关格式化 |
| `uv run ruff check --select E9,F63,F7,F82 services packages tests` | 通过 |
| 原设计及编码/差异检查 | 44个原设计文件逐项字节/大小/SHA-256核对；保留原UTF-8/BOM/换行；`git diff --check`通过 |

本轮新增13项回归，包含实际MCP SDK与HTTP连续读取；全量仍包含此前13项Web UI浏览器回归。未改Schema/MCP工具定义，原契约及SDK检查纳入全量执行。

11个跳过项为PG6、RabbitMQ1、SVN容器1、Compose1、PG恢复1、真实FlowTest1；当前没有启用对应隔离资源或真实项目读取，不计作通过。未执行远端CI、容量门禁、真实模型/IdP/业务验收和部署；没有新增依赖，也未以历史依赖审计代替本次测试结果。保留上游Starlette弃用及既有图表分块告警。

机器记录与源码摘要在`validation/results/document-reaudit-20261026.json`和`document-reaudit-source-manifest-20261026.json`；红/绿日志在忽略目录`.vks/reaudit/`。上述结果属于**本地代码与合成验证**，不代表真实业务验收。
