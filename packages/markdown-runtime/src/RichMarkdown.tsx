import {
  Children,
  createContext,
  createElement,
  isValidElement,
  lazy,
  memo,
  Suspense,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, {
  defaultUrlTransform,
  type Components,
} from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkCjkFriendly from "remark-cjk-friendly/parseOnly";
import remarkCjkFriendlyGfmStrikethrough from "remark-cjk-friendly-gfm-strikethrough/parseOnly";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import type { Root, RootContent, Definition, FootnoteDefinition } from "mdast";
import type { Processor } from "unified";
import { parseDocument } from "htmlparser2";
import { isTag, type AnyNode } from "domhandler";
import { DiagramBlock } from "./DiagramBlock";
import { diagramKind } from "./diagram-policy";
import { rehypeMathQuota, remarkMathAliases } from "./math-plugins";
import { RenderBoundary } from "./RenderBoundary";
import { openImagePreview } from "./imagePreview";
import { attachmentUrl, MarkdownAttachment } from "./MarkdownAttachment";

const CodeHighlight = lazy(() => import("./CodeHighlight"));
export type MarkdownDocument = {
  id: string;
  revision_id: string;
  name: string;
  path: string;
  namespace?: string;
  package_id?: string | null;
};
export type MarkdownReference = {
  revision_id: string;
  document_revision_id: string;
  document_name: string;
  name: string;
  content: string;
};
export type MarkdownSection = {
  id: string;
  revision_id: string;
  name: string;
  content: string;
  start_line?: number;
  end_line?: number;
  render_start_line?: number;
  heading_level?: number;
};
type AstNode = {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  position?: { start: { line: number } };
  children?: AstNode[];
  data?: { chapterHeading?: string };
};
export type ReadingContext = {
  projectId?: string;
  analysisSetId?: string;
  releaseId?: string;
  selectionVersion?: number;
  documents: MarkdownDocument[];
  onDocument?: (document: MarkdownDocument, fragment: string) => void;
  onSection?: (reference: MarkdownReference) => void;
  loadDocument?: (revisionId: string, signal: AbortSignal) => Promise<string>;
};
export const MarkdownReadingContext = createContext<ReadingContext>({
  documents: [],
});

// Parse source formatting before sanitizing, then add only platform-generated
// KaTeX output. Source scripts, handlers, arbitrary CSS and frames never run.
const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    "details",
    "summary",
    "kbd",
    "mark",
    "sub",
    "sup",
  ],
  attributes: {
    ...defaultSchema.attributes,
    code: [
      ...(defaultSchema.attributes?.code || []),
      ["className", /^language-[a-z0-9_+-]+$/, "math-inline", "math-display"],
    ],
    details: ["open"],
    th: [...(defaultSchema.attributes?.th || []), "align"],
    td: [...(defaultSchema.attributes?.td || []), "align"],
  },
  protocols: { ...defaultSchema.protocols, src: ["http", "https", "data"] },
};

function inlineRaster(source: string) {
  const match =
    /^data:image\/(png|jpe?g|gif|webp);base64,([a-z\d+/=\s]+)$/i.exec(source);
  if (!match || match[2].length > 28 * 1024 * 1024) return null;
  try {
    const head = atob(match[2].replace(/\s/g, "").slice(0, 64));
    const valid =
      match[1].toLowerCase() === "png"
        ? head.startsWith("\x89PNG\r\n\x1a\n")
        : /jpe?g/i.test(match[1])
          ? head.startsWith("\xff\xd8\xff")
          : match[1].toLowerCase() === "gif"
            ? /^GIF8[79]a/.test(head)
            : head.startsWith("RIFF") && head.slice(8, 12) === "WEBP";
    return valid ? source : null;
  } catch {
    return null;
  }
}

