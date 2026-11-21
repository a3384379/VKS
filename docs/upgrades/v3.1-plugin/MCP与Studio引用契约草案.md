# MCP 与 Studio 引用契约草案

对应《VKS 3.1 Codex 插件与跨平台优化完整方案》。以下新增字段、错误码、工具与命令为开发规格，不表示当前 VKS 已实现。示例 UUID、域名、业务文字均为合成占位内容，不可当作真实项目事实。

## 1 契约设计原则

1. 原有工具名优先复用；增加可选参数和字段，不用同名工具重复注册模拟版本。
2. 旧全功能 MCP 保持旧调用兼容。新 `requirements-readonly` profile 使用更严格的参数策略和工具白名单。
3. 业务服务统一校验项目、当前授权、冻结基线、资料来源；薄连接器不直读数据库，也不复制版本选择和检索逻辑。
4. 不接受客户端声明“已授权”或“已核验”的字段作为事实；所有链接和原文摘要由服务端生成。
5. 原文字段、生成说明、相似候选分开；相关度不是事实正确率。

## 2 源契约位置

| 工具 | 现有源契约 | 开发要求 |
| --- | --- | --- |
| get_project_context、search_knowledge、get_evidence | `packages/contracts/mcp_tools.json`，相关输出引用 `openapi_1.0.yaml` | 在源定义扩展，再通过build生成；不能只改V3生成器 |
| V3交互/工作区工具 | V3契约与 `scripts/freeze_v3_contracts.py` 等生成源 | 在对应源头修改；把这次新增输出具象化 |
| 新预览工具 | 拟新增版本化契约条目 | 加入构建链，断言全局工具名称唯一 |

`packages/contracts/mcp_tools.resolved.json` 是生成结果。`mcp_gateway.py::public_projection` 与 outputSchema 必须一起更新。以真实 SDK 拿到新字段作为验收，而不是只看 API JSON。

## 3 首次版本解析

拟扩展 `get_project_context`：

```json
{
  "project_id": "10000000-0000-4000-8000-000000000001",
  "release_selector": "latest_saved"
}
```

兼容旧显式调用：

```json
{
  "project_id": "10000000-0000-4000-8000-000000000001",
  "release_id": "20000000-0000-4000-8000-000000000002"
}
```

规则：

- `project_id` 必填；`release_id` 和 `release_selector` 恰好一个。
- 首版 selector 只接受 `latest_saved`，不接受含糊的 `latest` 或隐式草稿。
- 复用 `Service.releases` 与现有只读版本目录，在 `Service.project_context` 的共享服务中解析；HTTP context endpoint同步处理。
- 候选先遵守权限，再按保存时间降序并加确定性次序。不可见版本不泄露名称、时间或数量。
- latest_saved 包含系统生成的保存快照。它不是工作区当前状态，也不一定是人工认定的业务发布版；输出保存时间与说明。
- 无保存版本时返回 `VKS_NO_SAVED_VERSION`，不创建快照、不退到草稿、不推进 current_release_id。
- 返回实际 `scope.release_id`、`release_no`（若可读）、保存时间、manifest摘要，后续调用固定该 ID。
- 显式指定旧 release 时不改为最新；只读解析也不改项目默认指针。

建议公共 scope 投影：

```typescript
type SavedScope = {
  project_id: string;
  release_id: string;
  manifest_sha256: string;
  saved_at: string; // 从既有 published_at 等字段明确映射，ISO 8601
  release_label?: string;
  change_note?: string;
};
```

如现有 metadata 已有同义字段，可沿用并明确映射，不为改名强制所有旧客户端迁移。

## 4 只读 profile 入参约束

白名单：`list_projects`、`get_project_context`、`search_knowledge`、`get_workspace_document`、`get_interactive_context`、`get_prototype_action_context`、`get_evidence`、实现后的 `get_prototype_preview`。

