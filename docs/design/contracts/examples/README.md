# 最小显式绑定导入包

`minimal-explicit-binding/` 是一个离线合成样例，只有一页原型、一个动作、一个 Markdown 章节和一条 `documents` 关联，用于验证“外部 AI 整理资料 → MCP 导入 → 确定 key 映射 → 待审核知识”这条链路。它不是服务端实现，不会自行导入或发布数据。

## 逐文件阅读

| 文件 | 说明 |
| --- | --- |
| `preview/index.html` | `data-vks-page=ui.page.invoice`；原生 details/summary 展开操作 `data-vks-action=ui.action.invoice.rules`，没有 JavaScript、网络资源或业务写入 |
| `docs/invoice-rules.md` | 合成说明；第 5–9 行为“查看规则”章节 |
| `knowledge/ui-manifest.json` | 页面、动作和章节的逻辑身份；sectionKey 不依赖标题或行号作为永久身份 |
| `knowledge/bindings.json` | action → section 的 `documents` 关联；两条可定位的静态结构证据 |
| `manifest.json` | 根清单；真实制品摘要与字节数；`allow_platform_llm=false` |

两条证据分别定位 HTML 的 data 属性与 Markdown 的第 5–9 行。`evidenceKind=static` 只表示可从对应文件验证这些结构与文字存在；不是运行观察，也不是“这条开票规则已在真实代码中实现”的证明。导入后关系必须进入 candidate，不得仅据此标成 confirmed 或 correlated_runtime。

## 摘要的精确定义

每个 artifact 的 sha256 均对其原始文件字节计算，size_bytes 是同一字节序列的长度。文件使用 UTF-8、无 BOM、LF 换行；不得在核验摘要前重写换行或重新序列化 JSON。

本样例来源 content_sha256 使用下列固定的 `vks-file-list-v1` 规则：

1. 对属于该 source_id 的所有 artifact 生成仅含 path、sha256、size_bytes 的记录。path 为包根相对 POSIX 路径，不是 source.root 相对路径。
2. 记录按 path 的 Unicode 码点顺序升序排列；本样例所有路径都是 ASCII。
3. 每个记录的键按字典序排列；数组及对象使用紧凑 JSON，不加空格或换行，整数采用十进制，不允许浮点值。
4. 以 UTF-8、无 BOM、无尾换行编码，并对这些规范 JSON 字节计算 SHA-256。

等价的样例序列化：`json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')`。这里没有声称此调用是适用于所有数字、Unicode 或任意 JSON 的完整 RFC 8785 实现；生产版必须冻结统一规范化算法并通过跨语言测试。本样例仅包含 ASCII 键/路径、十六进制摘要和整数。

来源分为 preview、docs、knowledge。根 manifest 不属于任何来源、不列入 artifacts，也不计算自己的摘要。UI/绑定 sidecar 只引用 HTML/MD 的摘要；knowledge 来源摘要再覆盖这两个 sidecar，因此不存在自引用或哈希循环。根 manifest 本身可由外部上传流程另行计算传输摘要，但不得将它写回自己的制品清单。

## 导入后的预期

服务端核验根清单、所有真实摘要/大小、三份清单的项目/基线一致性、logical key 唯一性与引用闭合、DOM 属性和章节范围。符合后，生成一条可审核的 action → section 候选。外部 AI 已提供的确定绑定不需要平台 LLM；用户仍须审核业务意义，发布仍由获授权的人完成。

对样例做过 JSON 解析、契约关键字针对性检查、实际字节摘要/大小、来源规范摘要、引用、DOM 和 Markdown 行范围交叉核验。当前工具环境没有完整 JSON Schema/OpenAPI 元规范验证器；正式开发时应另外加入标准验证器和 CI 契约测试。