function remoteImage(source: string) {
  try {
    const url = new URL(source);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export const RichMarkdown = memo(function RichMarkdown({
  children,
  documentId,
  revisionId,
  references = [],
  onReference,
  omitInitialTitle,
  sections,
  selectedSection,
  sectionActions,
  sectionHeadingActions,
  streaming = false,
}: {
  children: string;
  documentId?: string;
  revisionId?: string;
  references?: MarkdownReference[];
  onReference?: (reference: MarkdownReference) => void;
  omitInitialTitle?: string;
  sections?: MarkdownSection[];
  selectedSection?: string;
  streaming?: boolean;
  sectionActions?: (
    section: MarkdownSection,
    position: "before" | "after",
  ) => ReactNode;
  sectionHeadingActions?: (section: MarkdownSection) => ReactNode;
}) {
  const reading = useContext(MarkdownReadingContext);
  const root = useRef<HTMLDivElement>(null);
  const referenceIds = [
    ...new Set(references.map((reference) => reference.document_revision_id)),
  ].sort();
  const referenceKey = JSON.stringify([
    reading.projectId,
    reading.analysisSetId,
    reading.releaseId,
    reading.selectionVersion,
    referenceIds,
  ]);
  const needsDefinitions =
    !documentId &&
    references.length > 0 &&
    /\[(?:\^[^\]\n]+|[^\]\n]+)\](?:\[[^\]\n]*\])?/.test(children);
  const [referenceState, setReferenceState] = useState<{
    key: string;
    documents: Record<string, string>;
  }>({ key: "", documents: {} });
  const referenceDocuments =
    referenceState.key === referenceKey ? referenceState.documents : {};
  useEffect(() => {
    if (!needsDefinitions || !reading.loadDocument || !referenceIds.length)
      return;
    const abort = new AbortController();
    let live = true;
    void Promise.all(
      referenceIds.map(async (id) => {
        try {
          return [id, await reading.loadDocument!(id, abort.signal)] as const;
        } catch {
          return [id, ""] as const;
        }
      }),
    ).then((entries) => {
      if (live)
        setReferenceState({
          key: referenceKey,
          documents: Object.fromEntries(entries),
        });
    });
    return () => {
      live = false;
      abort.abort();
    };
  }, [referenceKey, needsDefinitions]);
  const latest = useRef({
    reading,
    documentId,
    revisionId,
    references,
    onReference,
    sections,
    sectionActions,
    sectionHeadingActions,
    omitInitialTitle,
    referenceDocuments,
    streaming,
  });
  latest.current = {
    reading,
    documentId,
    revisionId,
    references,
    onReference,
    sections,
    sectionActions,
    sectionHeadingActions,
    omitInitialTitle,
    referenceDocuments,
    streaming,
  };
  const sourceImages = useRef(new Map<string, Set<string>>());
  const referencePlugin = useMemo(
    () =>
      function referencePlugin(this: Processor) {
        const processor = this;
        const parse = (source: string) => processor.parse(source) as Root;
        const isDefinition = (
          node: RootContent,
        ): node is Definition | FootnoteDefinition =>
          node.type === "definition" || node.type === "footnoteDefinition";
        const definitionsFrom = (sources: string[]) => {
          const definitions = new Map<
            string,
            { source: string; signature: string; ambiguous: boolean }
          >();
          for (const source of sources)
            for (const node of parse(source).children.filter(isDefinition)) {
              const start = node.position?.start.offset,
                end = node.position?.end.offset;
              if (start === undefined || end === undefined) continue;
              const key = node.type + ":" + node.identifier;
              const signature = JSON.stringify(node, (key, value) =>
                key === "position" ? undefined : value,
              );
              const old = definitions.get(key);
              if (old) old.ambiguous ||= old.signature !== signature;
              else
                definitions.set(key, {
                  source: source.slice(start, end),
                  signature,
                  ambiguous: false,
                });
            }
          return [...definitions.values()]
            .filter((item) => !item.ambiguous)
            .map((item) => item.source)
            .join("\n\n");
        };
        const augment = (source: string, definitions: string) => {
          const root = parse(source + "\n\n" + definitions);
          // An unfinished code/HTML block must not swallow appended source notes.
          if (
            root.children.some(
              (node) =>
                (node.position?.start.offset ?? source.length) <
                  source.length &&
                (node.position?.end.offset ?? 0) > source.length,
            )
          )
            return parse(source);
          root.children = root.children.filter(
            (node) =>
              (node.position?.start.offset ?? source.length) < source.length ||
              isDefinition(node),
          );
          return root;
        };
        return (tree: Root) => {
          const images = new Map<string, Set<string>>();
          const add = (url: string, document: string) => {
            if (!images.has(url)) images.set(url, new Set());
            images.get(url)!.add(document);
          };
          for (const reference of references) {
            const root = augment(
              reference.content,
              definitionsFrom([
                referenceDocuments[reference.document_revision_id] || "",
              ]),
            );
            const definitions = new Map(
              root.children
                .filter(isDefinition)
                .map((node) => [node.type + ":" + node.identifier, node]),
            );
            const visitedFootnotes = new Set<string>();
            const htmlImages = (nodes: AnyNode[]) =>
              nodes.forEach((node) => {
                if (
                  !isTag(node) ||
                  [
                    "script",
                    "style",
                    "iframe",
                    "object",
                    "embed",
                    "template",
                    "noscript",
                  ].includes(node.name)
                )
                  return;
                if (node.name === "img" && node.attribs.src)
                  add(node.attribs.src, reference.document_revision_id);
                htmlImages(node.children);
              });
            const walk = (node: RootContent) => {
              if (node.type === "image")
                add(node.url, reference.document_revision_id);
              else if (node.type === "imageReference") {
                const definition = definitions.get(
                  "definition:" + node.identifier,
                );
                if (definition?.type === "definition")
                  add(definition.url, reference.document_revision_id);
              } else if (node.type === "html")
                htmlImages(parseDocument(node.value).children);
              else if (
                node.type === "footnoteReference" &&
                !visitedFootnotes.has(node.identifier)
              ) {
                visitedFootnotes.add(node.identifier);
                const definition = definitions.get(
                  "footnoteDefinition:" + node.identifier,
                );
                if (definition?.type === "footnoteDefinition")
                  definition.children.forEach(walk);
              }
              if ("children" in node) node.children.forEach(walk);
            };
            root.children.filter((node) => !isDefinition(node)).forEach(walk);
          }
          sourceImages.current = images;
          if (Object.keys(referenceDocuments).length)
            tree.children = augment(
              children,
              definitionsFrom(Object.values(referenceDocuments)),
            ).children;
        };
      },
    [referenceDocuments, references, children],
  );
  const chapterPlugin = useMemo(
    () =>
      function chapterPlugin() {
        return (tree: AstNode) => {
          if (!sections?.length || !tree.children) return;
          const starts = sections
            .map((section) => ({
              section,
              line: section.render_start_line ?? section.start_line ?? 0,
            }))
            .filter((item) => item.line > 0)
            .sort((a, b) => a.line - b.line);
          if (!starts.length) return;
          const children: AstNode[] = [];
          let current: AstNode | undefined,
            selected = "";
          for (const child of tree.children) {
            const line = child.position?.start.line;
            const match =
              typeof line === "number"
                ? [...starts].reverse().find((item) => item.line <= line)
                : undefined;
            if (match && match.section.revision_id !== selected) {
              selected = match.section.revision_id;
              current = {
                type: "element",
                tagName: "section",
                properties: {
                  id: "section-" + selected,
                  className: [
                    "chapter",
                    ...(selected === selectedSection
                      ? ["chapter-selected"]
                      : []),
                  ],
                  dataChapter: selected,
                },
                children: [],
              };
              children.push(current);
              if (child.tagName && /^h[1-6]$/.test(child.tagName)) {
                child.data = { ...child.data, chapterHeading: selected };
              }
            }
            (current?.children || children).push(child);
          }
          tree.children = children;
        };
      },
    [sections, selectedSection],
  );
  const renderers = useMemo<Components>(() => {
    const sourceDocument = (source?: string) => {
      const { reading, documentId, revisionId, references } = latest.current;
      if (documentId && revisionId)
        return { id: documentId, revision_id: revisionId };
      const permitted = source ? sourceImages.current.get(source) : undefined;
      const relevant = references.filter(
        (ref) => !source || permitted?.has(ref.document_revision_id),
      );
      const ids = [
        ...new Set(
          (relevant.length ? relevant : references).map(
            (ref) => ref.document_revision_id,
          ),
        ),
      ];
      return ids.length === 1
        ? reading.documents.find((doc) => doc.revision_id === ids[0])
        : undefined;
    };
    const heading =
      (
        tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6",
      ): NonNullable<Components["h1"]> =>
      ({ node, children, ...props }) => {
        const { omitInitialTitle, sections, sectionHeadingActions } =
          latest.current;
        const title = sections?.find(
          (section) =>
            section.name === omitInitialTitle &&
            /^ {0,3}#\s/.test(section.content),
        );
        const titleLine = title?.render_start_line ?? title?.start_line ?? 1;
        if (
          tag === "h1" &&
          node?.position?.start.line === titleLine &&
          typeof children === "string" &&
          children.trim() === omitInitialTitle?.trim()
        )
          return null;
        const chapter = (node as AstNode | undefined)?.data?.chapterHeading;
        const heading = createElement(
          tag,
          { ...props, "data-source-line": node?.position?.start.line },
          children,
        );
        const section =
          chapter && sections?.find((item) => item.revision_id === chapter);
        const actions = section && sectionHeadingActions?.(section);
        return actions ? (
          <div className="chapter-heading">
            {heading}
            {actions}
          </div>
        ) : (
          heading
        );
      };
    return {
      section: ({ node, children, ...props }) => {
        const { sections, sectionActions } = latest.current;
        const section = sections?.find(
          (item) => item.revision_id === node?.properties.dataChapter,
        );
        return (
          <section {...props}>
            {section && sectionActions?.(section, "before")}
            {children}
            {section && sectionActions?.(section, "after")}
          </section>
        );
      },
      h1: heading("h1"),
      h2: heading("h2"),
      h3: heading("h3"),
      h4: heading("h4"),
      h5: heading("h5"),
      h6: heading("h6"),
      pre: ({ children }) => {
        const element = Children.toArray(children).find(isValidElement);
        const props = element?.props as
          | { className?: string; children?: ReactNode }
          | undefined;
        const language = /^language-([a-zA-Z0-9_+-]+)$/.exec(
          props?.className || "",
        )?.[1];
        const kind = diagramKind(language);
        return kind && !latest.current.streaming ? (
          <DiagramBlock
            kind={kind}
            source={String(props?.children ?? "").replace(/\n$/, "")}
          />
        ) : (
          <pre>{children}</pre>
        );
      },
      code: ({ children, className }) => {
        const language = /^language-([a-zA-Z0-9_+-]+)$/.exec(
          className || "",
        )?.[1];
        const value = String(children ?? "");
        if (!language || diagramKind(language) || latest.current.streaming)
          return <code className={className}>{children}</code>;
        return (
          <RenderBoundary fallback={<code>{value}</code>}>
            <Suspense fallback={<code>{value}</code>}>
              <CodeHighlight language={language} value={value} />
            </Suspense>
          </RenderBoundary>
        );
      },
      img: ({ src, alt, title }) => {
        const { reading, documentId, revisionId } = latest.current;
        const source = typeof src === "string" ? src : "";
        const inline = inlineRaster(source),
          remote = remoteImage(source);
        if (
          remote &&
          !(documentId && revisionId) &&
          !sourceImages.current.has(source)
        )
          return (
            <span className="attachment-note">
              {alt || "外部图片"}：此地址未出现在引用原文中。
            </span>
          );
        if (inline || remote)
          return (
            <img
              src={inline || remote!}
              alt={alt || "文档图片"}
              title={title}
              referrerPolicy="no-referrer"
              loading="lazy"
              tabIndex={0}
              aria-haspopup="dialog"
              onClick={(event) =>
                openImagePreview(
                  inline || remote!,
                  alt || "文档图片",
                  event.currentTarget.ownerDocument,
                  event.currentTarget,
                )
              }
              onKeyDown={(event) => {
                if (["Enter", " "].includes(event.key)) {
                  event.preventDefault();
                  openImagePreview(
                    inline || remote!,
                    alt || "文档图片",
                    event.currentTarget.ownerDocument,
                    event.currentTarget,
                  );
                }
              }}
            />
          );
        const doc = sourceDocument(source);
        const target = attachmentUrl(
          doc?.id,
          doc?.revision_id,
          source,
          reading,
        );
        return target ? (
          <MarkdownAttachment source={target} alt={alt || "文档图片"} />
        ) : (
          <span className="attachment-note" role="status">
            {alt || "图片"}：
            {source ? "未找到唯一的引用文档，请打开原文查看" : "图片地址无效"}
          </span>
        );
      },
      a: ({ href, children }) => {
        const { reading, references } = latest.current;
        const source = href || "";
        const doc = sourceDocument();
        if (source.startsWith("#") && !/^#\d+$/.test(source)) {
          let fragment = "";
          try {
            fragment = decodeURIComponent(source.slice(1));
          } catch {
            /* Invalid fragments stay inert. */
          }
          if (fragment)
            return (
              <a
                href={source}
                onClick={(event) => {
                  const target = [
                    fragment,
                    "user-content-" + fragment,
                    "vks-source-" + fragment,
                  ]
                    .map((id) => root.current?.ownerDocument.getElementById(id))
                    .find((node) => node && root.current?.contains(node));
                  if (target) {
                    event.preventDefault();
                    target.scrollIntoView({
                      behavior: "smooth",
                      block: "start",
                    });
                  } else if (doc) {
                    const origin = reading.documents.find(
                      (item) => item.revision_id === doc.revision_id,
                    );
                    if (origin && reading.onDocument) {
                      event.preventDefault();
                      reading.onDocument(origin, fragment);
                    }
                  }
                }}
              >
                {children}
              </a>
            );
        }
        if (source.startsWith("#") && /^#\d+$/.test(source)) {
          const reference = references[Number(source.slice(1)) - 1];
          const open = latest.current.onReference || reading.onSection;
          if (reference && open)
            return (
              <button
                type="button"
                className="markdown-reference"
                aria-label={`原文引用 ${source.slice(1)}`}
                onClick={() => open(reference)}
              >
                {children}
              </button>
            );
        }
        if (
          doc &&
          source &&
          !/^[a-z][a-z\d+.-]*:/i.test(source) &&
          !source.startsWith("//")
        ) {
          const origin = reading.documents.find(
            (item) => item.revision_id === doc.revision_id,
          );
          if (origin) {
            try {
              const target = new URL(
                source,
                "https://document.invalid/" + origin.path,
              );
              const path = decodeURIComponent(target.pathname).replace(
                /^\//,
                "",
              );
              const candidates = reading.documents.filter(
                (item) =>
                  item.path === path &&
                  (!origin.namespace || item.namespace === origin.namespace),
              );
              const match = source.startsWith("#")
                ? origin
                : candidates.length === 1
                  ? candidates[0]
                  : undefined;
              if (match && reading.onDocument)
                return (
                  <a
                    href={"#" + match.revision_id}
                    onClick={(event) => {
                      event.preventDefault();
                      reading.onDocument?.(
                        match,
                        decodeURIComponent(target.hash.slice(1)),
                      );
                    }}
                  >
                    {children}
                  </a>
                );
            } catch {
              /* Fall through to an authorized attachment or inert text. */
            }
          }
          const target = attachmentUrl(
            doc.id,
            doc.revision_id,
            source,
            reading,
          );
          if (target)
            return (
              <a href={target} download rel="noreferrer">
                {children}
              </a>
            );
        }
        const safe = defaultUrlTransform(source);
        return safe && /^(https?:|mailto:)/i.test(safe) ? (
          <a href={safe} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        ) : (
          <span>{children}</span>
        );
      },
    };
  }, []);
  const citationPlugin = useMemo(
    () => () => (tree: AstNode) => {
      if (!latest.current.onReference && !latest.current.reading.onSection)
        return;
      const count = latest.current.references.length;
      function visit(node: AstNode) {
        if (
          ["a", "code", "pre", "script", "math"].includes(node.tagName || "") ||
          String(node.properties?.className || "").includes("katex")
        )
          return;
        if (!node.children) return;
        node.children = node.children.flatMap((child) => {
          if (child.type !== "text" || !child.value) {
            visit(child);
            return [child];
          }
          const result: AstNode[] = [];
          let offset = 0;
          for (const match of child.value.matchAll(/\[(\d+)\]/g)) {
            const number = Number(match[1]);
            if (number < 1 || number > count) continue;
            if (match.index! > offset)
              result.push({
                type: "text",
                value: child.value.slice(offset, match.index),
              });
            result.push({
              type: "element",
              tagName: "a",
              properties: { href: `#${number}` },
              children: [{ type: "text", value: match[0] }],
            });
            offset = match.index! + match[0].length;
          }
          if (!result.length) return [child];
          if (offset < child.value.length)
            result.push({ type: "text", value: child.value.slice(offset) });
          return result;
        });
      }
      visit(tree);
    },
    [],
  );
  const content = children;
  return (
    <div className="markdown" ref={root}>
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkCjkFriendly,
          remarkCjkFriendlyGfmStrikethrough,
          remarkMath,
          remarkMathAliases,
          referencePlugin,
        ]}
        rehypePlugins={[
          rehypeRaw,
          [rehypeSanitize, schema],
          citationPlugin,
          rehypeMathQuota,
          [
            rehypeKatex,
            { trust: false, strict: "ignore", maxSize: 20, maxExpand: 1000 },
          ],
          chapterPlugin,
        ]}
        urlTransform={(url) => inlineRaster(url) || defaultUrlTransform(url)}
        components={renderers}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
