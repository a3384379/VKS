# VKS 1.0 UI Image Generation Prompts

Method: built-in image_gen. Eight selected UI concept images, not running software screenshots. All data, code snippets, source revisions, route names and counts are illustrative, not audited statements about the user's project. Main specification defines authoritative field names, permissions and behavior. Generated design images may contain small visual/text variations; use them for composition and style rather than copying every pixel as a requirement.

## File mapping

| Page ID | Screen | File |
|---|---|---|
| P01 | Project overview | 01_project_overview.png |
| P02 | Business workbench | 02_business_workbench.png |
| P03 | Import center | 03_import_center.png |
| P04 | Document center | 06_document_center.png |
| P05 | Link review | 04_link_review.png |
| P06 | Knowledge map | 07_knowledge_map.png |
| P07 | Version changes | 08_version_changes.png |
| P08 | MCP center | 05_mcp_center.png |

All selected images are 1586 x 992 PNG. Selected final output directory: output/vks_1_0/ui/.

## Shared style

Premium ice-blue and white, dark navy typography, thin outlines, blue interactions, readable Simplified Chinese, one screen per image, no dark dashboard. Workbench generated first and used as visual reference for subsequent screens.

## 02 Business Workbench original prompt

Use case: ui-mockup.
Create a polished high-fidelity product UI concept of a desktop web application named "知境 / VKS" for a software project knowledge studio. One single complete screen, not a collage. Wide 16:10 landscape, high resolution. Flat front-facing screenshot, edge-to-edge app screen, no device bezel or perspective.

Design system: premium sophisticated light ice-blue and white interface, bright blue primary action #2563EB, very pale blue-gray background #F4F8FF, white cards, dark navy small typography, thin borders, delicate soft shadows, subtle depth, crisp rounded rectangles, clean outlined icons. Refined and information-dense like a mature professional software tool, yet calm and elegant. No dark surfaces, black canvas, oversized gradient hero, neon glow, cartoon graphics or purple colors. All main headings and labels in accurately rendered Simplified Chinese, readable typographic hierarchy; technical identifiers in English monospace. Source data are illustrative.

SCREEN: "业务探索工作台". Persistent narrow far-left vertical nav (roughly 170px) white: blue abstract aperture icon then "知境" and tiny "VKS". Under project selector "云收费知识工程", navigation "项目总览", active "业务工作台", "知识文档", "关联审核", "版本变更", "MCP 中心", "设置". Bottom small avatar "开发者".
Top bar: breadcrumb "云收费 / 开票业务 / 缴费记录开票", global search, version pill "基线 v1.22", user icon.
Below header: title "业务探索工作台", quiet subtitle "从真实业务操作，理解接口、源码与规则"; segmented control with active "解释模式" and inactive "模拟操作"; safety note "解释模式不会提交业务请求". This warning should be visually clear but calm.

Main workspace three panes. Secondary scene tree at left about 190px: heading "业务场景", nested items "收费管理", "缴费记录", active "缴费记录开票", "批量开票", "红冲与下载". Under it scenario states: "列表", "开票弹窗", "提交成功", "校验失败". Current state "开票弹窗".
Large middle canvas about 50% of remainder: framed snapshot of a real-looking invoicing page in subtle white and ice blue, with table behind an invoice form dialog. Dialog title "申请开票". Fields "客户名称" value "示例客户 A"; "票种" options "普通发票" checked and "专用发票"; "开票金额" value "500.00"; "备注" empty. Bottom buttons "取消", bright blue "确认开票". A crisp blue hotspot outline encircles "确认开票", blue marker "A03" and small nonobstructive tooltip "已选操作：确认开票". Keep snapshot distinct from outer app with faint dotted grid outside.
Right knowledge detail pane about 340px: title "确认开票", tiny status "候选关联 · 待复核"; tabs "业务", selected "接口", "源码", "证据"; compact API card with green POST tag and "/pay/invoice/openInvoice"; evidence rows "运行记录 1", "静态路径 1", "文档章节 2"; call path as readable vertical slim list "PayInvoiceController", "PayInvoiceServiceImpl", "InvoiceMapper". A light-blue docs link card "开票技术设计.md / 金额校验"; footer link "查看完整证据". Do not label inferred business rule as proven.
Bottom timeline spanning center+right: heading "场景步骤", four cards "01 查询记录", "02 打开弹窗", active "03 确认开票", "04 查看结果". Bottom status "前端 r18420 · 后端 r18416 · 文档 v6". At far bottom subtle text "概念设计 · 示例数据".
Use ample usable space yet clear screen density. Do not add photographic images. All detailed UI text may be short but should be legible. No fake browser address or URLs.

