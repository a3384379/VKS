import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ChevronRight,
  FileText,
  Folder,
  FolderOpen,
  LoaderCircle,
  Search,
} from "lucide-react";

export type NavigationDocument = {
  kind: "document";
  id: string;
  entity_id?: string;
  name: string;
  path: string;
};
export type NavigationFolder = {
  kind: "folder";
  id: string;
  name: string;
  path: string;
  children: NavigationNode[];
};
export type NavigationNode = NavigationDocument | NavigationFolder;
export type NavigationCategory = {
  id: string;
  name: string;
  path: string;
  children: NavigationNode[];
};
export type NavigationPackage = {
  id: string;
  name: string;
  categories: NavigationCategory[];
};
export type DocumentNavigation = { packages: NavigationPackage[] };
export type NavigationSection = {
  id: string;
  revision_id: string;
  name: string;
  content: string;
  heading_level?: number;
};
export type NavigationArticle = {
  revision_id: string;
  name: string;
  sections: NavigationSection[];
};
type DocumentInfo = {
  id: string;
  revision_id: string;
  name: string;
  path: string;
  namespace?: string;
  package_id?: string | null;
};
type Entry = {
  document: NavigationDocument;
  package: NavigationPackage;
  category: NavigationCategory;
  folders: NavigationFolder[];
};

export function navigationEntries(navigation?: DocumentNavigation): Entry[] {
  const entries: Entry[] = [];
  for (const pack of navigation?.packages || [])
    for (const category of pack.categories) {
      const visit = (nodes: NavigationNode[], folders: NavigationFolder[]) => {
        for (const node of nodes)
          if (node.kind === "document")
            entries.push({ document: node, package: pack, category, folders });
          else visit(node.children, [...folders, node]);
      };
      visit(category.children, []);
    }
  return entries;
}

export function firstNavigationDocument(
  navigation: DocumentNavigation | undefined,
  documents: DocumentInfo[],
): string {
  const visible = new Set(documents.map((doc) => doc.revision_id));
  return (
    navigationEntries(navigation).find((entry) =>
      visible.has(entry.document.id),
    )?.document.id ||
    documents[0]?.revision_id ||
    ""
  );
}

function visibleNavigation(
  navigation: DocumentNavigation | undefined,
  documents: DocumentInfo[],
): DocumentNavigation {
  const known = new Map(documents.map((doc) => [doc.revision_id, doc]));
  const retained = new Set<string>();
  const nodes = (items: NavigationNode[]): NavigationNode[] =>
    items.flatMap<NavigationNode>((node) => {
      if (node.kind === "document") {
        const doc = known.get(node.id);
        if (!doc) return [];
        retained.add(doc.revision_id);
        return [{ ...node, name: doc.name, path: node.path || doc.path }];
      }
      const children = nodes(node.children);
      return children.length ? [{ ...node, children }] : [];
    });
  const packages = (navigation?.packages || [])
    .map((pack) => ({
      ...pack,
      categories: pack.categories
        .map((category) => ({
          ...category,
          children: nodes(category.children),
        }))
        .filter((category) => category.children.length),
    }))
    .filter((pack) => pack.categories.length);
  const extra = documents.filter((doc) => !retained.has(doc.revision_id));
  if (extra.length)
    packages.push({
      id: "vks-visible-documents",
      name: "项目文档",
      categories: [
        {
          id: "vks-visible-all",
          name: "全部文档",
          path: "",
          children: extra.map((doc) => ({
            kind: "document",
            id: doc.revision_id,
            entity_id: doc.id,
            name: doc.name,
            path: doc.path,
          })),
        },
      ],
    });
  return { packages };
}