`list_projects` 不要求 release。`get_project_context` 允许首次 selector。其余工具必须传 `project_id + release_id`；不接受 `analysis_set_id`，不接受省略范围后由服务自动选草稿。

不能仅以 HTTP GET/POST 判定读写：搜索可以是 POST，而导出可能启动任务。应按操作语义建立白名单。`readOnlyHint`、客户端 enabled_tools 和提示词不能代替服务端限制。

新 profile 的工具 inputSchema 可以投影成更严格子集，实际 dispatch 再做相同检查；旧全功能 profile 沿用旧schema。确保两层不会出现“列表说可用，调用总失败”的差异。

资源与 prompt 同样按权限和只读范围处理。MCP HTTP 每请求鉴权，不用其他人的长连接身份兜底；stdio 从当前配置引用取得本人的专用凭据。

## 5 搜索接口扩展

在现有字段基础上建议新增：

| 字段 | 规则 | 兼容策略 |
| --- | --- | --- |
| retrieval_mode | legacy / keyword / hybrid / auto | 旧调用缺省保持legacy，新插件显式auto |
| max_excerpt_chars | 单命中摘要上限，受服务硬上限限制 | 可选 |
| max_total_chars | 本次摘要总预算，不能突破服务上限 | 可选 |
| cursor | 若引入，绑定查询、范围、排序和契约版本 | 必须鉴权与完整性验证；不能把任意offset当跨scope游标 |

保留已有 `kinds/module_id/include_descendants/service_key/limit` 等字段含义；不在第一轮删除旧筛选能力。初版文档、章节、页面与业务点优先，不为新插件强迫用户上传代码实体。

示例：

```json
{
  "project_id": "10000000-0000-4000-8000-000000000001",
  "release_id": "20000000-0000-4000-8000-000000000002",
  "query": "费用调整的限制和例外",
  "retrieval_mode": "auto",
  "limit": 8,
  "max_excerpt_chars": 1200,
  "max_total_chars": 6000
}
```

### 5.1 返回结构

建议按既有响应 envelope 增量扩展，不要求以下示意类型替换所有现有字段：

```typescript
type SearchEnhancements = {
  scope: SavedScope;
  retrieval: {
    requested_mode: "legacy" | "keyword" | "hybrid" | "auto";
    used_mode: "legacy" | "keyword" | "hybrid";
    degraded_reason?: "embedding_disabled" | "provider_unavailable"
      | "index_unavailable" | "index_partial" | "index_revision_mismatch";
    truncated: boolean;
  };
  hits: Array<{
    entity_id: string;
    entity_revision_id: string;
    title: string;
    excerpt: string;
    source_kind: "original_section" | "authored_knowledge"
      | "generated_binding" | "candidate";
    validity: "valid" | "outdated" | "unknown";
    match_kind: "exact" | "fulltext" | "semantic" | "hybrid";
    locator?: KnowledgeLocatorV1;
    studio_url?: string;
    evidence_ids: string[];
    excerpt_truncated: boolean;
  }>;
};
```

以上是拟议增强投影。若旧 SearchResponse 使用不同容器名，保留旧名并逐项映射，避免让所有旧调用方同时改造。source_kind 必须由已有来源事实推导，不可让模型主观打“可靠”标签。

`validity=outdated`在此是展示投影，不替换底层`valid/stale/broken/obsolete`及独立`excluded`等字段；不得把展示分类写回既有状态枚举。

### 5.2 摘要与正文

摘要从准确原文命中窗口产生；不能继续只取文档开头。引用中的 line_range 等只辅助显示，原文字节/修订与章节 ID 才是定位依据。

如果一个公式或表格无法完整放进摘要，明确标记片段与截断，并让模型按需读完整章节。不得把截掉条件的半句话当完整业务规则。部分可见文档只返回获授权章节，不能因为父文档可见就带出全部正文。

## 6 KnowledgeLocatorV1

建议使用判别联合，避免混淆不同类型的身份。