## 01_project_overview.png original prompt

Use case: ui-mockup. Input image 1 is a STYLE REFERENCE ONLY, not the target content. Generate one NEW high-fidelity complete desktop application screen of "知境 / VKS", same product as reference. Wide 16:10 high-resolution flat screenshot. Keep same ice-blue-white light theme, typography, navy text, white far-left navigation rail, blue abstract aperture logo and "知境 VKS", rounded subtly bordered cards, delicate shadows, small crisp outline icons, calm premium mature software aesthetic. Match reference exact far-left nav ordering: project selector "云收费知识工程", "项目总览", "业务工作台", "知识文档", "关联审核", "版本变更", "MCP 中心", "设置"; bottom avatar "开发者". Change nav highlight to appropriate page. Keep top global search and version baseline pill. Do NOT retain invoice dialog/scene tree unless requested; this is a distinct product page. Accurate legible Simplified Chinese main labels plus English technical tokens. No dark canvas or dark panels, no giant gradients, no photography, no perspective or laptop, no collage, no marketing hero. Main headers large enough; body information compact and structured, with real breathing room. All data synthetic. Bottom right subtle "概念设计 · 示例数据".
SCREEN: 项目总览. Left nav "项目总览" active. Header breadcrumb "工作空间 / 云收费知识工程"; title "云收费知识工程"; subtitle "把业务操作与实现依据连接起来". On upper right buttons "查看业务场景" and primary blue "+ 导入资料". Small project context line "基线 v1.22 · 3 个源码仓库 · Java / Vue".
Below top four elegant compact KPI cards labeled "业务场景" value "12"; "已确认关联" value "86"; "待审核关联" amber value "18"; "待复核文档" amber value "4". These are illustrative counts not chart trend.
Main area two columns: left about 65%, right 35%.
Left primary large "业务场景" panel with 3 rich horizontal scene cards. First "缴费记录开票" with tiny light UI preview thumbnail, tags "4 个步骤" "可探索" "已关联 8 份知识", button "进入工作台". Second "批量开票" with tags "6 个步骤" "待审核 3 项". Third "红冲与下载" with "5 个步骤" "待复核 1 项". Scene card thumbnail simple interface miniature, not infographic.
Below left a wide table "知识来源": columns "来源 / 类型 / 版本 / 状态". 3 rows: "charge-web / Vue / r18420 / 已索引", "charge-manage / Spring Boot / r18416 / 已索引", "开票技术设计.md / Markdown / v6 / 已解析". Source icons small.
Right top "待处理" panel with 3 review tasks each with amber dots: "确认开票 → 金额校验说明", "Mapper 调整影响 2 个章节", "导入任务需要补充版本". Bottom primary link "进入关联审核".
Right below "最近分析" shows vertical understated timeline: "完成源码索引" "发现 18 条关联候选" "发布基线 v1.22". Small "知识健康" box shows three well-labeled thin horizontal progress tracks: "场景覆盖", "证据完整", "版本一致"; don't use pie charts or invented percentage as truth. Header shows "试点项目".
Make screen polished and useful, restrained ice-blue layered panels.

## 03_import_center.png original prompt

