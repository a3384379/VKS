import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  ChevronRight,
  FileText,
  Folder,
  LoaderCircle,
  Menu,
  Printer,
  Search,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import {
  handbookConfig,
  handbookEntries,
  handbookNavigation,
  handbookNeighbors,
  handbookOutline,
  handbookSearchSnippet,
  handbookTemplateManifest,
  type HandbookArticle,
  type HandbookConfig,
  type HandbookDocument,
  type HandbookHeading,
  type HandbookNavigation,
  type HandbookNode,
} from "../../../packages/handbook-template/src";
import "../../../packages/handbook-template/src/skin.css";
import "../../../packages/handbook-template/src/framework.css";

export type HandbookSearchResult = {
  document: string;
  section?: string;
  title: string;
  snippet: string;
};
export type HandbookTemplateProps = {
  projectName: string;
  documents: HandbookDocument[];
  navigation?: HandbookNavigation;
  selectedDocument: string;
  selectedSection: string;
  currentDocument: HandbookArticle | null;
  scopeKey: string;
  selectionVersion?: number;
  loadDocument: (
    revision: string,
    signal: AbortSignal,
  ) => Promise<HandbookArticle>;
  searchDocuments?: (
    query: string,
    signal: AbortSignal,
  ) => Promise<HandbookSearchResult[]>;
  onOpen: (document: string, section?: string) => void;
  onOverview?: () => void;
  onAskAI: () => void;
  onUpdate: () => void;
  onPrint?: () => void;
  readOnly?: boolean;
  config?: Partial<HandbookConfig>;
  renderReader: ReactNode;
  hidden?: boolean;
};