```typescript
type DocumentSectionLocator = {
  locator_version: 1;
  kind: "document_section";
  project_id: string;
  release_id: string;
  document_id: string;             // 逻辑文档Entity ID
  document_revision_id: string;
  section_revision_id: string;
  content_sha256: string;          // 对应所指原文/修订的摘要
};

type PrototypeBindingLocator = {
  locator_version: 1;
  kind: "prototype_binding";
  project_id: string;
  release_id: string;
  binding_id: string;
  package_artifact_id: string;     // 当前 binding.package_id 实际语义
  package_key?: string;            // 包根逻辑Entity ID；旧包可能没有
  package_revision_id?: string;    // 包根不可变Revision ID；旧包可能没有
  package_sha256: string;
  action_revision_id?: string;
  point_revision_id?: string;
  binding_version?: number;
};

type PrototypePageLocator = {
  locator_version: 1;
  kind: "prototype_page";
  project_id: string;
  release_id: string;
  package_artifact_id: string;
  package_key?: string;
  package_revision_id?: string;
  package_sha256: string;
  page_ref: string;                // 服务端已验证并登记的包内页面标识
  state: "default";               // 首版无binding只支持默认页面状态
};

type KnowledgeLocatorV1 = DocumentSectionLocator | PrototypeBindingLocator | PrototypePageLocator;
```

不新增独立 binding_revision 表。现有 release 的冻结 relations、准确 Action 修订与 interactive_anchor 已能复原目标；resolver 负责验证、映射和生成链接即可。

必须验证：

1. release 属于项目，manifest合法，当前用户可读。
2. 文档修订属于该 release 的选择，章节属于该文档冻结章节映射。
3. binding 来自该 release 的冻结关系且未被排除，所需资料当前可读。
4. binding.package_id 在准确 catalog 中匹配物理 Artifact；由 catalog 得到 package_key/revision/hash。
5. URL `package` 优先用 package_key；没有该字段的旧包用固定Artifact ID。不是随便将一个ID填到所有字段。
6. 不用同名文档、同路径截图、相似控件文本或最新修订补齐无法验证的定位。

旧材料缺字段时保留真实可证身份；locator 标记可用级别，不伪造新版本号。无法准确定位时返回可读的版本目录入口，不能伪装成精确引用。

`page_ref`由准确catalog/包内已验证页面映射产生，并在搜索/项目上下文的页面条目中返回，不由模型填写任意URL或磁盘路径。若现有目录没有该字段，新增最小页面定位投影即可，不要求先建立业务关联或新的控件版本系统。无法验证包内页面成员时不返回精确page locator。

## 7 原文与证据读取

`get_workspace_document` 保留现有 `document_id + revision_id`，新增可选 `section_revision_ids`、`max_chars` 和分页字段。section参数必须验证属于指定文档修订与release。

全功能旧调用不传新参数时可保持原行为。新插件必须传预算，服务端另有全局安全上限。只读profile下超长全文不能无限输出；返回可续读的边界与truncated。

`get_evidence` 新增可选 `release_id`；新profile强制提供。只返回本次保存版本可归属的 evidence_ids，不能拿同项目任意历史证据拼接为当前依据。若既有证据缺乏可验证的基线归属，应标记不可在此范围确认，而不是自动引用最新。

已知目标读取 `get_interactive_context/get_prototype_action_context` 保留准确 package 和基线。原工具的 mode/refresh 名称不要让Skill误以为可以生成或更新关联；纯只读路径不允许调用生成任务。

## 8 预览接口

拟新增工具 `get_prototype_preview` 输入采用已返回的准确 locator，避免模型重新构造页面参数：

工具接受`prototype_binding`或`prototype_page`两种locator；前者支持已有可核验目标状态，后者用于没有业务绑定的默认页面。以下示例展示binding分支。