Use case: ui-mockup. Input image 1 is a STYLE REFERENCE ONLY, not the target content. Generate one NEW high-fidelity complete desktop application screen of "知境 / VKS", same product as reference. Wide 16:10 high-resolution flat screenshot. Keep same ice-blue-white light theme, typography, navy text, white far-left navigation rail, blue abstract aperture logo and "知境 VKS", rounded subtly bordered cards, delicate shadows, small crisp outline icons, calm premium mature software aesthetic. Match reference exact far-left nav ordering: project selector "云收费知识工程", "项目总览", "业务工作台", "知识文档", "关联审核", "版本变更", "MCP 中心", "设置"; bottom avatar "开发者". Change nav highlight to appropriate page. Keep top global search and version baseline pill. Do NOT retain invoice dialog/scene tree unless requested; this is a distinct product page. Accurate legible Simplified Chinese main labels plus English technical tokens. No dark canvas or dark panels, no giant gradients, no photography, no perspective or laptop, no collage, no marketing hero. Main headers large enough; body information compact and structured, with real breathing room. All data synthetic. Bottom right subtle "概念设计 · 示例数据".
SCREEN: "资料接入与分析". Left nav "项目总览" active, breadcrumb "云收费知识工程 / 资料接入". Title "导入项目资料"; subtitle "保留来源与版本，让每条知识可追溯". Top right button "保存草稿". Four-step progress wizard across content: "1 选择来源", active "2 关联资料", "3 分析预检", "4 审核发布". First marked complete.
Main central panel left about 68% with "源码快照" section: small source tabs "Git", selected "SVN", "本地目录", "ZIP". Three source rows as compact cards with repo name, source type and revision: "charge-web · SVN · r18420" with tags "前端" "Vue"; "charge-manage · SVN · r18416" tags "后端" "Java 8"; "charge-billing · SVN · r18410" tags "服务" "MyBatis". All have little lock icons "只读导入". Link "+ 添加来源".
Next section "知识与原型资料", two side-by-side dropzones with plus icons: "Markdown / OpenAPI" and "Axure HTML / 页面录制". Existing file rows "开票技术设计.md 128 KB" and "invoice-prototype.zip 4.2 MB", both checked, concise.
A light-blue information note "不会修改源码仓库，也不会启动业务项目".
Right side panel "版本基线" contains field "基线名称" value "v1.22", and "来源版本已固定 3/3"; "分析策略" checkboxes "接口与调用关系", "Markdown 章节", "页面操作锚点" checked; "AI 语义补充" switch enabled, label "只生成候选，不直接发布". Bottom "预计分析范围" concise "3 个仓库 / 24 份文档 / 6 个页面".
Bottom full width sticky footer white "预检通过，尚未发布" at left; buttons "上一步", primary "开始分析". Under right status small three job rows "文件校验 已完成", "源码索引 待运行", "关联建议 待运行". Keep literal credentials out; no real repository URLs. No invoice dialog; this is ingest wizard.

## 04_link_review.png original prompt

Use case: ui-mockup. Input image 1 is a STYLE REFERENCE ONLY, not the target content. Generate one NEW high-fidelity complete desktop application screen of "知境 / VKS", same product as reference. Wide 16:10 high-resolution flat screenshot. Keep same ice-blue-white light theme, typography, navy text, white far-left navigation rail, blue abstract aperture logo and "知境 VKS", rounded subtly bordered cards, delicate shadows, small crisp outline icons, calm premium mature software aesthetic. Match reference exact far-left nav ordering: project selector "云收费知识工程", "项目总览", "业务工作台", "知识文档", "关联审核", "版本变更", "MCP 中心", "设置"; bottom avatar "开发者". Change nav highlight to appropriate page. Keep top global search and version baseline pill. Do NOT retain invoice dialog/scene tree unless requested; this is a distinct product page. Accurate legible Simplified Chinese main labels plus English technical tokens. No dark canvas or dark panels, no giant gradients, no photography, no perspective or laptop, no collage, no marketing hero. Main headers large enough; body information compact and structured, with real breathing room. All data synthetic. Bottom right subtle "概念设计 · 示例数据".
SCREEN: "关联审核". Left nav "关联审核" active. Title "关联审核"; subtitle "确认关系之前，先看证据". Breadcrumb "云收费知识工程 / 关联审核". Toolbar filter pills "待审核 18" selected, "已确认 86", "已驳回 3"; a search field. Right top buttons "查看审核记录".
Three-pane review workspace:
left 26% candidate queue as list, title "关联候选", selected first card "确认开票 → 金额校验" with amber tag "AI 推断" and small "2 项证据"; second "查询记录 → 查询接口" with blue "静态解析"; third "提交开票 → openInvoice" with teal "运行观测"; fourth "开票失败 → 异常处理" amber "AI 推断". Avoid score-as-probability; no percentages.
middle 48% evidence comparison panel top heading "确认开票 → 金额校验"; relation tag "业务规则说明"; tabs "对比证据" active and "相关上下文". Source evidence card titled "页面操作" with small white invoice snapshot preview (not huge), action label "确认开票 · A03", route and source baseline. A blue connecting link icon divider. Target evidence card titled "文档章节" has filename "开票技术设计.md", chapter "2.1 金额校验", readable 2-line excerpt "提交前校验可开票金额。" and "退款及已占用金额需要扣除。". Below source code evidence strip: "PayInvoiceServiceImpl.openInvoice" monospace with line numbers and tiny code excerpt only 3 lines. Small source pill "后端 r18416".
right 26% decision panel "审核结论". Clear status "待人工确认". "证据说明" checklist style: "接口路径一致" checked, "代码符号可定位" checked, "业务含义仍需确认" amber. Explanation box "名称及上下文相关，但未直接证明全部金额规则。" heading "风险提示". Textarea "审核备注" placeholder "填写确认或驳回原因…". Buttons blue "确认关联" and neutral "驳回"; below optional "稍后处理". Last tiny note "确认后进入当前草稿基线，不自动发布".
All source data illustrative; no fabricated claims of already verified semantics.

