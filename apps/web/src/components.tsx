import {
  Component,
  Children,
  Fragment,
  Suspense,
  lazy,
  createElement,
  isValidElement,
  useMemo,
  type ReactNode,
} from "react";
import { parseDocument } from "htmlparser2";
import { isTag, type AnyNode } from "domhandler";
import {
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Code2,
  FileText,
  FolderOpen,
  Info,
  Link2,
  LoaderCircle,
  LockKeyhole,
  MousePointer2,
  ShieldCheck,
  X,
} from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { DiagramBlock } from "./DiagramBlock";
import { diagramKind } from "./diagram-policy";
import { remarkMathAliases, rehypeMathQuota } from "./math-plugins";
import type { Entity, Evidence, Relation } from "./types";
import { MarkdownAttachment, attachmentUrl } from "./MarkdownAttachment";
import { matchesPassiveSelector, hiddenPassiveElement } from "./hotspots";
import { RenderBoundary } from "./RenderBoundary";
const CodeHighlight = lazy(() => import("./CodeHighlight"));

export function Button({
  children,
  onClick,
  kind = "",
  disabled = false,
  type = "button",
  title,
  ...attributes
}: {
  children: ReactNode;
  onClick?: () => void;
  kind?: string;
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  "aria-label"?: string;
  "aria-pressed"?: boolean;
}) {
  return (
    <button
      {...attributes}
      type={type}
      className={`button ${kind}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}
export function Panel({
  children,
  title,
  action,
  className = "",
}: {
  children: ReactNode;
  title?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <div className="panel-heading">
          <h2>{title}</h2>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
const names: Record<string, string> = {
  candidate: "待整理",
  confirmed: "已确认",
  rejected: "已拒绝",
  valid: "有效",
  stale: "待复核",
  broken: "断链",
  obsolete: "不再适用",
  static: "静态解析",
  manual: "人工标注",
  semantic: "语义候选",
  observed: "运行观察",
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  cancelling: "取消中",
  retry_wait: "等待重试",
  paused: "已暂停",
  draft: "草稿",
  ready_for_review: "待整理",
  frozen: "已冻结",
  released: "已发布",
  published: "已发布",
  complete: "完整",
  partial: "部分完成",
  none: "暂无结果",
  document: "文档",
  document_section: "文档章节",
  section: "文档章节",
  ui_action: "业务动作",
  action: "业务动作",
  ui_page: "页面",
  page: "页面",
  api: "API 接口",
  api_endpoint: "API 接口",
  code_symbol: "代码符号",
  symbol: "代码符号",
  scenario: "业务场景",
  sql: "SQL",
  documents: "文档说明",
  may_call: "可能调用",
  handler_of: "接口实现",
  contains: "包含",
  implements: "实现",
  maps_to_sql: "SQL 映射",
  observed_during: "运行邻接",
  not_established: "未建立因果",
  static_potential: "静态潜在路径",
  temporal_association: "时间邻接",
  correlated_runtime: "运行关联",
  manual_lock: "人工锁定",
};
export function label(value?: string) {
  return names[value || ""] || value || "—";
}
export function Tag({
  value,
  children,
}: {
  value?: string;
  children?: ReactNode;
}) {
  return (
    <span className={`tag ${value || ""}`}>
      {["confirmed", "valid", "succeeded", "published", "released"].includes(
        value || "",
      ) ? (
        <Check size={12} />
      ) : [
          "candidate",
          "stale",
          "queued",
          "running",
          "broken",
          "failed",
        ].includes(value || "") ? (
        <i />
      ) : null}
      {children || label(value)}
    </span>
  );
}
export function Notice({
  children,
  tone = "blue",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return (
    <div className={`notice ${tone}`}>
      <Info size={17} />
      <div>{children}</div>
    </div>
  );
}
export function Empty({
  title,
  description,
  action,
  icon = "folder",
}: {
  title: string;
  description: string;
  action?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        {icon === "link" ? (
          <Link2 size={27} />
        ) : icon === "document" ? (
          <FileText size={27} />
        ) : (
          <FolderOpen size={27} />
        )}
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Spinner() {
  return (
    <div className="loading">
      <LoaderCircle className="spin" size={23} />
      <span>正在读取项目知识…</span>
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
}) {
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`modal ${className}`}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="关闭对话框"
          >
            <X size={20} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
function sourceHeading(
  tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6",
): NonNullable<Components["h1"]> {
  return ({ node, children, ...props }) =>
    createElement(
      tag,
      {
        ...props,
        "data-source-line": node?.position?.start.line,
        tabIndex: -1,
      },
      children,
    );
}

export function Markdown({
  content,
  documentId,
  revisionId,
}: {
  content?: string;
  documentId?: string;
  revisionId?: string;
}) {
  const renderers = useMemo<Components>(
    () => ({
      h1: sourceHeading("h1"),
      h2: sourceHeading("h2"),
      h3: sourceHeading("h3"),
      h4: sourceHeading("h4"),
      h5: sourceHeading("h5"),
      h6: sourceHeading("h6"),
      pre: ({ children }) => {
        const element = Children.toArray(children).find(isValidElement);
        const props = element?.props as
          | { className?: string; children?: ReactNode }
          | undefined;
        const language = /^language-([a-zA-Z0-9_+-]+)$/.exec(
          props?.className || "",
        )?.[1];
        const kind = diagramKind(language);
        return kind ? (
          <DiagramBlock
            kind={kind}
            source={String(props?.children ?? "").replace(/\n$/, "")}
          />
        ) : (
          <pre>{children}</pre>
        );
      },
      a: ({ children, href }) => {
        const target = attachmentUrl(documentId, revisionId, href);
        return target ? (
          <a
            className="markdown-download"
            href={target}
            download
            rel="noreferrer"
          >
            {children}
          </a>
        ) : (
          <span
            className="markdown-link"
            title="外部或未授权链接不在阅读层打开"
          >
            {children}（链接未打开）
          </span>
        );
      },
      img: ({ alt, src }) => {
        const target = attachmentUrl(
          documentId,
          revisionId,
          typeof src === "string" ? src : undefined,
        );
        return target ? (
          <MarkdownAttachment source={target} alt={alt || "图片"} />
        ) : (
          <span className="attachment-note">
            [附件：{alt || "图片"}，只展示当前文档的授权相对附件]
          </span>
        );
      },
      code: ({ children, className }) => {
        const language = /^language-([a-zA-Z0-9_+-]+)$/.exec(
          className || "",
        )?.[1];
        if (!language) return <code>{children}</code>;
        const value = String(children ?? "");
        return (
          <RenderBoundary fallback={<code className={className}>{value}</code>}>
            <Suspense fallback={<code className={className}>{value}</code>}>
              <CodeHighlight language={language} value={value} />
            </Suspense>
          </RenderBoundary>
        );
      },
    }),
    [documentId, revisionId],
  );
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkMathAliases]}
        rehypePlugins={[
          rehypeMathQuota,
          [
            rehypeKatex,
            { trust: false, strict: "ignore", maxSize: 20, maxExpand: 1000 },
          ],
        ]}
        components={renderers}
      >
        {content || "当前章节还没有可用正文。"}
      </ReactMarkdown>
    </div>
  );
}
export function entityIcon(type?: string, size = 18) {
  return ["action", "ui_action"].includes(type || "") ? (
    <MousePointer2 size={size} />
  ) : ["symbol", "code_symbol", "sql"].includes(type || "") ? (
    <Code2 size={size} />
  ) : ["document", "section", "document_section"].includes(type || "") ? (
    <FileText size={size} />
  ) : (
    <Link2 size={size} />
  );
}
export function EntityCard({
  entity,
  onClick,
  selected = false,
}: {
  entity: Entity;
  onClick?: () => void;
  selected?: boolean;
}) {
  return (
    <button
      className={`entity-card ${selected ? "selected" : ""}`}
      onClick={onClick}
    >
      <span className={`entity-icon ${entity.type}`}>
        {entityIcon(entity.type)}
      </span>
      <span className="min-width">
        <strong>{entity.name}</strong>
        <small>
          {label(entity.type)} · {entity.stable_key}
        </small>
      </span>
      <ChevronRight size={16} />
    </button>
  );
}
export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  return (
    <div className="evidence-list">
      {evidence.map((e, i) => (
        <div className="evidence-item" key={e.id}>
          <div className="row between">
            <span className="evidence-number">
              E{String(i + 1).padStart(2, "0")}
            </span>
            <Tag value={e.kind || e.evidence_kind} />
          </div>
          <strong>{e.claim || e.artifact_path || "来源证据"}</strong>
          <code>{e.artifact_path || e.source || e.artifact_id || e.id}</code>
          {(e.content || e.excerpt) && <pre>{e.content || e.excerpt}</pre>}
          {(e.locator || e.source_locator) && (
            <small>{JSON.stringify(e.locator || e.source_locator)}</small>
          )}
          <div className="hash">
            <ShieldCheck size={12} />
            {(e.content_sha256 || "").slice(0, 18)}
            {e.content_sha256 ? "…" : "来源已固定"}
          </div>
        </div>
      ))}
      {!evidence.length && (
        <Notice tone="amber">当前对象暂无可访问的证据。可补充来源材料。</Notice>
      )}
    </div>
  );
}
export function RelationState({ relation }: { relation: Relation }) {
  return (
    <div className="row wrap">
      {relation.excluded && <Tag>已移除</Tag>}
      <Tag value={relation.validity_status} />
      {relation.manual_lock && (
        <span className="tag">
          <LockKeyhole size={12} />
          人工锁定
        </span>
      )}
    </div>
  );
}
export function Steps({
  steps,
  current,
}: {
  steps: string[];
  current: number;
}) {
  return (
    <div className="wizard-steps">
      {steps.map((s, i) => (
        <div key={s} className={i <= current ? "active" : ""}>
          <span>{i < current ? <Check size={16} /> : i + 1}</span>
          <strong>{s}</strong>
          {i < steps.length - 1 && <i />}
        </div>
      ))}
    </div>
  );
}
const allowed = new Set([
  "main",
  "article",
  "section",
  "div",
  "span",
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "header",
  "footer",
  "nav",
  "aside",
  "strong",
  "b",
  "em",
  "i",
  "small",
  "code",
  "pre",
  "br",
  "hr",
  "ul",
  "ol",
  "li",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "caption",
  "button",
  "label",
  "details",
  "summary",
  "blockquote",
  "dl",
  "dt",
  "dd",
]);
const omitted = new Set([
  "script",
  "style",
  "link",
  "iframe",
  "object",
  "embed",
  "svg",
  "math",
  "img",
  "video",
  "audio",
  "source",
  "meta",
  "base",
  "head",
  "title",
  "template",
  "noscript",
]);
const tableContainers = new Set([
  "table",
  "thead",
  "tbody",
  "tfoot",
  "tr",
  "colgroup",
]);
export function SafePrototype({
  html,
  actions,
  selected,
  onSelect,
}: {
  html: string;
  actions: Entity[];
  selected?: string;
  onSelect: (entity: Entity) => void;
}) {
  const doc = parseDocument(html);
  function render(node: AnyNode, index: number): ReactNode {
    if (node.type === "text") return node.data;
    if (!isTag(node)) return null;
    const el = node;
    if (omitted.has(el.name) || hiddenPassiveElement(el.attribs)) return null;
    const children = el.children
      .filter(
        (n) =>
          !(
            tableContainers.has(el.name) &&
            n.type === "text" &&
            !n.data.trim()
          ),
      )
      .map((n, i) => render(n, i));
    if (!allowed.has(el.name))
      return createElement(Fragment, { key: index }, ...children);
    const action = actions.find(
      (a) =>
        a.metadata?.status !== "removed" &&
        (a.stable_key === el.attribs["data-vks-action"] ||
          (typeof a.metadata?.selector === "string" &&
            matchesPassiveSelector(a.metadata.selector, el.name, el.attribs))),
    );
    const props: Record<string, unknown> = { key: index };
    if (action) {
      props.className = `prototype-hotspot ${selected === action.id ? "active" : ""}`;
      props.onClick = (e: React.MouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        onSelect(action);
      };
      props["aria-label"] = `选择动作：${action.name}`;
      props.title = `查看知识：${action.name}`;
    }
    if (el.name === "button") props.type = "button";
    if (el.name === "details") props.open = true;
    if (el.name === "td" || el.name === "th") {
      if (el.attribs.colspan)
        props.colSpan = Math.min(Number(el.attribs.colspan) || 1, 10);
    }
    return createElement(el.name, props, ...children);
  }
  return (
    <div className="safe-prototype">
      {doc.children.map((n, i) => render(n, i))}
    </div>
  );
}
export function ArrowLink({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="text-button" onClick={onClick}>
      {children}
      <ArrowRight size={15} />
    </button>
  );
}
export function Success({ children }: { children: ReactNode }) {
  return (
    <div className="success-inline">
      <CheckCircle2 size={17} />
      {children}
    </div>
  );
}

export class PreviewBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <Empty
        title="原型无法安全呈现"
        description="当前原型的结构不受支持，知识侧栏和动作列表仍可使用。请检查原型文件后重新导入。"
      />
    ) : (
      this.props.children
    );
  }
}
export function issueText(issue: unknown): string {
  if (typeof issue === "string") return issue;
  if (issue && typeof issue === "object") {
    const obj = issue as Record<string, unknown>;
    const message = String(
      obj.message || obj.reason || obj.code || "该材料部分内容未解析",
    );
    const path = obj.path || obj.artifact_path || obj.file;
    return path ? `${message}（${String(path)}）` : message;
  }
  return "存在待检查的材料，请展开诊断查看详情。";
}
