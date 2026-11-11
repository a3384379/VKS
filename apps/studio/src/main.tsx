import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BookOpen,
  ChevronRight,
  FileText,
  Link2,
  MousePointer2,
  PanelRight,
  Pin,
  Search,
  X,
  Check,
  ArrowUpRight,
  GripHorizontal,
  Minus,
  Plus,
  LogOut,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  MarkdownAttachment,
  attachmentUrl,
} from "../../web/src/MarkdownAttachment";
import { api, mutation, setCsrf } from "../../web/src/api";
import { OriginalRuntime, isTarget } from "./runtime";
import type { Target, RuntimeEvent, RuntimeHandle } from "./runtime";
import "./style.css";

type DocRef = { id: string; revision_id: string; name: string; path: string };
type Chapter = {
  id: string;
  revision_id: string;
  name: string;
  content: string;
};
type Document = DocRef & { sections: Chapter[] };
type Citation = {
  revision_id: string;
  name: string;
  content: string;
  document_revision_id: string;
  document_name: string;
};
type Binding = {
  id: string;
  binding_version: number;
  point_revision_id: string;
  title: string;
  summary: string;
  package_id: string;
  target: Target;
  sections: Citation[];
  excluded: boolean;
  validity_status: string;
};
type Catalog = {
  analysis_set_id: string;
  release_id: string | null;
  selection_version: number;
  editable: boolean;
  packages: { id: string; kind: string; name: string; sha256: string }[];
  documents: DocRef[];
};
type Route = {
  project: string;
  analysis: string;
  release: string;
  view: string;
  doc: string;
  section: string;
  binding: string;
};
const getRoute = (): Route =>
  Object.fromEntries(
    ["project", "analysis", "release", "view", "doc", "section", "binding"].map(
      (k) => [k, new URLSearchParams(location.hash.slice(1)).get(k) || ""],
    ),
  ) as Route;
const sameTarget = (a: Target, b: Target) =>
  a.route === b.route &&
  a.selector === b.selector &&
  a.label === b.label &&
  a.state === b.state &&
  JSON.stringify(a.frame_path) === JSON.stringify(b.frame_path);