## 05_mcp_center.png original prompt

Use case: ui-mockup. Input image 1 is a STYLE REFERENCE ONLY, not the target content. Generate one NEW high-fidelity complete desktop application screen of "知境 / VKS", same product as reference. Wide 16:10 high-resolution flat screenshot. Keep same ice-blue-white light theme, typography, navy text, white far-left navigation rail, blue abstract aperture logo and "知境 VKS", rounded subtly bordered cards, delicate shadows, small crisp outline icons, calm premium mature software aesthetic. Match reference exact far-left nav ordering: project selector "云收费知识工程", "项目总览", "业务工作台", "知识文档", "关联审核", "版本变更", "MCP 中心", "设置"; bottom avatar "开发者". Change nav highlight to appropriate page. Keep top global search and version baseline pill. Do NOT retain invoice dialog/scene tree unless requested; this is a distinct product page. Accurate legible Simplified Chinese main labels plus English technical tokens. No dark canvas or dark panels, no giant gradients, no photography, no perspective or laptop, no collage, no marketing hero. Main headers large enough; body information compact and structured, with real breathing room. All data synthetic. Bottom right subtle "概念设计 · 示例数据".
SCREEN: "MCP 中心". Left nav "MCP 中心" active. Title "MCP 中心"; subtitle "让 AI 在受控权限内读取和整理项目知识". Breadcrumb "云收费知识工程 / MCP 中心". Top right buttons "接入说明" and primary "+ 创建客户端".
Three summary cards top compact: "已连接客户端 2", "执行中任务 1", "待审批请求 2". No charts required.
Main grid two columns left 60% right 40%. Left top "客户端与权限" table with columns "客户端 / 权限 / 最近活动 / 状态", rows "研发助手 A / 知识读取 / 2 分钟前 / 已连接" and "分析助手 B / 导入与提议 / 5 分钟前 / 已连接". Next "可用工具" table with clear scope tags and statuses: "search_knowledge / 只读 / 已启用"; "explain_ui_action / 只读 / 已启用"; "import_bundle / 导入 / 需授权"; "propose_links / 提议 / 已启用"; "publish_baseline / 人工审批 / 不向自动代理开放". Use English identifiers rendered precisely.
Right top "异步任务" card "导入并分析开票场景" badge "运行中", job id "job_a18f", a progress bar at 3 of 5 stages, stages "资料校验" done, "源码索引" done, "候选生成" current, "人工审核" pending, "发布基线" pending. Small links "查看日志" "取消任务".
Right bottom "待审批" card contains "分析助手 B 请求导入新基线", description "范围：云收费项目 · 3 个资料包", actions "查看请求" and "拒绝". Blue info note "读取与写入分别授权；AI 不可自动发布".
Bottom left "连接配置" card shows protocol "Streamable HTTP", "访问令牌" value "••••••••••••" with tiny copy icon disabled/redacted; "凭据只引用 secret_ref，不返回明文". Bottom footer audits "审计日志 · 所有导入和审批操作均记录操作者与基线". No real endpoints/secrets, no fake external login. Maintain refined light theme.