type Heading = { section: NavigationSection; children: Heading[] };
function chapterTree(document: NavigationArticle): Heading[] {
  const roots: Heading[] = [],
    stack: Array<{ heading: Heading; level: number }> = [];
  let firstHeading = true;
  for (let index = 0; index < document.sections.length; index++) {
    const section = document.sections[index];
    if (section.heading_level === 0) continue;
    const level =
      section.heading_level ||
      /^ {0,3}(#{1,6})\s/.exec(section.content)?.[1].length ||
      1;
    const title = firstHeading && level === 1 && section.name === document.name;
    firstHeading = false;
    if (title) continue;
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    const heading = { section, children: [] };
    (stack.length ? stack[stack.length - 1].heading.children : roots).push(
      heading,
    );
    stack.push({ heading, level });
  }
  return roots;
}

function headingAncestors(
  headings: Heading[],
  selected: string,
): string[] | undefined {
  for (const heading of headings) {
    if (heading.section.revision_id === selected) return [];
    const child = headingAncestors(heading.children, selected);
    if (child) return [heading.section.revision_id, ...child];
  }
}

export function DocumentLibrary({
  navigation,
  documents,
  selectedDocument,
  selectedSection,
  currentDocument,
  scopeKey,
  selectionVersion,
  query,
  onQuery,
  onOpen,
  loadDocument,
  linkedDocuments = [],
  hidden,
  children,
}: {
  navigation?: DocumentNavigation;
  documents: DocumentInfo[];
  selectedDocument: string;
  selectedSection: string;
  currentDocument: NavigationArticle | null;
  scopeKey: string;
  selectionVersion?: number;
  query: string;
  onQuery: (value: string) => void;
  onOpen: (document: string, section?: string) => void;
  loadDocument: (
    revision: string,
    signal: AbortSignal,
  ) => Promise<NavigationArticle>;
  linkedDocuments?: string[];
  hidden?: boolean;
  children: ReactNode;
}) {
  const tree = useMemo(
    () => visibleNavigation(navigation, documents),
    [navigation, documents],
  );
  const entries = useMemo(() => navigationEntries(tree), [tree]);
  const location = entries.find(
    (entry) => entry.document.id === selectedDocument,
  );
  const [packageId, setPackage] = useState(
    location?.package.id || tree.packages[0]?.id || "",
  );
  const [categoryId, setCategory] = useState(
    location?.category.id || tree.packages[0]?.categories[0]?.id || "",
  );
  const [folders, setFolders] = useState<Set<string>>(new Set());
  const [articles, setArticles] = useState<Set<string>>(
    new Set(selectedDocument ? [selectedDocument] : []),
  );
  const [chapters, setChapters] = useState<Set<string>>(new Set());
  const [cache, setCache] = useState<Record<string, NavigationArticle>>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Set<string>>(new Set());
  const loader = useRef(loadDocument),
    controllers = useRef(new Map<string, AbortController>());
  const sidebar = useRef<HTMLDivElement>(null);
  const categoryTabs = useRef<HTMLDivElement>(null);
  loader.current = loadDocument;
  const identity = scopeKey + ":" + selectionVersion;
  const generation = useRef(identity);
  const entryPath = location
    ? [
        location.package.id,
        location.category.id,
        ...location.folders.map((folder) => folder.id),
      ].join("|")
    : "";
  useEffect(() => {
    generation.current = identity;
    for (const controller of controllers.current.values()) controller.abort();
    controllers.current.clear();
    setCache({});
    setPending(new Set());
    setErrors(new Set());
    return () => {
      for (const controller of controllers.current.values()) controller.abort();
      controllers.current.clear();
    };
  }, [identity]);
  useEffect(() => {
    if (!location) return;
    setPackage(location.package.id);
    setCategory(location.category.id);
    setFolders(
      (previous) =>
        new Set([...previous, ...location.folders.map((folder) => folder.id)]),
    );
    setArticles((previous) => new Set([...previous, selectedDocument]));
  }, [selectedDocument, entryPath]);
  useEffect(() => {
    if (
      !currentDocument ||
      !documents.some((doc) => doc.revision_id === currentDocument.revision_id)
    )
      return;
    setCache((previous) => ({
      ...previous,
      [currentDocument.revision_id]: currentDocument,
    }));
  }, [currentDocument, documents]);
  useEffect(() => {
    if (!selectedSection || !cache[selectedDocument]) return;
    const ancestors = headingAncestors(
      chapterTree(cache[selectedDocument]),
      selectedSection,
    );
    if (ancestors)
      setChapters((previous) => new Set([...previous, ...ancestors]));
  }, [selectedDocument, selectedSection, cache]);
  const request = (revision: string) => {
    if (cache[revision] || controllers.current.has(revision)) return;
    const controller = new AbortController(),
      expected = identity;
    controllers.current.set(revision, controller);
    setPending((previous) => new Set([...previous, revision]));
    setErrors(
      (previous) => new Set([...previous].filter((id) => id !== revision)),
    );
    void loader
      .current(revision, controller.signal)
      .then((document) => {
        if (
          !controller.signal.aborted &&
          generation.current === expected &&
          document.revision_id === revision
        )
          setCache((previous) => ({ ...previous, [revision]: document }));
      })
      .catch(() => {
        if (!controller.signal.aborted && generation.current === expected)
          setErrors((previous) => new Set([...previous, revision]));
      })
      .finally(() => {
        if (controllers.current.get(revision) === controller)
          controllers.current.delete(revision);
        if (generation.current === expected)
          setPending(
            (previous) =>
              new Set([...previous].filter((id) => id !== revision)),
          );
      });
  };
  useEffect(() => {
    for (const revision of articles)
      if (
        documents.some((doc) => doc.revision_id === revision) &&
        !cache[revision] &&
        !errors.has(revision)
      )
        request(revision);
  }, [articles, cache, identity, documents]);
  const pack =
    tree.packages.find((pack) => pack.id === packageId) ||
    location?.package ||
    tree.packages[0];
  const category =
    pack?.categories.find((category) => category.id === categoryId) ||
    (location?.package.id === pack?.id ? location?.category : undefined) ||
    pack?.categories[0];
  const categoryEntries = entries.filter(
    (entry) =>
      entry.category.id === category?.id && entry.package.id === pack?.id,
  );
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      (
        sidebar.current?.querySelector(".document-heading-row.selected") ||
        sidebar.current?.querySelector(".document-open[aria-current='page']")
      )?.scrollIntoView({ block: "nearest", inline: "nearest" }),
    );
    return () => cancelAnimationFrame(frame);
  }, [selectedDocument, selectedSection, entryPath, cache[selectedDocument]]);
  useEffect(() => {
    categoryTabs.current
      ?.querySelector("button.active")
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [category?.id]);
  const toggle = (
    update: (
      value: Set<string> | ((previous: Set<string>) => Set<string>),
    ) => void,
    id: string,
  ) =>
    update((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const openCategory = (
    pack: NavigationPackage,
    category: NavigationCategory,
  ) => {
    setPackage(pack.id);
    setCategory(category.id);
    onQuery("");
    const first = entries.find(
      (entry) =>
        entry.package.id === pack.id && entry.category.id === category.id,
    );
    if (first) onOpen(first.document.id);
  };
  const headingNodes = (nodes: Heading[], documentId: string): ReactNode => (
    <ul className="document-heading-tree">
      {nodes.map(({ section, children }) => (
        <li key={section.revision_id}>
          <div
            className={
              "document-heading-row " +
              (selectedSection === section.revision_id ? "selected" : "")
            }
          >
            {children.length ? (
              <button
                className="tree-toggle"
                aria-label={`${chapters.has(section.revision_id) ? "收起" : "展开"}下级章节 ${section.name}`}
                aria-expanded={chapters.has(section.revision_id)}
                onClick={() => toggle(setChapters, section.revision_id)}
              >
                <ChevronRight size={12} />
              </button>
            ) : (
              <span className="tree-spacer" />
            )}
            <button
              className="heading-link"
              onClick={() => onOpen(documentId, section.revision_id)}
            >
              {section.name}
            </button>
          </div>
          {children.length > 0 &&
            chapters.has(section.revision_id) &&
            headingNodes(children, documentId)}
        </li>
      ))}
    </ul>
  );
  const nodes = (items: NavigationNode[]): ReactNode => (
    <ul className="document-tree">
      {items.map((node) =>
        node.kind === "folder" ? (
          <li key={node.id}>
            <button
              className="document-folder"
              aria-expanded={folders.has(node.id)}
              onClick={() => toggle(setFolders, node.id)}
            >
              <ChevronRight size={13} />
              {folders.has(node.id) ? (
                <FolderOpen size={15} />
              ) : (
                <Folder size={15} />
              )}
              <span>{node.name}</span>
            </button>
            {folders.has(node.id) && nodes(node.children)}
          </li>
        ) : (
          <li
            key={node.id}
            className={
              "document-node " + (selectedDocument === node.id ? "current" : "")
            }
          >
            <div className="document-node-row">
              <button
                className="tree-toggle"
                aria-label={`${articles.has(node.id) ? "收起" : "展开"}章节 ${node.name}`}
                aria-expanded={articles.has(node.id)}
                onClick={() => {
                  toggle(setArticles, node.id);
                  if (!articles.has(node.id)) request(node.id);
                }}
              >
                <ChevronRight size={13} />
              </button>
              <button
                className="document-open"
                aria-current={selectedDocument === node.id ? "page" : undefined}
                onClick={() => onOpen(node.id)}
              >
                <FileText size={14} />
                <span>{node.name}</span>
                {linkedDocuments.includes(node.id) && (
                  <i className="link-dot" />
                )}
              </button>
            </div>
            {articles.has(node.id) &&
              (pending.has(node.id) ? (
                <div className="tree-loading">
                  <LoaderCircle size={12} />
                  正在读取章节…
                </div>
              ) : errors.has(node.id) ? (
                <button className="tree-retry" onClick={() => request(node.id)}>
                  章节未能读取，重试
                </button>
              ) : cache[node.id] ? (
                chapterTree(cache[node.id]).length ? (
                  headingNodes(chapterTree(cache[node.id]), node.id)
                ) : (
                  <div className="tree-loading">本篇为连续正文</div>
                )
              ) : null)}
          </li>
        ),
      )}
    </ul>
  );
  const matches = query.trim()
    ? entries.filter((entry) =>
        [
          entry.document.name,
          entry.document.path,
          entry.category.name,
          ...entry.folders.map((folder) => folder.name),
        ]
          .join(" ")
          .toLowerCase()
          .includes(query.trim().toLowerCase()),
      )
    : [];
  const uniqueMatches = [
    ...new Map(matches.map((entry) => [entry.document.id, entry])).values(),
  ];
  return (
    <section className="document-library" hidden={hidden}>
      <nav className="document-categories" aria-label="文档分类">
        {tree.packages.length > 1 && (
          <label className="document-package-picker">
            <span>资料包</span>
            <select
              aria-label="文档资料包"
              value={pack?.id || ""}
              onChange={(event) => {
                const next = tree.packages.find(
                  (pack) => pack.id === event.target.value,
                );
                if (next?.categories[0]) openCategory(next, next.categories[0]);
              }}
            >
              {tree.packages.map((pack) => (
                <option key={pack.id} value={pack.id}>
                  {pack.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="document-category-tabs" ref={categoryTabs}>
          {pack?.categories.map((item) => (
            <button
              key={item.id}
              className={item.id === category?.id ? "active" : ""}
              aria-current={item.id === category?.id ? "page" : undefined}
              onClick={() => openCategory(pack, item)}
            >
              {item.name}
              <span>
                {
                  new Set(
                    entries
                      .filter(
                        (entry) =>
                          entry.package.id === pack.id &&
                          entry.category.id === item.id,
                      )
                      .map((entry) => entry.document.id),
                  ).size
                }
              </span>
            </button>
          ))}
        </div>
      </nav>
      <div className="documents-space">
        <aside className="doc-sidebar">
          <div className="sidebar-title">
            <FileText size={17} />
            <b>{query ? "全部搜索结果" : category?.name || "项目知识库"}</b>
            <span>
              {query
                ? uniqueMatches.length
                : new Set(categoryEntries.map((entry) => entry.document.id))
                    .size}
            </span>
          </div>
          <div className="search">
            <Search size={15} />
            <input
              aria-label="搜索文档"
              placeholder="搜索全部分类的文档…"
              value={query}
              onChange={(event) => onQuery(event.target.value)}
            />
          </div>
          <div className="doc-list" ref={sidebar}>
            {query.trim() ? (
              <div className="document-search-results">
                {uniqueMatches.length ? (
                  uniqueMatches.map((entry) => (
                    <button
                      key={entry.document.id}
                      onClick={() => {
                        onQuery("");
                        onOpen(entry.document.id);
                      }}
                    >
                      <FileText size={14} />
                      <span>
                        {entry.document.name}
                        <small>
                          {[
                            entry.package.name,
                            entry.category.name,
                            ...entry.folders.map((folder) => folder.name),
                          ].join(" / ")}
                        </small>
                      </span>
                    </button>
                  ))
                ) : (
                  <p className="document-tree-empty">没有匹配的文档</p>
                )}
              </div>
            ) : category ? (
              nodes(category.children)
            ) : (
              <p className="document-tree-empty">当前版本没有可阅读的文档</p>
            )}
          </div>
        </aside>
        {children}
      </div>
    </section>
  );
}
