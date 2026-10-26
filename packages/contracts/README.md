# 契约基线

`docs/design/contracts` 保留用户设计包原件，不随实现修改。本目录以该契约为基线；`scenario.schema.json` 兼容扩展了可选 `role` 和 `expected_results`，分别表示合成测试角色说明和人员核对的预期结果，不赋予权限或执行表达式；只读 `wait_for.state` 支持 `disabled` 断言；`goto.wait_until` 可选择有界页面等待阶段（默认 domcontentloaded）。其他原始契约保持基线格式。

`mcp_tools.resolved.json` 由 `uv run python scripts/build_contracts.py` 构建，展开全部本地引用后用于 SDK 注册。MCP 输入严格校验；输出从 REST 工作台扩展中投影到设计 DTO 后再次校验。错误会明确返回，不能用错误数据伪装成功。

本地首迭代新增的登录、上传传输、预检、工作台聚合、令牌、发布人员确认等接口由运行中 `/openapi.json` 描述。首迭代扩展字段与未支持范围见 `docs/IMPLEMENTATION.md`；设计文件并不表示服务器已实现全部内容。

来源摘要使用设计样例冻结的 `vks-file-list-v1`：按包根相对路径排序，将各项 `path/sha256/size_bytes` 按键排序，以 UTF-8 紧凑 JSON 序列化后 SHA-256。导入时检查实际字节与声明一致，不执行源码或访问清单里的远程地址。

审计修复兼容扩展：场景状态可选`pageKey`、`actionKey`、`pageBindingOrigin`/`actionBindingOrigin`（仅declared）及`snapshotIndex`（0–19）；观察状态的captureRef由服务端验证同项目及知识基线后保存为新修订，不提升因果等级。服务端先执行Schema校验再解析状态引用。

action context新增可选`relation_policy`，默认trusted（confirmed+valid），candidate及review分别读取候选/失效关系；rejected不用于讲解。运行API增加项目分页搜索`q`、准确capture详情与`/scenarios/{sid}/capture-bindings`，以后端`/openapi.json`为准；分页`meta`保留cursor/limit。MCP展开契约随场景Schema重新生成。以上均不修改`docs/design/contracts`原件。