## 06_document_center.png original prompt

Use case: ui-mockup. Input image 1 is the established STYLE REFERENCE only. Create a NEW high-fidelity full-screen desktop application UI concept for "知境 / VKS". One screen, not a collage. Wide 16:10, high-resolution, flat front-facing screenshot. Match the reference refined ice-blue-white light application, dark navy typography, bright blue primary controls, white left rail, compact line icons, thin borders, soft shadows, rounded cards. No dark/black surfaces, giant hero, saturated gradients, neon glow, photography, 3D, browser bezel or perspective. Chinese main text rendered clearly and accurately, English code tokens in monospace. Far-left rail same as reference, source project selector "云收费知识工程", nav "项目总览", "业务工作台", "知识文档", "关联审核", add "知识图谱", "版本变更", "MCP 中心", "设置". Small bottom avatar "开发者". Top global search and version baseline selector. All numbers/data synthetic. Footer "概念设计 · 示例数据". Never show unexplained knowledge percentage metrics.
SCREEN: 知识文档. Left nav "知识文档" active. Breadcrumb "云收费知识工程 / 知识文档 / 开票技术设计.md". Main header title "开票技术设计"; compact status "草稿 v7" amber; top right secondary "查看历史" and blue "保存草稿", not publish. Page width partition: document tree 210px; editor about 55%; right related-evidence panel about 310px.
Document tree heading "知识目录" with search, nested rows "业务说明", expanded "开票技术设计.md" and chapter items "1 业务概述", active "2.1 金额校验", "2.2 重复开票", "3 接口说明", "4 数据设计"; another "异常处理.md". Footer "24 份文档".
Center editor card top segmented "阅读 / 编辑 / 对比" with "编辑" active; small formatting toolbar H B I link code list. Visible clear Markdown-style document section heading "2.1 金额校验". A blue border selected block with readable paragraph "提交开票前校验可开票金额。退款金额及已占用金额需要扣除。" Below a smaller gray evidence footnote "依据：PayInvoiceServiceImpl.openInvoice · r18416". Next "业务补充说明" paragraph in lightly amber lock framed block, small lock icon and tag "人工段落 · 已锁定"; text "特殊退款场景需由业务负责人确认口径。" A small callout "AI 更新不会覆盖已锁定段落". Then mini table "字段 / 含义 / 来源": "invoiceAmount / 申请开票金额 / 请求参数"; "availableAmount / 可开票金额 / 业务计算". Keep AI-generated prose marked "待复核" not verified. Bottom editor status "已自动保存草稿 · 2分钟前". No fictional code implementation beyond illustration.
Right panel heading "当前章节的关联". Top selected chapter small "2.1 金额校验". Cards: "UI 操作" with tiny UI preview of invoice action and label "确认开票 · A03", link "在工作台定位"; "关联接口" POST "/pay/invoice/openInvoice"; "源码证据" monospace "PayInvoiceServiceImpl.openInvoice", version pill "r18416", relation label "静态解析"; a small "关联状态" card showing "1 条已确认 / 1 条待审核". Bottom subtle "+ 添加关联" and "查看证据". All short text readable; maintain mature editorial layout.

## 07_knowledge_map.png original prompt