```json
{
  "project_id": "10000000-0000-4000-8000-000000000001",
  "release_id": "20000000-0000-4000-8000-000000000002",
  "locator": {
    "locator_version": 1,
    "kind": "prototype_binding",
    "project_id": "10000000-0000-4000-8000-000000000001",
    "release_id": "20000000-0000-4000-8000-000000000002",
    "binding_id": "30000000-0000-4000-8000-000000000003",
    "package_artifact_id": "40000000-0000-4000-8000-000000000004",
    "package_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "max_width": 1280
}
```

重复的外层/内层project与release必须一致；不一致直接拒绝。也可在最终schema中避免重复，但不可默默取其中一套。

成功结果包含：

- `structuredContent`：准确scope、locator、page/state说明、图片摘要/MIME/尺寸、来源与生成时间、Studio链接。
- `content`：兼容 JSON 文本块；一张合法的 MCP ImageContent。不要把base64同时放进structuredContent造成重复膨胀。
- 图片经权限、大小、MIME、来源摘要验证，只接受支持的栅格格式；不把SVG/HTML交给任意渲染器。

无图是正常结果，不是业务异常：

```json
{
  "status": "preview_unavailable",
  "reason": "no_exact_preview",
  "message": "当前原型版本没有可核验的页面截图，可在 Studio 打开原型。",
  "studio_url": "http://127.0.0.1:5193/#project=EXAMPLE&release=EXAMPLE&view=prototype"
}
```

以上链接仅展示格式，不是可用项目地址。未授权与不存在不得通过reason差异泄露目标；对外用统一安全响应。旧raster图只证明自身page/state关系，不能据此推断它是HTML包截图；没有准确映射就不提供图片。

### 8.1 HTML当前页预览的写入边界

M5 若缺少真实素材，在Studio提供显式更新当前页预览：

- 用户选择准确包/页面/状态，沿用已有采集权限与受控任务。
- 从固定原型字节与可复现打开步骤运行；不访问任意URL，不使用用户日常Cookie。
- 截图登记为衍生资产，包含source tuple、viewport、DPR、截图时间、图像摘要与生成器版本。
- source tuple至少包含project、package artifact/root revision与sha、页面/目标、状态；若状态只能降级到默认页，在metadata和展示文案中明确。
- 执行与提交时复核权限、来源和任务取消；不保存业务关联，不修改历史manifest。
- 缓存复用必须source tuple匹配；来源变化使旧图不再匹配，不能贴到新版本。
- 尽量复用Artifact/采集任务；新增缓存索引应可重建，GC和备份规则明确。
- MCP只读取已存在预览；绝不以“图不存在”为由触发这条写入链。

## 9 链接与浏览器导航规则

现有文档格式：`/#project=...&release=...&view=documents&doc=<document_revision_id>&section=<section_revision_id>&reader=standard`。

现有绑定格式：`/#project=...&release=...&view=prototype&package=<package_key-or-artifact-id>&binding=<binding_id>`。

M5默认页预览增加的拟议格式：`/#project=...&release=...&view=prototype&package=...&page_ref=<registered-page-ref>`。`page_ref`不是当前已支持的路由；实现时增加严格只读解析，只打开已验证包内默认页，不执行外来脚本或复杂状态动作。缺可验证页面时仅给包级入口，不假报精准定位。

采用URL参数编码，禁止手拼未经转义的标题/路径。分享链接不包含token、密码、任意selector、源码路径或open_steps。link builder只接受可信studio_base_url，不能由文档内容覆盖origin。

前端按顺序：

1. 解析参数与长度限制，拒绝互斥字段冲突。
2. 保持目标完成登录。
3. 在当前用户权限下解析项目/release/目标。
4. 文档读取准确修订并滚动；原型先选准确包，等runtime就绪，再发送带request ID的target.reveal。
5. 仅在相应确认到达时展示已定位；新导航取消旧请求。
6. 失败不清空原参数、不自动选首项。用户可主动选择打开版本目录或只打开页面。