export function HandbookTemplate({
  projectName,
  documents,
  navigation,
  selectedDocument,
  selectedSection,
  currentDocument,
  scopeKey,
  selectionVersion,
  loadDocument,
  searchDocuments,
  onOpen,
  onOverview,
  onAskAI,
  onUpdate,
  onPrint,
  readOnly = false,
  config,
  renderReader,
  hidden = false,
}: HandbookTemplateProps) {
  const settings = useMemo(
    () => handbookConfig(projectName, config),
    [projectName, config],
  );
  const tree = useMemo(
    () => handbookNavigation(navigation, documents),
    [navigation, documents],
  );
  const entries = useMemo(() => handbookEntries(tree), [tree]);
  const selected = entries.find(
    (entry) => entry.document.id === selectedDocument,
  );
  const [packageId, setPackage] = useState(
    selected?.package.id || tree.packages[0]?.id || "",
  );
  const [categoryId, setCategory] = useState(
    selected?.category.id || tree.packages[0]?.categories[0]?.id || "",
  );
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [folders, setFolders] = useState<Set<string>>(new Set());
  const [outlineCache, setOutlineCache] = useState<
    Record<string, HandbookArticle>
  >({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Set<string>>(new Set());
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [fullMatches, setFullMatches] = useState<HandbookSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [scanned, setScanned] = useState(0);
  const [searchError, setSearchError] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null),
    searchInput = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLElement>(null);
  const cache = useRef(new Map<string, HandbookArticle>());
  const loads = useRef(new Map<string, AbortController>());
  const loader = useRef(loadDocument);
  loader.current = loadDocument;
  const searcher = useRef(searchDocuments);
  searcher.current = searchDocuments;
  const generation = scopeKey + ":" + selectionVersion;
  const generationRef = useRef(generation);
  const inputId = useId();
  const visibleIds = useMemo(
    () => new Set(documents.map((doc) => doc.revision_id)),
    [documents],
  );
  const pack =
    tree.packages.find((item) => item.id === packageId) ||
    selected?.package ||
    tree.packages[0];
  const category =
    pack?.categories.find((item) => item.id === categoryId) ||
    selected?.category ||
    pack?.categories[0];
  const categoryEntries = entries.filter(
    (entry) =>
      entry.package.id === pack?.id && entry.category.id === category?.id,
  );
  const neighbors = useMemo(
    () => handbookNeighbors(entries, selectedDocument),
    [entries, selectedDocument],
  );
  const uniqueDocuments = useMemo(
    () => [
      ...new Map(entries.map((entry) => [entry.document.id, entry])).values(),
    ],
    [entries],
  );
  const cacheArticle = (article: HandbookArticle) => {
    cache.current.delete(article.revision_id);
    cache.current.set(article.revision_id, article);
    while (cache.current.size > 80)
      cache.current.delete(cache.current.keys().next().value!);
  };
  const headingsOnly = (article: HandbookArticle) => ({
    revision_id: article.revision_id,
    name: article.name,
    sections: article.sections.map((section) => ({
      ...section,
      heading_level:
        section.heading_level ??
        /^ {0,3}(#{1,6})\s/.exec(section.content)?.[1].length ??
        1,
      content: "",
    })),
  });
  useEffect(() => {
    generationRef.current = generation;
    cache.current.clear();
    for (const controller of loads.current.values()) controller.abort();
    loads.current.clear();
    setOutlineCache({});
    setPending(new Set());
    setErrors(new Set());
    setFullMatches([]);
    return () => {
      for (const controller of loads.current.values()) controller.abort();
      loads.current.clear();
    };
  }, [generation]);
  useEffect(() => {
    setQuery("");
    setSearchOpen(false);
    setMobileOpen(false);
    setExpanded(new Set());
    setFolders(new Set());
  }, [scopeKey]);
  useEffect(() => {
    if (!currentDocument || !visibleIds.has(currentDocument.revision_id))
      return;
    cacheArticle(currentDocument);
    setOutlineCache((previous) => ({
      ...previous,
      [currentDocument.revision_id]: headingsOnly(currentDocument),
    }));
  }, [currentDocument, generation, visibleIds]);
  useEffect(() => {
    if (!selected) return;
    setPackage(selected.package.id);
    setCategory(selected.category.id);
    setFolders(
      (previous) =>
        new Set([...previous, ...selected.folders.map((folder) => folder.id)]),
    );
    setMobileOpen(false);
  }, [selectedDocument, selected?.package.id, selected?.category.id]);
  useEffect(() => {
    if (hidden) return;
    const frame = requestAnimationFrame(() => {
      (
        root.current?.querySelector(".hb-toc [aria-current='location']") ||
        root.current?.querySelector(".hb-document-link[aria-current='page']")
      )?.scrollIntoView({ block: "nearest", inline: "nearest" });
      root.current
        ?.querySelector(".hb-categories [aria-current='page']")
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedDocument, selectedSection, category?.id, outlineCache, hidden]);
  useEffect(() => {
    if (hidden) setSearchOpen(false);
  }, [hidden]);
  useEffect(() => {
    if (!searchOpen) {
      if (dialog.current?.open) dialog.current.close();
      return;
    }
    dialog.current?.showModal();
    searchInput.current?.focus();
  }, [searchOpen]);
  useEffect(() => {
    if (hidden) return;
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape" && mobileOpen && !searchOpen) {
        setMobileOpen(false);
        root.current
          ?.querySelector<HTMLButtonElement>(".hb-mobile-toggle")
          ?.focus();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [hidden, mobileOpen, searchOpen]);
  const titleMatches = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return [];
    return uniqueDocuments
      .filter((entry) =>
        [
          entry.document.name,
          entry.document.path,
          entry.category.name,
          ...entry.folders.map((folder) => folder.name),
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(needle),
      )
      .map((entry) => ({
        document: entry.document.id,
        title: entry.document.name,
        snippet: entry.document.path,
      }));
  }, [query, uniqueDocuments]);
  useEffect(() => {
    setFullMatches([]);
    setScanned(0);
    setSearchError("");
    if (hidden || !searchOpen || query.trim().length < 2) {
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const expected = generation;
    const needle = query.trim();
    const timer = setTimeout(() => {
      setSearching(true);
      void (async () => {
        try {
          if (searcher.current) {
            const results = await searcher.current(needle, controller.signal);
            if (
              !controller.signal.aborted &&
              generationRef.current === expected
            )
              setFullMatches(
                results.filter((result) => visibleIds.has(result.document)),
              );
            return;
          }
          let completed = 0,
            failed = 0;
          const results: HandbookSearchResult[] = [];
          for (
            let index = 0;
            index < uniqueDocuments.length && !controller.signal.aborted;
            index += 4
          ) {
            const batch = uniqueDocuments.slice(index, index + 4);
            await Promise.all(
              batch.map(async (entry) => {
                try {
                  const id = entry.document.id;
                  const article =
                    cache.current.get(id) ||
                    (await loader.current(id, controller.signal));
                  if (
                    controller.signal.aborted ||
                    generationRef.current !== expected
                  )
                    return;
                  if (article.revision_id !== id)
                    throw new Error("文档版本不一致，请重新读取当前资料");
                  cacheArticle(article);
                  const content =
                    article.content ??
                    article.sections
                      .map((section) => section.content)
                      .join("\n\n");
                  const snippet = handbookSearchSnippet(content, needle);
                  if (snippet)
                    results.push({
                      document: id,
                      section: article.sections.find((section) =>
                        section.content
                          .toLocaleLowerCase()
                          .includes(needle.toLocaleLowerCase()),
                      )?.revision_id,
                      title: article.name,
                      snippet,
                    });
                } catch {
                  if (!controller.signal.aborted) failed++;
                } finally {
                  completed++;
                }
              }),
            );
            if (
              !controller.signal.aborted &&
              generationRef.current === expected
            ) {
              setFullMatches([...results]);
              setScanned(completed);
            }
          }
          if (failed && !controller.signal.aborted)
            setSearchError(`${failed} 篇正文未能读取，可重新搜索重试。`);
        } catch (error) {
          if (!controller.signal.aborted && generationRef.current === expected)
            setSearchError(
              error instanceof Error ? error.message : "全文检索未能完成",
            );
        } finally {
          if (!controller.signal.aborted && generationRef.current === expected)
            setSearching(false);
        }
      })();
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, searchOpen, generation, hidden, uniqueDocuments, visibleIds]);
  const matches: HandbookSearchResult[] = [
    ...new Map(
      [...titleMatches, ...fullMatches].map((result) => [
        result.document,
        result,
      ]),
    ).values(),
  ];
  const open = (document: string, section = "") => {
    onOpen(document, section);
    setSearchOpen(false);
    setMobileOpen(false);
  };
  const loadOutline = async (id: string) => {
    if (!visibleIds.has(id) || outlineCache[id] || loads.current.has(id))
      return;
    const controller = new AbortController(),
      expected = generation;
    loads.current.set(id, controller);
    setPending((previous) => new Set([...previous, id]));
    setErrors(
      (previous) => new Set([...previous].filter((value) => value !== id)),
    );
    try {
      const article =
        cache.current.get(id) || (await loader.current(id, controller.signal));
      if (article.revision_id !== id)
        throw new Error("文档版本不一致，请重新读取当前资料");
      if (
        !controller.signal.aborted &&
        generationRef.current === expected &&
        article.revision_id === id
      ) {
        cacheArticle(article);
        setOutlineCache((previous) => ({
          ...previous,
          [id]: headingsOnly(article),
        }));
      }
    } catch {
      if (!controller.signal.aborted && generationRef.current === expected)
        setErrors((previous) => new Set([...previous, id]));
    } finally {
      if (loads.current.get(id) === controller) loads.current.delete(id);
      if (!controller.signal.aborted && generationRef.current === expected)
        setPending(
          (previous) => new Set([...previous].filter((value) => value !== id)),
        );
    }
  };
  const toggle = (
    setter: (callback: (value: Set<string>) => Set<string>) => void,
    id: string,
  ) =>
    setter((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const outline = (
    headings: HandbookHeading[],
    document: string,
  ): ReactNode => (
    <ul className="hb-headings">
      {headings.map(({ section, children }) => (
        <li key={section.revision_id}>
          <button
            className={
              section.revision_id === selectedSection ? "selected" : ""
            }
            aria-current={
              section.revision_id === selectedSection ? "location" : undefined
            }
            onClick={() => open(document, section.revision_id)}
          >
            {section.name}
          </button>
          {children.length > 0 && outline(children, document)}
        </li>
      ))}
    </ul>
  );
  const nodes = (items: HandbookNode[]): ReactNode => (
    <ul className="hb-tree">
      {items.map((node) =>
        node.kind === "folder" ? (
          <li key={node.id}>
            <button
              className="hb-folder"
              aria-expanded={folders.has(node.id)}
              onClick={() => toggle(setFolders, node.id)}
            >
              <ChevronRight size={13} />
              <Folder size={14} />
              {node.name}
            </button>
            {folders.has(node.id) && nodes(node.children)}
          </li>
        ) : (
          <li
            key={node.id}
            className={node.id === selectedDocument ? "hb-current" : ""}
          >
            <div className="hb-document-row">
              <button
                className="hb-heading-toggle"
                aria-label={`${expanded.has(node.id) ? "收起" : "展开"}章节 ${node.name}`}
                aria-expanded={expanded.has(node.id)}
                onClick={() => {
                  toggle(setExpanded, node.id);
                  if (!expanded.has(node.id)) void loadOutline(node.id);
                }}
              >
                <ChevronRight size={13} />
              </button>
              <button
                className="hb-document-link"
                aria-current={node.id === selectedDocument ? "page" : undefined}
                onClick={() => open(node.id)}
              >
                <FileText size={13} />
                <span>{node.name}</span>
              </button>
            </div>
            {expanded.has(node.id) &&
              (pending.has(node.id) ? (
                <p className="hb-tree-status">
                  <LoaderCircle size={12} />
                  正在读取章节…
                </p>
              ) : errors.has(node.id) ? (
                <button
                  className="hb-tree-status"
                  onClick={() => void loadOutline(node.id)}
                >
                  章节未能读取，重试
                </button>
              ) : outlineCache[node.id] ? (
                outline(handbookOutline(outlineCache[node.id]), node.id)
              ) : null)}
          </li>
        ),
      )}
    </ul>
  );
  const overview = () => {
    if (onOverview) onOverview();
    else onOpen("");
  };
  const switchCategory = (entry: { pack: string; category: string }) => {
    setPackage(entry.pack);
    setCategory(entry.category);
    const first = entries.find(
      (item) =>
        item.package.id === entry.pack && item.category.id === entry.category,
    );
    if (first) open(first.document.id);
  };
  const counts = {
    documents: uniqueDocuments.length,
    categories: tree.packages.reduce(
      (count, item) => count + item.categories.length,
      0,
    ),
    packages: tree.packages.length,
  };
  return (
    <section
      ref={root}
      className={"vks-handbook " + (mobileOpen ? "hb-mobile-open" : "")}
      hidden={hidden}
      aria-label="知识手册阅读模板"
      data-template={handbookTemplateManifest.id}
      data-template-version={handbookTemplateManifest.version}
      data-readonly={readOnly}
      style={{ "--hb-accent": settings.accent } as CSSProperties}
    >
      <aside className="hb-sidebar" aria-label="当前分类目录">
        <button className="hb-brand" onClick={overview}>
          <span className="hb-brand-mark">{settings.brand}</span>
          <span>
            {settings.title}
            <small>{settings.subtitle}</small>
          </span>
        </button>
        <button
          className="hb-search-launch"
          aria-label="搜索全文"
          onClick={() => setSearchOpen(true)}
        >
          <Search size={14} />
          搜索全文<kbd>Ctrl K</kbd>
        </button>
        {tree.packages.length > 1 && (
          <label className="hb-package-picker">
            文档资料包
            <select
              aria-label="手册资料包"
              value={pack?.id || ""}
              onChange={(event) => {
                const next = tree.packages.find(
                  (item) => item.id === event.target.value,
                );
                if (next?.categories[0])
                  switchCategory({
                    pack: next.id,
                    category: next.categories[0].id,
                  });
              }}
            >
              {tree.packages.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="hb-category-name">
          <small>当前分类</small>
          <strong>{category?.name || "项目文档"}</strong>
          <span>
            {new Set(categoryEntries.map((entry) => entry.document.id)).size} 篇
          </span>
        </div>
        <div className="hb-toc-label">
          <span>文章目录</span>
          <button onClick={() => setExpanded(new Set())}>收起小节</button>
        </div>
        <nav className="hb-toc" aria-label="手册文章目录">
          {category ? (
            nodes(category.children)
          ) : (
            <p className="hb-tree-status">当前手册尚无文档</p>
          )}
        </nav>
        <div className="hb-sidebar-foot">
          <span className="hb-status-dot" />
          {readOnly ? "只读阅读" : "项目资料"}
          <span>{counts.documents} 篇</span>
        </div>
      </aside>
      {mobileOpen && (
        <button
          className="hb-sidebar-dismiss"
          aria-label="关闭手册目录"
          onClick={() => setMobileOpen(false)}
        />
      )}
      <div className="hb-workspace">
        <header className="hb-topbar">
          <button
            className="hb-mobile-toggle"
            aria-label="打开手册目录"
            aria-expanded={mobileOpen}
            onClick={() => setMobileOpen(!mobileOpen)}
          >
            <Menu size={17} />
          </button>
          <div className="hb-breadcrumb">
            {settings.title}
            <span>
              {" "}
              / {selected?.category.name || "阅读总览"}
              {selected ? " / " + selected.document.name : ""}
            </span>
          </div>
          <div className="hb-actions">
            <button
              onClick={onAskAI}
              aria-label="向手册 AI 提问"
              title="向 AI 提问"
            >
              <Sparkles size={16} />
            </button>
            <button onClick={overview}>总览</button>
            {!readOnly && (
              <button onClick={onUpdate}>
                <Upload size={13} />
                更新文档
              </button>
            )}
            <button onClick={() => (onPrint || (() => window.print()))()}>
              <Printer size={13} />
              打印 / PDF
            </button>
          </div>
        </header>
        <nav className="hb-categories" aria-label="手册文档分类">
          {pack?.categories.map((item) => (
            <button
              key={item.id}
              aria-current={item.id === category?.id ? "page" : undefined}
              className={item.id === category?.id ? "active" : ""}
              onClick={() =>
                switchCategory({ pack: pack.id, category: item.id })
              }
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
        </nav>
        <main className="hb-main" aria-label="手册正文">
          {selectedDocument && visibleIds.has(selectedDocument) ? (
            <>
              <div className="hb-article-path">{selected?.document.path}</div>
              <div className="hb-article">{renderReader}</div>
              <nav className="hb-pagination" aria-label="文章翻页">
                {neighbors.previous ? (
                  <button onClick={() => open(neighbors.previous!.document.id)}>
                    <small>
                      <ArrowLeft size={12} />
                      上一篇
                    </small>
                    <strong>{neighbors.previous.document.name}</strong>
                  </button>
                ) : (
                  <span />
                )}
                {neighbors.next && (
                  <button onClick={() => open(neighbors.next!.document.id)}>
                    <small>
                      下一篇
                      <ArrowRight size={12} />
                    </small>
                    <strong>{neighbors.next.document.name}</strong>
                  </button>
                )}
              </nav>
            </>
          ) : (
            <section className="hb-cover" aria-label="手册阅读总览">
              <div className="hb-eyebrow">
                <span />
                {settings.subtitle}
              </div>
              <h1>
                {settings.title}
                <em>项目知识手册</em>
              </h1>
              <p className="hb-cover-lead">{settings.introduction}</p>
              <div className="hb-stats">
                <div>
                  <strong>{counts.documents}</strong>
                  <span>篇完整文档</span>
                </div>
                <div>
                  <strong>{counts.categories}</strong>
                  <span>个阅读分类</span>
                </div>
                <div>
                  <strong>{counts.packages}</strong>
                  <span>个资料包</span>
                </div>
              </div>
              {counts.documents ? (
                <>
                  <div className="hb-reading-note">
                    <strong>阅读方式</strong>
                    <p>
                      顶部切换分类，在左侧选择文章。展开文章小节可直接定位；全文搜索和引用会跳转到准确文档。图片、表格与图表由项目共用阅读器呈现。
                    </p>
                  </div>
                  <button
                    className="hb-start-reading"
                    onClick={() => open(uniqueDocuments[0].document.id)}
                  >
                    开始阅读
                    <ArrowRight size={18} />
                  </button>
                </>
              ) : (
                <div className="hb-reading-note">
                  <BookOpen size={22} />
                  <strong>从第一篇文档开始</strong>
                  <p>
                    {readOnly
                      ? "这个手册尚未包含文档。"
                      : "导入 Markdown、HTML 或文档包，按原有目录组织项目知识。"}
                  </p>
                  {!readOnly && (
                    <button onClick={onUpdate}>
                      <Upload size={14} />
                      导入文档
                    </button>
                  )}
                </div>
              )}
            </section>
          )}
          <footer className="hb-book-footer">
            {settings.title} · {counts.documents} 篇文档
            <br />
            <span>
              知识手册模板 {handbookTemplateManifest.version} ·{" "}
              {readOnly ? "只读成品" : "项目独立管理"}
            </span>
          </footer>
        </main>
      </div>
      <dialog
        ref={dialog}
        className="hb-search-dialog"
        aria-label="手册全文搜索"
        onCancel={() => setSearchOpen(false)}
        onClose={() => setSearchOpen(false)}
      >
        <div className="hb-search-header">
          <label htmlFor={inputId}>
            <Search size={17} />
            搜索全文
          </label>
          <button
            aria-label="关闭全文搜索"
            onClick={() => setSearchOpen(false)}
          >
            <X size={17} />
          </button>
        </div>
        <input
          ref={searchInput}
          id={inputId}
          aria-label="手册搜索内容"
          value={query}
          placeholder="输入标题、路径或正文关键词…"
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="hb-search-status" role="status">
          {searching ? (
            <>
              <LoaderCircle size={13} />
              正在检索正文
              {searcher.current ? "" : ` ${scanned} / ${counts.documents}`}…
            </>
          ) : query.trim() ? (
            `${matches.length} 篇匹配文档`
          ) : (
            "输入至少两个字符检索正文，标题和路径即时匹配。"
          )}
        </div>
        {searchError && (
          <p className="hb-search-error" role="alert">
            {searchError}
          </p>
        )}
        <div className="hb-search-results">
          {matches.map((result) => (
            <button
              key={result.document}
              onClick={() => open(result.document, result.section)}
            >
              <strong>{result.title}</strong>
              <span>{result.snippet}</span>
            </button>
          ))}
        </div>
      </dialog>
    </section>
  );
}
