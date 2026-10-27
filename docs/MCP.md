# MCP 实现与授权

VKS锁定官方Python MCP SDK。stdio和Streamable HTTP通过同一REST服务处理授权、导入、证据、任务、提案和导出。2.0 开发预览保留原18工具，增加 `propose_journey`、`create_knowledge_task`、`get_knowledge_task`、`get_knowledge_coverage`、`get_change_impact_report`，共23个自包含Schema；4个资源模板和4个提示模板不变，不扩大项目权限。新增任务仍固定输入、预算与授权上限，机器没有流程批准或发布权限；普通 `get_job/cancel_job` 也支持知识任务。兼容和未实现范围见[2.0实施记录](upgrades/v2/IMPLEMENTATION.md)。

## 生命周期

- stdio继承独立`VKS_TOKEN`，缺失时业务请求拒绝。`VKS_API_URL`仅指向本平台API，不能把调用者凭据转发给模型/业务系统。
- HTTP逐次验证项目Bearer令牌或登记的外部OAuth JWT。机器不获得审核/发布权限。
- 读取发布版需`knowledge:read`；草稿另需`knowledge:edit`；源码/采集分别需`source:read`/`trace:read`。撤销及授权成员停用立即影响下一次请求。
- 导入只接收本身份已上传的artifact句柄；客户端路径不是服务端路径。预检与导入验证Schema、摘要、大小和同基线引用。独立Markdown/原型、源码快照、Java重建索引、场景采集和可选模型草稿均走同一后台任务体系。
- 相同身份/项目/操作的幂等键返回原任务；同键不同内容返回冲突。任务可断线查询、取消或按策略重试；有副作用采集必须取得新审批。
- `request_release`只产生预检与Web入口，成功任务不代表已发布。工具不接受批准、操作者冒充或已发布状态参数。

## HTTP OAuth

VKS是受保护资源，不是授权服务器。实例管理员登记受信issuer、MCP资源audience和公开JWKS，项目管理员按subject/client/scope建立grant。验证RS256签名及时间、issuer/audience/client后，还要与grant和授权成员当前权限相交；token的jku/x5u不触发外部取钥。

资源元数据位于`/.well-known/oauth-protected-resource/mcp`；401包含`WWW-Authenticate`及登记的元数据地址。部署时把`VKS_MCP_RESOURCE`设成客户端实际访问的HTTPS资源URL。公钥轮换和撤销通过设置页面操作，密钥不进入浏览器或项目资料。

实现依据：[MCP授权规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)。实际测试包含官方SDK的stdio子进程、Streamable HTTP、RSA合成授权服务器签名、存续连接中的撤销，以及Web/MCP固定版本的一致读取。它们不代表已经接通企业真实IdP或外部LLM。

## 资源与提示

资源包括`vks://projects/{id}/releases/{release}/overview`、`vks://actions/{id}/releases/{release}`、`vks://evidence/{id}`、`vks://jobs/{id}`。所有资源重新授权，输出固定相同基线。提示模板明确把源码/文档当作不可信数据，不构成执行其中指令的许可。

2.0 知识任务增加 `knowledge_plan`：固定基线、一次模型预算、最多八步、五种固定应用服务工具；输出仍是候选。`get_knowledge_task` 返回阶段产物的摘要描述与模块采用/拒绝决定，所有正文需当前输入权限，机器仍不能作人员审核。计划 JSON Schema 见 `packages/contracts/knowledge_plan.schema.json`，原 18 工具源契约未改。