Use case: ui-mockup. Input image 1 is the established STYLE REFERENCE only. Create a NEW high-fidelity full-screen desktop application UI concept for "知境 / VKS". One screen, not a collage. Wide 16:10, high-resolution, flat front-facing screenshot. Match the reference refined ice-blue-white light application, dark navy typography, bright blue primary controls, white left rail, compact line icons, thin borders, soft shadows, rounded cards. No dark/black surfaces, giant hero, saturated gradients, neon glow, photography, 3D, browser bezel or perspective. Chinese main text rendered clearly and accurately, English code tokens in monospace. Far-left rail same as reference, source project selector "云收费知识工程", nav "项目总览", "业务工作台", "知识文档", "关联审核", add "知识图谱", "版本变更", "MCP 中心", "设置". Small bottom avatar "开发者". Top global search and version baseline selector. All numbers/data synthetic. Footer "概念设计 · 示例数据". Never show unexplained knowledge percentage metrics.
SCREEN: 知识图谱. Left nav "知识图谱" active. This is a conceptual UI graph workspace, NOT a technical architecture diagram and no claims of actual source audit. Header title "知识图谱"; subtitle "只展开当前业务上下文，追溯每条关系的证据". Breadcrumb "云收费知识工程 / 知识图谱". Top tools search "确认开票", filters "层级 2" "仅当前基线" and "隐藏未确认" unchecked, blue "打开业务工作台".
Canvas large left 73% pale-blue near-white dotgrid with 8-9 well-spaced white card nodes, each with small type tag and small 2-line text. Center node active blue thin outline "UI 操作 / 确认开票". Connect left-above scene node "业务场景 / 缴费记录开票"; direct right API node "POST / openInvoice"; below-right source node "方法 / PayInvoiceServiceImpl"; lower-right SQL node "SQL / invoice_order"; below-center doc node "文档 / 开票技术设计"; upper-right rule node "规则 / 金额校验"; far-bottom optional test evidence node "录制 / 示例场景 01". Do not more than 9 nodes or spiderweb. Different exact edge styling: blue solid for observed, slate solid for static parsed, amber dashed for candidate; small labels "运行观测", "静态解析", "待审核". Use selective simple connectors, not unintelligible grid. One edge selected blue visibly, connecting "确认开票" to API node "openInvoice".
Right narrow details panel: title "关系详情"; "确认开票 → openInvoice"; type badge "运行观测"; evidence cards "录制场景 示例场景 01", "步骤 03 · 确认开票", "接口 POST /pay/invoice/openInvoice", "基线 v1.22", a compact note "仅证明此场景发生该请求，不代表全部分支". Button "查看记录" and link "查看源码上下文". Lower panel "关系图例" with blue line "运行观测", gray line "静态解析", amber dashed line "候选推断". Bottom canvas left 3 controls plus/minus/fit, right minimap tiny and unobtrusive. Bottom canvas status "当前显示 8 个节点 · 7 条关系 · 局部视图". Sample graph is conceptual; no all-repository star cloud, no dark canvas.

## 08_version_changes.png original prompt

Use case: ui-mockup. Input image 1 is the established STYLE REFERENCE only. Create a NEW high-fidelity full-screen desktop application UI concept for "知境 / VKS". One screen, not a collage. Wide 16:10, high-resolution, flat front-facing screenshot. Match the reference refined ice-blue-white light application, dark navy typography, bright blue primary controls, white left rail, compact line icons, thin borders, soft shadows, rounded cards. No dark/black surfaces, giant hero, saturated gradients, neon glow, photography, 3D, browser bezel or perspective. Chinese main text rendered clearly and accurately, English code tokens in monospace. Far-left rail same as reference, source project selector "云收费知识工程", nav "项目总览", "业务工作台", "知识文档", "关联审核", add "知识图谱", "版本变更", "MCP 中心", "设置". Small bottom avatar "开发者". Top global search and version baseline selector. All numbers/data synthetic. Footer "概念设计 · 示例数据". Never show unexplained knowledge percentage metrics.
SCREEN: 版本变更. Left nav "版本变更" active. Header "版本变更与知识复核"; subtitle "比较代码与资料基线，确认哪些业务知识需要更新". Top selectors "已发布 v1.22" arrow "草稿 v1.23"; top right buttons "重新分析" and blue "提交发布审批".
Three source version cards across top labeled "前端" showing "r18420 → r18502", "后端" "r18416 → r18488", "文档" "v6 → v7"; below compact line "版本向量已锁定 · 修改不会覆盖历史基线". No percentages.
Main workspace 3 columns: left 27% "受影响项" tabs "操作 3 / 文档 2 / 接口 1"; selected row "确认开票" badge amber "待复核"; rows "查询记录" badge "无语义变化", "开票技术设计.md" badge "章节需复核", "异常处理.md" badge "待复核". Above list filter "仅需处理".
Center 46% "变更证据" heading "金额校验实现调整"; tabs selected "源码对比", "文档建议", "关联影响". Two-column unified-style compact code diff, labels "后端 r18416" and "后端 r18488"; few simple illustrative lines in monospace with line numbers. Red old line "available = paid - occupied;" green new line "available = paid - refunded - occupied;" old/new should clearly synthetic. At bottom diff footnote "代码片段仅作版本对比示意". Below a document patch preview "建议更新章节 2.1 金额校验" with proposed sentence "新增退款金额扣除说明", tag "AI 建议 · 待审核", locked paragraph note "人工段落保持不变". Link "查看完整变更".
Right 27% "复核与发布" status amber "存在待复核项". Checklist three items: "接口签名未变" checked, "调用关系已重建" checked, "业务金额口径确认" pending. Textarea "复核意见" placeholder "补充业务确认依据…". Buttons "确认此项" blue and "暂不接受". Bottom release readiness panel "发布检查": "来源版本 已固定" green; "待复核关联 2 项" amber; "人工审批 未完成" gray; final disabled button "发布基线" and note "完成复核后才可发布". Maintain careful no-auto-publish semantic.