const text = (e: unknown) => (e instanceof Error ? e.message : String(e));
function Markdown({
  children,
  documentId,
  revisionId,
}: {
  children: string;
  documentId?: string;
  revisionId?: string;
}) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: ({ src, alt }) => {
            const source = attachmentUrl(
              documentId,
              revisionId,
              typeof src === "string" ? src : undefined,
            );
            return source ? (
              <MarkdownAttachment source={source} alt={alt || "文档图片"} />
            ) : (
              <span>{alt || "文档图片"}（可在原手册中查看）</span>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function App() {
  const [auth, setAuth] = useState<boolean | null>(null),
    [username, setUsername] = useState("owner"),
    [password, setPassword] = useState("");
  const [route, setRoute] = useState(getRoute),
    [catalog, setCatalog] = useState<Catalog | null>(null),
    [bindings, setBindings] = useState<Binding[]>([]),
    [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [viewDoc, setViewDoc] = useState<Document | null>(null),
    [docQuery, setDocQuery] = useState(""),
    [originalBook, setOriginalBook] = useState(false);
  const [bookMounted, setBookMounted] = useState(false);
  const [target, setTarget] = useState<Target | null>(null),
    [active, setActive] = useState<Binding | null>(null),
    [knowledge, setKnowledge] = useState(true),
    [editing, setEditing] = useState(false),
    [picking, setPicking] = useState(false),
    [saving, setSaving] = useState(false),
    [pinned, setPinned] = useState(false),
    [docked, setDocked] = useState(false),
    [folded, setFolded] = useState(false);
  const [title, setTitle] = useState(""),
    [summary, setSummary] = useState(""),
    [editDoc, setEditDoc] = useState<Document | null>(null),
    [editDocId, setEditDocId] = useState(""),
    [sectionId, setSectionId] = useState(""),
    [popupQuery, setPopupQuery] = useState("");
  const [position, setPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const prototype = useRef<RuntimeHandle | null>(null),
    handbook = useRef<RuntimeHandle | null>(null),
    windowRef = useRef<HTMLElement>(null),
    pendingReveal = useRef<Target | null>(null),
    docEpoch = useRef(0),
    editEpoch = useRef(0),
    loadEpoch = useRef(0);
  const prefix = `/api/v3/projects/${route.project}/interactive`,
    scope = route.release
      ? `release_id=${encodeURIComponent(route.release)}`
      : `analysis_set_id=${encodeURIComponent(route.analysis)}`;
  const navigate = useCallback((patch: Partial<Route>, replace = false) => {
    const next = { ...getRoute(), ...patch };
    const p = new URLSearchParams(Object.entries(next).filter(([, v]) => v));
    history[replace ? "replaceState" : "pushState"]({}, "", `#${p}`);
    setRoute(next);
  }, []);
  useEffect(() => {
    const update = () => setRoute(getRoute());
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("hashchange", update);
    };
  }, []);
  useEffect(() => {
    api<{ authenticated: boolean; csrf_token?: string }>("/auth/session")
      .then((s) => {
        setCsrf(s.csrf_token);
        setAuth(s.authenticated);
      })
      .catch(() => setAuth(false));
  }, []);
  const reload = useCallback(async () => {
    const epoch = ++loadEpoch.current;
    const [c, b] = await Promise.all([
      api<Catalog>(`${prefix}/catalog?${scope}`),
      api<Binding[]>(`${prefix}/bindings?${scope}`),
    ]);
    if (epoch !== loadEpoch.current) return;
    setCatalog(c);
    setBindings(b);
    return { c, b };
  }, [prefix, scope]);
  useEffect(() => {
    if (!auth) return;
    let cancelled = false;
    api<{ id: string; name: string }[]>("/projects")
      .then(async (p) => {
        if (cancelled) return;
        setProjects(p);
        if (!route.project && p[0]) navigate({ project: p[0].id }, true);
        else if (route.project && !route.analysis && !route.release) {
          const w = await api<{ selected_analysis_set: { id: string } }>(
            "/projects/" + route.project + "/workspace",
          );
          if (!cancelled)
            navigate(
              { analysis: w.selected_analysis_set.id, view: "prototype" },
              true,
            );
        }
      })
      .catch((e) => setError(text(e)));
    return () => {
      cancelled = true;
    };
  }, [auth, route.project, route.analysis, route.release, navigate]);
  useEffect(() => {
    if (!auth || !route.project || (!route.analysis && !route.release)) return;
    setCatalog(null);
    setBindings([]);
    setActive(null);
    setTarget(null);
    setEditing(false);
    setViewDoc(null);
    setBookMounted(false);
    setOriginalBook(false);
    setError("");
    void reload().catch((e) => setError(text(e)));
    return () => {
      loadEpoch.current++;
    };
  }, [auth, route.project, route.analysis, route.release, reload]);
  useEffect(() => {
    if (!route.binding) return;
    const b = bindings.find((b) => b.id === route.binding);
    if (b) {
      setActive(b);
      setTarget(b.target);
      setKnowledge(true);
    }
  }, [route.binding, bindings]);
  useEffect(() => {
    if (!catalog || !route.doc) return;
    const epoch = ++docEpoch.current;
    api<Document>(`${prefix}/documents/${route.doc}?${scope}`)
      .then((d) => {
        if (epoch === docEpoch.current) setViewDoc(d);
      })
      .catch((e) => setError(text(e)));
    return () => {
      docEpoch.current++;
    };
  }, [catalog?.analysis_set_id, route.doc, prefix, scope]);
  useEffect(() => {
    if (viewDoc && route.section && route.view === "documents")
      requestAnimationFrame(() =>
        document
          .getElementById("section-" + route.section)
          ?.scrollIntoView({ block: "start", behavior: "instant" }),
      );
  }, [viewDoc, route.section, route.view]);
  useEffect(() => {
    if (!editDocId) {
      setEditDoc(null);
      return;
    }
    const epoch = ++editEpoch.current;
    api<Document>(`${prefix}/documents/${editDocId}?${scope}`)
      .then((d) => {
        if (epoch === editEpoch.current) setEditDoc(d);
      })
      .catch((e) => setError(text(e)));
    return () => {
      editEpoch.current++;
    };
  }, [editDocId, prefix, scope]);
  useEffect(() => {
    const clamp = () =>
      setPosition((p) =>
        p
          ? {
              left: Math.max(
                8,
                Math.min(
                  p.left,
                  innerWidth - (windowRef.current?.offsetWidth || 420) - 8,
                ),
              ),
              top: Math.max(
                100,
                Math.min(
                  p.top,
                  innerHeight - (windowRef.current?.offsetHeight || 560) - 8,
                ),
              ),
            }
          : null,
      );
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, []);
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const s = await api<{ csrf_token: string }>(
        "/auth/login",
        mutation("POST", { username, password }),
      );
      setCsrf(s.csrf_token);
      setPassword("");
      setAuth(true);
    } catch (e) {
      setError(text(e));
    }
  }
  function onRuntime(event: RuntimeEvent) {
    if (event.type === "context.changed" && pendingReveal.current) {
      prototype.current?.send("target.reveal", {
        target: pendingReveal.current,
      });
      pendingReveal.current = null;
    }
    if (event.type === "picker.cancelled") setPicking(false);
    if (event.type === "command.result") {
      const result = event.payload as { message: string };
      setNotice(result.message);
    }
    if (event.type !== "target.selected" && event.type !== "action.activated")
      return;
    const t = event.payload;
    if (!isTarget(t)) return;
    if (event.type === "target.selected") {
      setPicking(false);
      setTarget(t);
      const b = bindings.find((b) => sameTarget(b.target, t));
      setActive(b || null);
      setTitle(b?.title || t.label);
      setSummary(b?.summary || "");
      setEditDocId(b?.sections[0]?.document_revision_id || "");
      setSectionId(b?.sections[0]?.revision_id || "");
      setEditing(true);
      setKnowledge(true);
      setFolded(false);
      setNotice("已选中控件，请选择文档章节并保存说明");
    } else if (!pinned && !editing) {
      setTarget(t);
      setActive(
        bindings.find((b) => sameTarget(b.target, t) && !b.excluded) || null,
      );
    }
  }
  function startPicker() {
    if (editing) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    setPicking(true);
    prototype.current?.send("picker.start");
    navigate({ view: "prototype" });
    setNotice("请点击需要关联的原型控件；按 Esc 或点击取消结束");
  }
  function openDoc(c: Citation, b: Binding) {
    setOriginalBook(false);
    navigate({
      view: "documents",
      doc: c.document_revision_id,
      section: c.revision_id,
      binding: b.id,
    });
    setKnowledge(false);
  }
  function reveal(b: Binding) {
    setEditing(false);
    setKnowledge(true);
    setActive(b);
    setTarget(b.target);
    navigate({ view: "prototype", binding: b.id });
    if (prototype.current)
      prototype.current.send("target.reveal", { target: b.target });
    else pendingReveal.current = b.target;
  }
  function editBinding(b: Binding) {
    setActive(b);
    setTarget(b.target);
    setTitle(b.title);
    setSummary(b.summary);
    setEditDocId(b.sections[0]?.document_revision_id || "");
    setSectionId(b.sections[0]?.revision_id || "");
    setEditing(true);
    setKnowledge(true);
    setFolded(false);
  }
  async function save() {
    if (!target || !catalog || !sectionId || !summary.trim() || !title.trim())
      return;
    setSaving(true);
    setError("");
    try {
      const p = catalog.packages.find((p) => p.kind === "prototype");
      if (!p) throw new Error("请先导入原型包");
      const result = await api<{ binding_id: string }>(
        `${prefix}/bindings`,
        mutation(
          "POST",
          {
            analysis_set_id: route.analysis,
            package_id: p.id,
            target,
            point: { title, summary },
            section_revision_ids: [sectionId],
            ...(active
              ? {
                  binding_id: active.id,
                  binding_version: active.binding_version,
                  point_revision_id: active.point_revision_id,
                }
              : {}),
          },
          catalog.selection_version,
        ),
      );
      const updated = await reload();
      const b = updated?.b.find((b) => b.id === result.binding_id);
      setActive(b || null);
      setEditing(false);
      setNotice("关联已保存，刷新后仍可查看");
      navigate({ binding: result.binding_id }, true);
    } catch (e) {
      setError(text(e));
      void reload();
    } finally {
      setSaving(false);
    }
  }
  async function exclude(b: Binding) {
    if (!catalog) return;
    try {
      await api(
        `${prefix}/bindings/${b.id}/exclude`,
        mutation(
          "POST",
          {
            analysis_set_id: route.analysis,
            binding_version: b.binding_version,
            excluded: !b.excluded,
          },
          catalog.selection_version,
        ),
      );
      await reload();
      setNotice(b.excluded ? "关联已恢复" : "关联已移除");
    } catch (e) {
      setError(text(e));
    }
  }
  function drag(e: React.PointerEvent) {
    if ((e.target as HTMLElement).closest("button") || docked) return;
    const rect = windowRef.current!.getBoundingClientRect();
    const x = e.clientX,
      y = e.clientY;
    e.currentTarget.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) =>
      setPosition({
        left: Math.max(
          8,
          Math.min(innerWidth - rect.width - 8, rect.left + ev.clientX - x),
        ),
        top: Math.max(
          100,
          Math.min(innerHeight - rect.height - 8, rect.top + ev.clientY - y),
        ),
      });
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
  }
  if (auth === null) return <div className="splash">正在打开知境 Studio…</div>;
  if (!auth)
    return (
      <main className="login">
        <form onSubmit={login}>
          <span className="brand-mark">
            <BookOpen size={25} />
          </span>
          <h1>知境 Studio</h1>
          <p>让知识回到业务操作发生的地方</p>
          <label>
            登录名
            <input
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label>
            密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <button className="primary" type="submit">
            打开交互工作台 <ArrowUpRight size={16} />
          </button>
          <small>VKS 3.0 · 原始交互与知识关联预览</small>
        </form>
      </main>
    );
  const p = catalog?.packages.find((p) => p.kind === "prototype"),
    h = catalog?.packages.find((p) => p.kind === "handbook");
  const currentView = route.view || "prototype";
  const selectedSection = editDoc?.sections.find(
    (s) => s.revision_id === sectionId,
  );
  const related = bindings.filter(
    (b) =>
      !b.excluded &&
      b.sections.some(
        (c) =>
          c.document_revision_id === route.doc &&
          (!route.section || c.revision_id === route.section),
      ),
  );
  return (
    <div className="studio">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            <BookOpen size={21} />
          </span>
          <b>
            知境 <span>Studio</span>
          </b>
          <span className="version">3.0</span>
        </div>
        <span className="top-divider" />
        <select
          aria-label="当前项目"
          disabled={editing}
          value={route.project}
          onChange={(e) =>
            navigate({
              project: e.target.value,
              analysis: "",
              release: "",
              doc: "",
              section: "",
              binding: "",
            })
          }
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <nav>
          {[
            ["prototype", "原型"],
            ["documents", "文档"],
            ["links", "关联管理"],
          ].map(([v, l]) => (
            <button
              key={v}
              className={currentView === v ? "selected" : ""}
              onClick={() => {
                if (editing) {
                  setNotice("请先保存或取消当前编辑");
                  return;
                }
                navigate({ view: v });
                if (v === "documents" && !route.doc && catalog?.documents[0])
                  navigate({ view: v, doc: catalog.documents[0].revision_id });
              }}
            >
              {l}
            </button>
          ))}
        </nav>
        <div className="top-actions">
          <span className="draft">
            <i />
            {route.release ? "知识快照" : "当前草稿"}
          </span>
          <button
            className="quiet"
            onClick={() => {
              setKnowledge(!knowledge);
              setFolded(false);
            }}
          >
            <BookOpen size={16} />
            知识小窗
          </button>
          <button
            aria-label="退出登录"
            className="icon"
            onClick={() =>
              void api("/auth/session", mutation("DELETE", {})).finally(() => {
                setAuth(false);
                setCatalog(null);
              })
            }
          >
            <LogOut size={16} />
          </button>
        </div>
      </header>
      <div className="contextbar">
        <div>
          <span className="context-badge">
            {currentView === "prototype"
              ? "原始交互"
              : currentView === "documents"
                ? "知识文档"
                : "关联管理"}
          </span>
          <ChevronRight size={13} />
          <span>
            {currentView === "prototype"
              ? target?.label || "在原页面操作，即可查看相关知识"
              : currentView === "documents"
                ? viewDoc?.name || "独立阅读与章节关联"
                : `${bindings.filter((b) => !b.excluded).length} 条有效关联`}
          </span>
        </div>
        <div>
          {currentView === "prototype" &&
            (picking ? (
              <button
                className="picker-active"
                onClick={() => {
                  prototype.current?.send("picker.cancel");
                  setPicking(false);
                }}
              >
                <X size={14} />
                取消选择
              </button>
            ) : (
              <button disabled={!catalog?.editable || !p} onClick={startPicker}>
                <MousePointer2 size={14} />
                关联知识
              </button>
            ))}
          {currentView === "documents" && (
            <button
              onClick={() => {
                setOriginalBook(!originalBook);
                setBookMounted(true);
              }}
            >
              <FileText size={14} />
              {originalBook ? "工作区文档" : "原手册"}
            </button>
          )}
          <span className="subtle">
            {catalog ? `${catalog.documents.length} 篇文档` : "正在加载…"}
          </span>
        </div>
      </div>
      {error && (
        <div className="banner error" role="alert">
          {error}
          <button onClick={() => setError("")} aria-label="关闭错误">
            <X size={14} />
          </button>
        </div>
      )}
      {notice && (
        <div className="toast" role="status">
          <Check size={15} />
          {notice}
          <button onClick={() => setNotice("")} aria-label="关闭提示">
            <X size={13} />
          </button>
        </div>
      )}
      <main className={"workspace " + (knowledge && docked ? "with-dock" : "")}>
        <section
          className="prototype-space"
          hidden={currentView !== "prototype"}
        >
          {p ? (
            <OriginalRuntime
              key={p.id + scope}
              url={`${prefix}/packages/${p.id}/content?${scope}`}
              digest={p.sha256}
              kind="prototype"
              handle={prototype}
              onEvent={onRuntime}
            />
          ) : (
            <div className="empty">
              <BookOpen />
              <h2>
                {catalog ? "当前版本没有可运行的原型包" : "正在打开项目…"}
              </h2>
              <p>原型通过现有资料导入入口导入后，在这里完整交互。</p>
            </div>
          )}
        </section>
        {currentView === "documents" && (
          <section className="documents-space" hidden={originalBook}>
            <aside className="doc-sidebar">
              <div className="sidebar-title">
                <FileText size={17} />
                <b>项目知识库</b>
                <span>{catalog?.documents.length}</span>
              </div>
              <div className="search">
                <Search size={15} />
                <input
                  aria-label="搜索文档"
                  placeholder="搜索文档标题…"
                  value={docQuery}
                  onChange={(e) => setDocQuery(e.target.value)}
                />
              </div>
              <div className="doc-list">
                {catalog?.documents
                  .filter((d) =>
                    d.name.toLowerCase().includes(docQuery.toLowerCase()),
                  )
                  .map((d) => (
                    <button
                      className={d.revision_id === route.doc ? "active" : ""}
                      key={d.id}
                      onClick={() =>
                        navigate({
                          doc: d.revision_id,
                          section: "",
                          view: "documents",
                        })
                      }
                    >
                      <FileText size={14} />
                      <span>{d.name}</span>
                      {bindings.some(
                        (b) =>
                          !b.excluded &&
                          b.sections.some(
                            (s) => s.document_revision_id === d.revision_id,
                          ),
                      ) && <i className="link-dot" />}
                    </button>
                  ))}
              </div>
            </aside>
            <article className="document-reader">
              <div className="reader-label">
                工作区文档 <span>· 准确修订</span>
              </div>
              <h1>{viewDoc?.name || "选择一篇文档开始阅读"}</h1>
              {viewDoc && (
                <div className="article-toc">
                  <b>本篇目录</b>
                  {viewDoc.sections.map((s) => (
                    <a
                      key={s.id}
                      href={`#section-${s.revision_id}`}
                      onClick={(e) => {
                        e.preventDefault();
                        navigate({ section: s.revision_id });
                      }}
                    >
                      {s.name}
                    </a>
                  ))}
                </div>
              )}
              {viewDoc?.sections.map((s) => (
                <section
                  className={
                    "chapter " +
                    (route.section === s.revision_id ? "chapter-selected" : "")
                  }
                  id={"section-" + s.revision_id}
                  key={s.id}
                >
                  <div className="chapter-actions">
                    <button
                      onClick={() => {
                        navigate({ section: s.revision_id });
                        setKnowledge(true);
                        setActive(
                          bindings.find(
                            (b) =>
                              !b.excluded &&
                              b.sections.some(
                                (c) => c.revision_id === s.revision_id,
                              ),
                          ) || null,
                        );
                      }}
                    >
                      <Link2 size={13} />
                      关联操作
                    </button>
                  </div>
                  <Markdown
                    documentId={viewDoc?.id}
                    revisionId={viewDoc?.revision_id}
                  >
                    {s.content}
                  </Markdown>
                  {bindings
                    .filter(
                      (b) =>
                        !b.excluded &&
                        b.sections.some((c) => c.revision_id === s.revision_id),
                    )
                    .map((b) => (
                      <button
                        key={b.id}
                        className="reverse-link"
                        onClick={() => reveal(b)}
                      >
                        <MousePointer2 size={15} />
                        打开原型 · {b.target.label}
                        <ArrowUpRight size={14} />
                      </button>
                    ))}
                </section>
              ))}
            </article>
            <aside className="related-sidebar">
              <span className="eyebrow">与当前文章相关</span>
              <h3>关联业务操作</h3>
              {related.length ? (
                related.map((b) => (
                  <button key={b.id} onClick={() => reveal(b)}>
                    <MousePointer2 size={17} />
                    <span>
                      <b>{b.title}</b>
                      <small>{b.target.label}</small>
                    </span>
                    <ArrowUpRight size={15} />
                  </button>
                ))
              ) : (
                <p>选中原型控件并保存关联后，可在这里跳回对应操作。</p>
              )}
            </aside>
          </section>
        )}
        {h && bookMounted && (
          <section
            className="handbook-space"
            hidden={currentView !== "documents" || !originalBook}
          >
            <OriginalRuntime
              key={h.id + scope}
              url={`${prefix}/packages/${h.id}/content?${scope}`}
              digest={h.sha256}
              kind="handbook"
              handle={handbook}
              onEvent={() => {}}
            />
          </section>
        )}
        {currentView === "links" && (
          <section className="links-space">
            <span className="eyebrow">知识与业务操作</span>
            <h1>关联管理</h1>
            <p>
              维护原型中的位置、业务说明与准确章节。点击操作即可返回原页面。
            </p>
            <div className="binding-grid">
              {bindings.map((b) => (
                <article key={b.id} className={b.excluded ? "excluded" : ""}>
                  <div className="card-label">
                    <Link2 size={15} />
                    {b.excluded ? "已移除" : "已关联"}
                    <small>{b.sections.length} 个章节</small>
                  </div>
                  <h3>{b.title}</h3>
                  <p>{b.summary}</p>
                  {b.sections.map((c) => (
                    <button
                      className="citation"
                      key={c.revision_id}
                      onClick={() => openDoc(c, b)}
                    >
                      <FileText size={15} />
                      <span>
                        {c.document_name}
                        <small>{c.name}</small>
                      </span>
                      <ArrowUpRight size={14} />
                    </button>
                  ))}
                  <footer>
                    <button onClick={() => reveal(b)}>打开原型</button>
                    <button
                      disabled={!catalog?.editable}
                      onClick={() => editBinding(b)}
                    >
                      编辑
                    </button>
                    <button
                      disabled={!catalog?.editable}
                      onClick={() => void exclude(b)}
                    >
                      {b.excluded ? "恢复关联" : "移除关联"}
                    </button>
                  </footer>
                </article>
              ))}
            </div>
            {!bindings.length && (
              <div className="empty">
                <Link2 />
                <h3>从原型开始第一条关联</h3>
                <button className="primary" onClick={startPicker}>
                  选择控件并关联知识
                </button>
              </div>
            )}
          </section>
        )}
      </main>
      {knowledge && (
        <aside
          ref={windowRef}
          aria-label="知识小窗"
          className={
            "knowledge-window " +
            (docked ? "docked" : "") +
            (folded ? " folded" : "")
          }
          style={
            !docked && position
              ? {
                  left: position.left,
                  top: position.top,
                  right: "auto",
                  bottom: "auto",
                }
              : {}
          }
          onKeyDown={(e) => {
            if (e.key === "Escape" && !editing) {
              setKnowledge(false);
            }
          }}
        >
          <div className="knowledge-header" onPointerDown={drag}>
            <div>
              <BookOpen size={17} />
              <b>{editing ? "关联知识" : "知识小窗"}</b>
              <GripHorizontal size={14} />
            </div>
            <div>
              <button
                className={"icon " + (pinned ? "enabled" : "")}
                aria-label="固定当前知识"
                onClick={() => setPinned(!pinned)}
              >
                <Pin size={14} />
              </button>
              <button
                className="icon"
                aria-label={docked ? "浮动小窗" : "固定到侧边"}
                onClick={() => {
                  setDocked(!docked);
                  setPosition(null);
                }}
              >
                <PanelRight size={15} />
              </button>
              <button
                className="icon"
                aria-label="收起小窗"
                onClick={() => setFolded(!folded)}
              >
                {folded ? <Plus size={15} /> : <Minus size={15} />}
              </button>
              <button
                className="icon"
                aria-label="关闭知识小窗"
                onClick={() => {
                  if (editing) {
                    setNotice("请先保存或取消当前编辑");
                    return;
                  }
                  setKnowledge(false);
                }}
              >
                <X size={15} />
              </button>
            </div>
          </div>
          {!folded && (
            <>
              <div className="knowledge-context">
                <MousePointer2 size={14} />
                <span>{target?.label || "请选择一个原型操作"}</span>
                <span className="pill">
                  {editing ? "维护关联" : "当前上下文"}
                </span>
              </div>
              <div className="knowledge-body">
                {editing ? (
                  <>
                    <label>
                      知识标题
                      <input
                        aria-label="知识标题"
                        value={title}
                        maxLength={160}
                        onChange={(e) => setTitle(e.target.value)}
                      />
                    </label>
                    <label>
                      业务说明
                      <textarea
                        aria-label="业务说明"
                        placeholder="描述业务规则、适用条件和操作结果…"
                        value={summary}
                        maxLength={8000}
                        onChange={(e) => setSummary(e.target.value)}
                        rows={4}
                      />
                    </label>
                    <label>
                      关联文档
                      <div className="search">
                        <Search size={14} />
                        <input
                          aria-label="搜索关联文档"
                          placeholder="搜索文档…"
                          value={popupQuery}
                          onChange={(e) => setPopupQuery(e.target.value)}
                        />
                      </div>
                      <select
                        aria-label="关联文档"
                        value={editDocId}
                        onChange={(e) => {
                          setEditDocId(e.target.value);
                          setSectionId("");
                        }}
                      >
                        <option value="">请选择文档</option>
                        {catalog?.documents
                          .filter(
                            (d) =>
                              d.name.includes(popupQuery) ||
                              d.revision_id === editDocId,
                          )
                          .map((d) => (
                            <option key={d.id} value={d.revision_id}>
                              {d.name}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      准确章节
                      <select
                        aria-label="关联章节"
                        value={sectionId}
                        onChange={(e) => setSectionId(e.target.value)}
                      >
                        <option value="">请选择章节</option>
                        {editDoc?.sections.map((s) => (
                          <option key={s.id} value={s.revision_id}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {selectedSection && (
                      <div className="section-preview">
                        <span className="eyebrow">原文预览</span>
                        <Markdown
                          documentId={editDoc?.id}
                          revisionId={editDoc?.revision_id}
                        >
                          {selectedSection.content}
                        </Markdown>
                      </div>
                    )}
                  </>
                ) : active && !active.excluded ? (
                  <>
                    <div className="knowledge-title">
                      <span className="eyebrow">业务知识</span>
                      <h2>{active.title}</h2>
                    </div>
                    <Markdown>{active.summary}</Markdown>
                    <h4 className="source-label">
                      原文引用 <span>{active.sections.length}</span>
                    </h4>
                    {active.sections.map((c) => (
                      <button
                        key={c.revision_id}
                        className="citation"
                        onClick={() => openDoc(c, active)}
                      >
                        <FileText size={18} />
                        <span>
                          {c.document_name}
                          <small>{c.name}</small>
                        </span>
                        <ArrowUpRight size={15} />
                      </button>
                    ))}
                    <details className="inline-preview">
                      <summary>在小窗查看原文</summary>
                      {active.sections.map((c) => (
                        <Markdown
                          key={c.revision_id}
                          documentId={
                            catalog?.documents.find(
                              (d) => d.revision_id === c.document_revision_id,
                            )?.id
                          }
                          revisionId={c.document_revision_id}
                        >
                          {c.content}
                        </Markdown>
                      ))}
                    </details>
                  </>
                ) : (
                  <div className="knowledge-empty">
                    <div>
                      <Link2 size={27} />
                    </div>
                    <h3>
                      {target ? "这个操作还没有关联知识" : "边操作，边查看知识"}
                    </h3>
                    <p>
                      {target
                        ? "点击“关联知识”，选择控件和文档章节，保存你的业务说明。"
                        : "点击原页面中的按钮，已有的业务说明与引用会显示在这里。"}
                    </p>
                    <button disabled={!catalog?.editable} onClick={startPicker}>
                      <Plus size={15} />
                      建立知识关联
                    </button>
                  </div>
                )}
              </div>
              <footer className="knowledge-footer">
                {editing ? (
                  <>
                    <span>保存到当前草稿</span>
                    <button disabled={saving} onClick={() => setEditing(false)}>
                      取消
                    </button>
                    <button
                      className="primary"
                      disabled={
                        saving || !summary.trim() || !title.trim() || !sectionId
                      }
                      onClick={() => void save()}
                    >
                      {saving ? "保存中…" : "保存关联"}
                    </button>
                  </>
                ) : (
                  <>
                    <span>
                      <i className="status-dot" />
                      {active ? "关联已保存" : "跟随原型操作"}
                    </span>
                    {active && (
                      <button
                        disabled={!catalog?.editable}
                        onClick={() => editBinding(active)}
                      >
                        编辑知识
                      </button>
                    )}
                    <button disabled={!catalog?.editable} onClick={startPicker}>
                      <Link2 size={14} />
                      关联知识
                    </button>
                  </>
                )}
              </footer>
            </>
          )}
        </aside>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
