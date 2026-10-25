# UI 设计图说明

这些图片是 VKS 1.0 的**高保真概念设计**，并非已运行的软件截图。所有页面均为 1586×992 PNG，采用冰蓝＋白色、明亮蓝色交互强调和克制的层次。页面规格、权限、状态和交互以主方案第 5–8 章为准。

| 页面 ID | 页面 | 文件 |
| --- | --- | --- |
| P01 | 项目总览 | [01_project_overview.png](01_project_overview.png) |
| P02 | 业务探索工作台 | [02_business_workbench.png](02_business_workbench.png) |
| P03 | 资料接入与分析 | [03_import_center.png](03_import_center.png) |
| P04 | Markdown 知识文档中心 | [06_document_center.png](06_document_center.png) |
| P05 | 关联审核 | [04_link_review.png](04_link_review.png) |
| P06 | 局部知识图谱 | [07_knowledge_map.png](07_knowledge_map.png) |
| P07 | 版本变更与知识复核 | [08_version_changes.png](08_version_changes.png) |
| P08 | MCP 中心与项目设置 | [05_mcp_center.png](05_mcp_center.png) |

文件编号记录设计产出顺序，不代表功能页面 ID。正式导航顺序和名称以主文档为准，不机械复制图片中的细微导航差异。

## 重点设计

- P02：从业务截图/原型中选中动作，右侧读取关联文档、API、源码和证据，底部切换场景步骤。
- P04：反向从文档章节进入业务场景；人工锁定段落不被 AI 重写。
- P05：候选、静态解析、运行观测分开显示；确认候选不等于发布。
- P07：固定来源版本向量，查看关系和文档的受影响范围。
- P08：读取、导入、提议、审核申请分权；后台机器任务不等待人工审批。

## 图像说明与修正记录

图像使用内置图像生成方式制作，完整提示词见 [prompts.md](prompts.md)。原始提示词保留为设计过程记录，后续修正指令优先；提示词中曾出现的旧字段或旧任务阶段不构成实现契约。

已修正总览覆盖指标的分母、审核页的页面路由、MCP 工具命名，以及异步任务与人工审批的分离。图中的示例代码仅表达布局，不能复制为实际业务实现。小字或代码片段即使存在渲染偏差，也不能替代正式 Schema 或 API 字段。