## Final corrections

### overview_fix

Target: /workspace/scratch/ef5b9a0391a4/output/vks_1_0/ui/01_project_overview.png

Use case: precise-object-edit. Edit this UI screenshot only as follows, preserve the entire layout, typography, colors, all other text and elements unchanged. In the bottom-right card currently titled "知识健康度", replace title with "知识覆盖 · 示例". Keep the three progress bars but add exact numerator/denominator labels so they have defined meaning: row 1 label "场景关联" and number "8 / 12"; row 2 label "证据齐全" and number "64 / 86"; row 3 label "版本一致" and number "21 / 24". Use short legible text. Set each bar approximate proportion to correspond respectively to two-thirds, three-quarters, and seven-eighths. Do not invent percentages. Keep "试点项目" pill. No other modifications. Keep full image aspect ratio and dimensions.

### review_fix

Target: /workspace/scratch/ef5b9a0391a4/output/vks_1_0/ui/04_link_review.png

Use case: precise-object-edit. Edit this high-fidelity UI screenshot with only one localized text correction. In the center column, inside the source evidence "页面操作" card under the small invoice screenshot, there is a label "页面路径" followed by "/pay/invoice/openInvoice". Replace ONLY its value with exactly "/payments/records". Keep the label "页面路径" unchanged. This is the page route, not the API. Preserve every other element, all other API identifiers elsewhere, code text, typography, style, dimensions and all layout. No extra changes.


### mcp_contract_alignment_fix

Target: /workspace/scratch/ef5b9a0391a4/output/vks_1_0/ui/05_mcp_center.png

Use case: precise-object-edit.
Input image is the EDIT TARGET: final MCP Center UI concept for 知境 / VKS. Preserve full screen aspect ratio, layout, left nav, heading, colors, typography, all cards and other content exactly. Only make these localized text changes, with clean legible Chinese and accurate English identifiers:
1. In the "可用工具" table, keep row 1 "search_knowledge", change row 2 tool identifier from "explain_ui_action" to exactly "get_action_context". Its description should read "获取操作上下文与证据"; keep "只读" scope and enabled status.
2. Keep row 3 "import_bundle", row 4 "propose_links" unchanged.
3. Change row 5 identifier from "publish_baseline" to exactly "request_release". Change this row description to exactly "申请审核，不直接发布". Scope badge "申请审核"; status text "需人工审批". The tool only requests review, and cannot publish. Use smaller row text if needed to fit without truncation.
4. In the upper-right "异步任务" card's five step labels replace the stages with, in order: "资料校验", "源码索引", "候选生成", "结果校验", "完成". Keep first two checked, third active, remaining pending, and progress "3 / 5". Human approval and publication are NOT machine task stages.
5. Preserve the separate "待审批" card below and its contents, because human approval remains a separate process. Preserve blue safety note "读取与写入分别授权；AI 不可自动发布".
No other changes, no new diagrams or interactions, no added panels. All data are conceptual examples.

Result: saved selected revised image to the same UI deliverable filename. Built-in image generation used; original generated source images preserved. The tool names and machine-only job stages were visually checked.
