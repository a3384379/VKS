# VKS 插件化开发交接包

安装和使用候选版请见 [Windows Codex 安装与试用](windows-codex.md)或[连接器安装与诊断](连接器安装与诊断.md)。下文为项目启动时的设计交接材料。

日期：2026-11-21。

本包是开发方案与接口/配置草案，不是已编译插件，不包含可安装的个人Skill，不会修改你的Codex配置或VKS数据。请交给Codex在VKS仓库中实施。

## 已确定的方向

Codex负责需求分析和设计，VKS提供业务知识。Codex显示摘要、准确引用、可选真实原型缩略图；点击进入VKS Studio操作。公司源码不上传VKS，不新增覆盖率评分、补全任务平台或审批模块。

首发以用户已有本机VKS为主，完整验证macOS arm64/x64、Windows10/11 x64。同一套业务逻辑分别打包；HTTP MCP可作为另一接入方式，但不能因此宣称现有原型runtime已经支持全远程部署。

## 文件阅读顺序

| 文件 | 用途 |
| --- | --- |
| `VKS_3.1_Codex插件与跨平台优化完整方案.md` | 产品边界、现状缺口、架构、检索、预览、跳转、跨平台、发行与阶段安排 |
| `MCP与Studio引用契约草案.md` | 只读profile、版本解析、locator、搜索/原文/预览、链接和错误协议 |
| `跨平台与真实使用验收清单.md` | 真实SDK、浏览器、各OS、安装升级、权限和业务试点的验收标准 |
| `Codex分阶段开发指令.md` | 可复制的总约束、M0–M5任务和阶段回报格式 |
| `examples/` | 仅用于设计/生成测试的JSON与TOML模板；不是当前可执行配置 |

## 怎么交给 Codex

1. 将本包的文档放到VKS仓库新建的方案目录，例如`docs/upgrades/v3.1-plugin/`，不要覆盖原始`docs/design/`。
2. 在Codex打开VKS仓库，先粘贴下面的启动指令。
3. M0输出差异和环境记录后，按分阶段指令继续。若仓库已有新修改，以当前代码为准复核，而不是回退到本文基线。

```text
请阅读仓库AGENTS.md，以及docs/upgrades/v3.1-plugin/中的完整方案、契约草案、
验收清单和Codex分阶段开发指令。
本次先执行“总约束 + M0”，不要一次实现全部阶段。
目标是VKS只读MCP业务知识库接入、Codex轻量预览与Studio准确跳转，
兼容Mac arm64/x64及Windows10/11 x64。
不要开发知识覆盖率、自动补全平台、源码上传或内嵌Studio。
不要改生产数据、安装到我的全局Codex或自动发布/推送。
输出当前基线差异、最小改动落点、实际SDK验证结果、平台风险和后续阶段安排。
```

## 模板注意事项

- 所有域名、UUID、文件路径、业务文字都是占位符。
- `vks-connect`及其命令、`requirements-readonly`、`get_prototype_preview`均为拟开发内容。
- `examples/search-enhancements.example.json`是增强投影示例，不是现有SearchResponse完整Schema；`example_only/contract_status`仅为示例标识，不进入正式返回。
- `plugin-manifest.template.json`为简化portable元数据示例；正式发行需按实际客户端/schema验证和补齐项目元数据。
- 不能直接重命名模板后宣称插件已完成：还缺实际连接器、Skill、契约实现、凭据配置与全部验收。
- Mac与Windows模板的`${PLUGIN_ROOT}`来自当前官方本地插件规范，仍需在目标客户端实测。
- 纯MCP TOML与插件内MCP配置二选一，避免重复注册和重复调用。
- 禁止将真实token放进模板、Git或ZIP；示例不包含任何凭据。

## 审阅边界

源码核对基线：`e57d02508c5b8f61be9aa6e8277349784bc18652`。本次完成方案与静态代码核对，没有实施业务代码或宣称已通过macOS/Windows验收。官方宿主能力按2026-11-21文档核对，发行时需按选定客户端重新验证。
