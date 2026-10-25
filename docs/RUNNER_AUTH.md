# 受控测试认证与采集

采集必须使用独立的测试账号、合成数据和实例允许的测试来源。普通讲解模式仅切换状态与知识对象。采集任务需要人员签发、绑定场景修订和环境策略的短期单次授权；机器令牌不能批准。

## 实例登记认证引用

`VKS_RUNNER_SECRET_REGISTRY` 指向实例管理员维护的私有 JSON 文件。建议将主机文件放在 `.vks/runner-secrets/` 或仓库外，目录权限 0700、文件 0600（容器中只读 0400 也可）。注册表及会话文件必须属于实际运行 API/Runner 的用户，不允许符号链接；Docker 镜像运行用户 UID 为 10001。不要把现有用户浏览器的 Cookie 或个人 profile 复制进去。

注册表格式如下。`headers_env` 读取实例环境变量，名称必须以 `VKS_RUNNER_AUTH_` 开头；值不通过项目接口传递。也可由实例管理员在同一私有文件的 `headers` 对象中登记测试授权头。支持的头仅有 Authorization 和 X-API-Key。

```json
{
  "schema_version": "1",
  "entries": {
    "test-reader": {
      "origin": "https://test.example.invalid:443",
      "revision": "test-session-v1",
      "test_only": true,
      "project_keys": ["your-test-project"],
      "headers_env": {"Authorization": "VKS_RUNNER_AUTH_READER_TOKEN"},
      "storage_state_path": "reader-state.json"
    }
  }
}
```

`project_keys` 限定可使用引用的 VKS 项目 key；名单变化也使旧绑定失效。未限定项目的引用只能由实例管理员登记到项目环境，项目管理员不能借用别的项目的凭据。

`storage_state_path` 可省略；需要时，它是管理员登记的私有 Playwright storage-state JSON，支持 `cookies` 和 `origins[].localStorage`。Cookie 的域必须等于登记主机，禁止父域通配 Cookie；HTTPS Cookie 必须 secure。localStorage 只允许登记的同一个 origin。凭据值至少 8 个字符，不接受其他 origin 或 indexedDB。文件路径不会传给浏览器；服务器验证后只把内存中的隔离会话交给新建浏览器上下文。

API 与负责浏览器的 Worker 需要相同的只读注册表/会话配置，以及共享实例数据目录。Docker 使用本地 Compose override 挂载 `.vks/runner-secrets` 到例如 `/run/vks-test-auth:ro`，配置容器内注册表路径和必要的 `VKS_RUNNER_AUTH_*` 环境变量；默认 Compose 不挂载任何用户凭据。启用采集需同时设置 `VKS_RUNNER_ENABLED=1` 并启用 runner profile；实例默认关闭录制。回环合成服务还需要 `VKS_RUNNER_ALLOW_LOOPBACK=1`，不能用该开关访问平台控制端口。

项目环境只保存 `auth_ref`、登记版本和服务器随机私有 key 的 HMAC 绑定，不保存授权头、Cookie、storage-state 路径或秘密的裸 hash。每次审批、请求/步骤调度和结果提交都会重新验证注册表。即使 revision 未改，秘密或会话内容变化也会使绑定失效；请登记新的环境版本、更新场景环境引用并重新审批。旧票据不能继续执行或接受迟到输出。备份不复制实例认证注册表和 HMAC 私钥；恢复实例需要重新登记测试认证。

认证头和 Cookie 只转发给完全相同的 origin；其他已允许的资源来源也不会收到这些认证信息。浏览器使用独立临时 HOME/profile 和环境变量白名单，不获得数据库、消息队列或模型密钥。截图、DOM、请求摘要在保存前按模式及实际解析的秘密原值脱敏，不保存原始网络 body、认证头或 Trace。

## 场景与观察

`goto.wait_until` 可选 domcontentloaded（默认）、load、networkidle。认证 SPA 若在 DOMContentLoaded 后才刷新测试会话，可显式选择有超时限制的 networkidle，并仅在该 goto 标记 test_write、环境仅授权精确的刷新路径；后续断言仍保持 read_only。networkidle 不扩大写路径权限，其他 POST 仍被阻断。

测试环境可设置 `viewport`（width 320–3840、height 240–2160，默认 1440×900），并保存到状态快照。DSL 可选 `role` 是合成测试角色说明，`expected_results` 是人员核对的结果列表；二者不授予权限，也不作为表达式执行。原设计交付文件未修改，运行契约是兼容的可选扩展。

环境的可选 `service_key` 将业务来源限定到相应服务。网络摘要保存 request_id、步骤窗口、实际观察步骤、方法、来源、脱敏路径和时间。只有当前版本中唯一的方法/路径/服务声明匹配，且没有未核验的请求条件时，才生成 observed_during 候选，因果依据为 temporal_association。轮询也可能出现在同一窗口；候选始终需要人工复核，不建立 confirmed_trigger 或生产部署源码对齐。多服务匹配、条件歧义或缺少动作身份时保留 unresolved；重采集不会覆盖人工锁定、拒绝或已审核的关系。

外部 Trace/OTel 导入及强请求关联尚未启用。网络观察只证明登记测试会话中发生了请求，不能代替生产部署对齐或完整调用链证据。