原型回执须带实际route/frame/target及`navigation_level=page|control|state`或等价字段，状态分支还需`state_verified`和可核验的状态标识。现有reveal的ok=true只表示找到了可见元素，尚未比较目标状态；必须补可观察状态校验。两状态出现同selector/label时不能跳过必要打开步骤后错误宣称状态一致。无法核验状态只能报告控件可见/页面打开，不能把DOM观察升级成业务运行事实。

首次登录、新Tab、刷新、后退、跨包、慢runtime和快速连点必须走同一套状态机。保存了原型面板标题或hash，不等于控件真实可见。

## 10 错误与降级

以下为拟议稳定错误类别，可与项目已有错误码映射，避免同义多码：

| 类别 | 建议码 | 客户端处理 |
| --- | --- | --- |
| 凭据缺失/过期 | VKS_AUTH_REQUIRED | 提示重新连接，不将其当无知识 |
| 目标不可读或不存在 | VKS_TARGET_UNAVAILABLE | 不泄露对象名称，不转到最新或首项 |
| 无保存版本 | VKS_NO_SAVED_VERSION | 指向Studio现有保存版本操作，不自动写入 |
| 只读profile不允许 | VKS_READONLY_PROFILE | 立即停止，不改走其他写入口 |
| 无基线/草稿参数 | VKS_SAVED_SCOPE_REQUIRED | 使用首次解析的release重试一次 |
| 多包/目标歧义 | VKS_TARGET_AMBIGUOUS | 在可见候选中要求明确目标，不猜第一个 |
| 参数或scope冲突 | VKS_INVALID_SCOPE | 修正请求，不默默取默认值 |
| 来源/修订无法确认 | VKS_REVISION_MISMATCH | 说明无法核验，重新取得locator |
| 预算超限 | VKS_BUDGET_EXCEEDED | 缩小范围/分页，不无限循环 |
| API不可达/超时 | VKS_UNAVAILABLE | 显示连接诊断；不声称检索完成 |
| 无预览 | 正常status preview_unavailable | 文本+链接继续可用 |
| 向量不可用 | 正常retrieval.degraded_reason | 返回关键词结果并标明降级 |

协议层错误与工具执行错误按MCP规范区别。知识工具失败一般以 `isError=true` 和安全错误内容返回，不将错误文本伪装成正常hits。可以按已验证幂等读取做有限重试；认证/权限/非法scope不自动重试。任何重试都不能切版本、改权限、改为写任务。

## 11 输出尺寸与延时

以服务端配置为硬上限，不完全信任客户端传值。文字字符预算和整个协议消息字节预算分别测量；JSON TextContent与structuredContent重复部分、base64膨胀也计入网络/上下文测试。

建议超时层级：内部检索上限小于API/网关上限，网关上限小于客户端tool_timeout；不出现客户端已放弃而服务继续无限工作。具体秒数与取消传播在M0/M2用实测校准。

禁止把 `limit=0`、负值、超大整数、无穷数字等当无限制；Schema与服务共同验证。cursor、preview_ref只携带资源定位/完整性信息，不能变成绕过鉴权的下载口令。

## 12 必须有的契约测试

- 新旧schema快照、工具名唯一、旧调用兼容、profile严格子集。
- latest_saved首次解析、相同时间排序、连续保存、多自动快照、显式旧版、无版本、权限变化。
- SDK initialize/list_tools/call_tool：stdio与Streamable HTTP都拿到完整locator、scope、降级字段。
- 修改REST但漏改schema时测试必失败，避免public_projection静默丢字段。
- 混用项目、文档、章节、release、包artifact/root revision时拒绝。
- 缺少release或传analysis_set的强token调用也被只读profile拒绝。
- 无图正常结果与授权错误不同处理，但不泄露隐藏目标。
- 图片只出现一次有效ImageContent；正文不含token/base64重复副本；超限回退。
- 共享Python/TypeScript链接fixture：按同一locator生成/解析相同语义。
- malformed URL、任意scheme、userinfo、跨origin跳转、超长参数、材料提示注入均不可改变权限或执行意图。
