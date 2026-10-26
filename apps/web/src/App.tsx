import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Aperture,
  ArrowRight,
  BookOpen,
  Boxes,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Command,
  FileText,
  GitCompareArrows,
  House,
  Layers3,
  Link2,
  LogOut,
  Network,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import { api, apiPage, ApiError, mutation, setCsrf } from "./api";
import {
  Button,
  Empty,
  Modal,
  Notice,
  Panel,
  Spinner,
  Tag,
  entityIcon,
  label,
} from "./components";
import { WorkspaceContext } from "./context";
import {
  emptyNavigation,
  resolveExplanation,
  type ExplanationNavigation,
  type NavigationIntent,
} from "./navigation";
import { hasProjectPermission } from "./permissions";
import { LoginForm } from "./LoginForm";
import { RenderBoundary } from "./RenderBoundary";
import { flattenModules, moduleBaseline, type ModuleNode } from "./ModuleTree";
import {
  Overview,
  Imports,
  Workbench,
  Documents,
  Reviews,
  Graph,
  Versions,
  Settings,
} from "./pages";
import type { Entity, Project, Session, View, Workspace } from "./types";
const nav: {
  id: View;
  title: string;
  icon: typeof House;
  description: string;
}[] = [
  {
    id: "overview",
    title: "项目总览",
    icon: House,
    description: "从业务操作出发，让每一份项目知识都有依据。",
  },
  {
    id: "imports",
    title: "接入与任务",
    icon: Upload,
    description: "保留资料的原始版本，让知识从可靠来源开始。",
  },
  {
    id: "workbench",
    title: "业务工作台",
    icon: Boxes,
    description: "选择页面中的操作，探索背后的文档、接口与实现。",
  },
  {
    id: "documents",
    title: "文档中心",
    icon: FileText,
    description: "阅读、关联与维护项目知识，让上下文始终连贯。",
  },
  {
    id: "reviews",
    title: "关联审核",
    icon: Link2,
    description: "先检查来源和证据，再确认知识之间的关系。",
  },
  {
    id: "graph",
    title: "知识地图",
    icon: Network,
    description: "在当前业务上下文中，追溯每条关系的依据。",
  },
  {
    id: "versions",
    title: "版本与变更",
    icon: GitCompareArrows,
    description: "固定来源组合，审核并发布可追溯的知识版本。",
  },
  {
    id: "settings",
    title: "MCP 与设置",
    icon: Settings2,
    description: "让 AI 在明确的项目范围和权限内协助整理知识。",
  },
];
const initial = new URLSearchParams(
  window.location.hash.slice(1) || window.location.search.slice(1),
);
const initialView = initial.get("view") || initial.get("page") || "overview";
function workspaceHash(
  view: View,
  project: string,
  baseline: string,
  entity: string,
  explanation: ExplanationNavigation = emptyNavigation,
) {
  const query = new URLSearchParams({ view });
  if (project) query.set("project", project);
  if (baseline) query.set("baseline", baseline);
  if (entity) query.set("entity", entity);
  if (explanation.pageId) query.set("ui_page", explanation.pageId);
  if (explanation.scenarioId) query.set("scenario", explanation.scenarioId);
  if (explanation.stateKey) query.set("state", explanation.stateKey);
  if (explanation.actionId) query.set("action", explanation.actionId);
  return `#${query}`;
}
export default function App() {
  const workspaceRequest = useRef(0);
  const writtenRoute = useRef<{
    view: View;
    projectId: string;
    baseline: string;
    navigation: string;
  } | null>(null);
  const [session, setSession] = useState<Session | null>(null),
    [authError, setAuthError] = useState("");
  const [projects, setProjects] = useState<Project[]>([]),
    [projectId, setProjectId] = useState(initial.get("project") || ""),
    [baseline, setBaseline] = useState(initial.get("baseline") || "");
  const [view, setView] = useState<View>(
      (nav.some((n) => n.id === initialView)
        ? initialView
        : "overview") as View,
    ),
    [selected, setSelectedId] = useState(initial.get("entity") || "");
  const [w, setWorkspace] = useState<Workspace | null>(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false),
    [searchOpen, setSearchOpen] = useState(false),
    [helpOpen, setHelpOpen] = useState(false),
    [toast, setToast] = useState<{ message: string; tone: string } | null>(
      null,
    ),
    [hasUnsavedChanges, setUnsavedFlag] = useState(false);
  const [explanation, setExplanation] = useState<ExplanationNavigation>({
    pageId: initial.get("ui_page") || "",
    scenarioId: initial.get("scenario") || "",
    stateKey: initial.get("state") || "",
    actionId: initial.get("action") || "",
  });
  const explanationRef = useRef(explanation);
  explanationRef.current = explanation;
  const workspaceRef = useRef(w);
  workspaceRef.current = w;
  const unsavedChanges = useRef(false);
  const setUnsavedChanges = useCallback((value: boolean) => {
    unsavedChanges.current = value;
    setUnsavedFlag(value);
  }, []);
  const notify = useCallback((message: string, tone = "success") => {
    setToast({ message, tone });
  }, []);
  const navigateExplanation = useCallback(
    (intent: NavigationIntent) => {
      if (!workspaceRef.current) return;
      if (unsavedChanges.current) {
        notify("请先保存或还原当前草稿", "error");
        return;
      }
      try {
        const next = resolveExplanation(
          workspaceRef.current,
          explanationRef.current,
          intent,
        );
        explanationRef.current = next;
        setExplanation(next);
        setSelectedId(next.actionId);
      } catch (error) {
        notify((error as Error).message, "error");
      }
    },
    [notify],
  );
  const setSelected = useCallback(
    (id: string) => {
      if (workspaceRef.current?.actions.some((action) => action.id === id)) {
        navigateExplanation({ actionId: id, followTarget: false });
      } else {
        setSelectedId(id);
        if (!id) setExplanation(emptyNavigation);
      }
    },
    [navigateExplanation],
  );
  useEffect(() => {
    if (!w || w.project.id !== projectId) return;
    if (
      baseline.startsWith("draft:") &&
      w.selected_analysis_set?.id !== baseline.slice(6)
    )
      return;
    if (
      baseline.startsWith("release:") &&
      w.selected_release?.id !== baseline.slice(8)
    )
      return;
    try {
      const actionId =
        explanation.actionId ||
        (w.actions.some((a) => a.id === selected) ? selected : "");
      const next = resolveExplanation(w, explanation, {
        ...explanation,
        actionId,
        followTarget: false,
      });
      if (JSON.stringify(next) !== JSON.stringify(explanation))
        setExplanation(next);
      if (view === "workbench" && selected !== next.actionId)
        setSelectedId(next.actionId);
    } catch (error) {
      notify((error as Error).message, "error");
      setExplanation(
        resolveExplanation(w, emptyNavigation, { followTarget: false }),
      );
    }
  }, [w, projectId, baseline, view, selected, explanation, notify]);
  const [projectSearch, setProjectSearch] = useState("");
  const [projectCursor, setProjectCursor] = useState<string | null>(null);
  const [projectLoading, setProjectLoading] = useState(false);
  const projectRequest = useRef(0);
  const projectSelection = useRef(projectId);
  projectSelection.current = projectId;
  const lastProjectQuery = useRef<string | null>(null);
  const loadProjects = useCallback(
    async (query = lastProjectQuery.current || "", cursor?: string) => {
      const sequence = ++projectRequest.current;
      lastProjectQuery.current = query;
      setProjectLoading(true);
      try {
        const params = new URLSearchParams({ limit: "20" });
        if (query.trim()) params.set("q", query.trim());
        if (cursor) params.set("cursor", cursor);
        const response = await apiPage<Project[]>(`/projects?${params}`);
        if (sequence !== projectRequest.current) return;
        const selectedId = projectSelection.current;
        const pinned =
          selectedId && !response.data.some((p) => p.id === selectedId)
            ? await api<Project>(`/projects/${selectedId}`)
            : null;
        if (sequence !== projectRequest.current) return;
        setProjects((previous) => {
          const combined = [
            ...(pinned ? [pinned] : []),
            ...(cursor ? previous : []),
            ...response.data,
          ];
          return combined.filter(
            (p, index) =>
              combined.findIndex((other) => other.id === p.id) === index,
          );
        });
        setProjectCursor(response.meta.next_cursor || null);
        setProjectId((previous) => previous || response.data[0]?.id || "");
      } finally {
        if (sequence === projectRequest.current) setProjectLoading(false);
      }
    },
    [],
  );
  useEffect(() => {
    if (!session?.authenticated || lastProjectQuery.current === projectSearch)
      return;
    const timer = window.setTimeout(() => {
      void loadProjects(projectSearch).catch((e) => setError(e.message));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [projectSearch, session?.authenticated, loadProjects]);
  useEffect(() => {
    api<Session>("/auth/session")
      .then((s) => {
        setCsrf(s.csrf_token);
        setSession(s);
        if (s.authenticated)
          void loadProjects().catch((e) => setError(e.message));
      })
      .catch((e) => setAuthError(e.message));
  }, [loadProjects]);
  const refresh = useCallback(async () => {
    if (!projectId) return;
    const sequence = ++workspaceRequest.current;
    const query = new URLSearchParams();
    if (baseline.startsWith("release:"))
      query.set("release_id", baseline.slice(8));
    if (baseline.startsWith("draft:"))
      query.set("analysis_set_id", baseline.slice(6));
    const currentSession = await api<Session>("/auth/session");
    if (sequence !== workspaceRequest.current) return;
    setSession(currentSession);
    setCsrf(currentSession.csrf_token);
    if (!currentSession.authenticated) {
      setWorkspace(null);
      return;
    }
    let result: Workspace;
    try {
      result = await api<Workspace>(
        `/projects/${projectId}/workspace?${query}`,
      );
    } catch (error) {
      if (sequence !== workspaceRequest.current) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        setWorkspace(null);
        setUnsavedChanges(false);
        setSelected("");
        if (error.status === 401) {
          setSession({ authenticated: false });
          setCsrf(undefined);
        } else {
          await loadProjects();
        }
      }
      throw error;
    }
    if (sequence !== workspaceRequest.current) return;
    setProjects((previous) =>
      previous.some((p) => p.id === result.project.id)
        ? previous
        : [result.project, ...previous],
    );
    setWorkspace({
      ...result,
      entities: result.entities || [],
      relations: result.relations || [],
      evidence: result.evidence || [],
      documents: result.documents || [],
      pages: result.pages || [],
      actions: result.actions || [],
      scenarios: result.scenarios || [],
      sources: result.sources || [],
      jobs: result.jobs || [],
      audit_events: result.audit_events || [],
      analysis_sets: result.analysis_sets || [],
      releases: result.releases || [],
      unknowns: result.unknowns || [],
      stats: result.stats || {},
      capabilities: result.capabilities || {},
    });
    setError("");
  }, [projectId, baseline, loadProjects]);
  useEffect(() => {
    if (!session?.authenticated || !projectId) return;
    let active = true;
    setWorkspace(null);
    setLoading(true);
    void refresh()
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      workspaceRequest.current++;
    };
  }, [session?.authenticated, projectId, refresh]);
  useEffect(() => {
    if (
      !w?.jobs.some((j) =>
        ["queued", "running", "cancelling", "retry_wait"].includes(j.state),
      )
    )
      return;
    const timer = window.setInterval(() => {
      void refresh().catch(() => {});
    }, 1700);
    return () => window.clearInterval(timer);
  }, [w?.jobs, refresh]);
  useEffect(() => {
    const hash = workspaceHash(
      view,
      projectId,
      baseline,
      selected,
      explanation,
    );
    const previous = writtenRoute.current;
    const scopeChanged =
      previous &&
      (previous.view !== view ||
        previous.projectId !== projectId ||
        previous.baseline !== baseline ||
        previous.navigation !== JSON.stringify(explanation));
    if (window.location.hash !== hash) {
      if (session?.authenticated && previous?.projectId && scopeChanged) {
        history.pushState(null, "", hash);
      } else {
        history.replaceState(null, "", hash);
      }
    }
    writtenRoute.current = {
      view,
      projectId,
      baseline,
      navigation: JSON.stringify(explanation),
    };
  }, [
    view,
    projectId,
    baseline,
    selected,
    explanation,
    session?.authenticated,
  ]);
  useEffect(() => {
    function applyLocation() {
      const query = new URLSearchParams(window.location.hash.slice(1));
      const requested = query.get("view") || query.get("page") || "overview";
      const nextView = (
        nav.some((item) => item.id === requested) ? requested : "overview"
      ) as View;
      const nextProject = query.get("project") || projectId;
      const nextBaseline = query.get("baseline") || "";
      const nextEntity = query.get("entity") || "";
      const changed =
        nextView !== view ||
        nextProject !== projectId ||
        nextBaseline !== baseline ||
        nextEntity !== selected ||
        (query.get("action") || "") !== explanation.actionId ||
        (query.get("ui_page") || "") !== explanation.pageId ||
        (query.get("scenario") || "") !== explanation.scenarioId ||
        (query.get("state") || "") !== explanation.stateKey;
      if (changed && unsavedChanges.current) {
        history.replaceState(
          null,
          "",
          workspaceHash(view, projectId, baseline, selected, explanation),
        );
        notify("文档草稿尚未保存，请先保存或还原本地修改", "error");
        return;
      }
      setView(nextView);
      setProjectId(nextProject);
      setBaseline(nextBaseline);
      setSelectedId(nextEntity);
      setExplanation({
        pageId: query.get("ui_page") || "",
        scenarioId: query.get("scenario") || "",
        stateKey: query.get("state") || "",
        actionId: query.get("action") || "",
      });
    }
    window.addEventListener("hashchange", applyLocation);
    window.addEventListener("popstate", applyLocation);
    return () => {
      window.removeEventListener("hashchange", applyLocation);
      window.removeEventListener("popstate", applyLocation);
    };
  }, [
    view,
    projectId,
    baseline,
    selected,
    explanation,
    hasUnsavedChanges,
    notify,
  ]);
  useEffect(() => {
    const prevent = (e: BeforeUnloadEvent) => {
      if (unsavedChanges.current) e.preventDefault();
    };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen(true);
      }
      if (e.key === "Escape") {
        setSearchOpen(false);
        setCreateOpen(false);
        setHelpOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  async function run<T>(
    operation: () => Promise<T>,
    success?: string,
  ): Promise<T | undefined> {
    setBusy(true);
    try {
      const result = await operation();
      await refresh();
      if (success) notify(success);
      return result;
    } catch (e) {
      notify(
        e instanceof ApiError && e.status === 412
          ? `版本冲突：${e.message}。请刷新后比较本地修改。`
          : (e as Error).message,
        "error",
      );
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  function navigate(next: View, entity?: Entity) {
    if (unsavedChanges.current && next !== "documents") {
      notify("文档草稿尚未保存，请先保存或还原本地修改", "error");
      return;
    }
    setView(next);
    if (entity) setSelected(entity.id);
  }
  async function signIn(
    mode: "password" | "bootstrap",
    values: Record<string, string>,
  ) {
    setAuthError("");
    setBusy(true);
    try {
      const s = await api<Session>(
        mode === "bootstrap" ? "/auth/session" : "/auth/login",
        mutation("POST", values),
      );
      setCsrf(s.csrf_token);
      setSession(s);
      await loadProjects();
    } catch (e) {
      setAuthError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const active = nav.find((n) => n.id === view)!;
  if (session === null && !authError)
    return (
      <div className="auth-page" role="status" aria-label="正在验证本地会话">
        <Spinner />
      </div>
    );
  if (!session?.authenticated)
    return (
      <div className="auth-page">
        <div className="auth-brand">
          <Aperture size={34} />
          <strong>知境</strong>
          <span>VKS</span>
        </div>
        <div className="auth-grid">
          <section className="auth-intro">
            <span className="eyebrow">VISUAL KNOWLEDGE STUDIO</span>
            <h1>
              让项目知识，
              <br />
              有境可循。
            </h1>
            <p>
              从一个业务操作，连接文档、接口与源码。
              <br />
              每一条关联，都能回到它的来源。
            </p>
            <div className="auth-illustration">
              <div className="illustration-node n1">
                <Boxes size={20} />
                <span>业务操作</span>
              </div>
              <div className="illustration-node n2">
                <FileText size={20} />
                <span>知识文档</span>
              </div>
              <div className="illustration-node n3">
                <Network size={20} />
                <span>接口与实现</span>
              </div>
              <div className="illustration-core">
                <Aperture size={40} />
              </div>
              <svg viewBox="0 0 480 260">
                <path d="M120 75 L240 130 L365 65 M240 130 L350 215" />
              </svg>
              <span className="illustration-caption">
                <ShieldCheck size={14} /> 固定版本 · 人工审核 · 来源可追溯
              </span>
            </div>
          </section>
          <section className="auth-card">
            <span className="tag static">
              <ShieldCheck size={13} />
              本地私有工作空间
            </span>
            <h2>进入知境</h2>
            <LoginForm busy={busy} error={authError} onLogin={signIn} />
            <div className="auth-footnote">
              <LockIcon />
              数据保留在当前部署，平台模型默认关闭。
            </div>
          </section>
        </div>
        <footer>
          知境 VKS <span>把业务操作与实现依据连接起来</span>
        </footer>
      </div>
    );
  return (
    <div className="app">
      <aside className="sidebar">
        <button
          className="brand"
          onClick={() => navigate("overview")}
          aria-label="知境首页"
        >
          <Aperture size={34} />
          <strong>知境</strong>
          <span>VKS</span>
        </button>
        <div className="project-picker">
          <Layers3 size={17} />
          <select
            aria-label="当前项目"
            value={projectId}
            onChange={(e) => {
              if (hasUnsavedChanges) {
                notify("文档草稿尚未保存，请先保存或还原本地修改", "error");
                return;
              }
              setProjectId(e.target.value);
              setBaseline("");
              setSelected("");
            }}
          >
            {!projects.length && <option value="">选择项目</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <ChevronDown size={14} />
        </div>
        <input
          aria-label="搜索项目"
          placeholder="搜索项目名称或 key"
          value={projectSearch}
          onChange={(e) => setProjectSearch(e.target.value)}
        />
        {projectCursor && (
          <button
            className="text-button"
            disabled={projectLoading}
            onClick={() => {
              void loadProjects(projectSearch, projectCursor).catch((e) =>
                setError(e.message),
              );
            }}
          >
            加载更多项目
          </button>
        )}
        <div className="nav-label">工作空间</div>
        <nav aria-label="主导航">
          {nav.map((n) => (
            <button
              key={n.id}
              className={`nav-item ${view === n.id ? "active" : ""}`}
              onClick={() => navigate(n.id)}
            >
              <n.icon size={19} />
              <span>{n.title}</span>
              {n.id === "reviews" &&
                !!w?.relations.filter((r) => r.review_status === "candidate")
                  .length && (
                  <b>
                    {
                      w.relations.filter((r) => r.review_status === "candidate")
                        .length
                    }
                  </b>
                )}
            </button>
          ))}
        </nav>
        {session.user?.instance_admin && (
          <button className="sidebar-add" onClick={() => setCreateOpen(true)}>
            <Plus size={16} />
            新建项目
          </button>
        )}
        <div className="sidebar-bottom">
          <div className="local-mode">
            <span className="status-dot" /> 本地工作空间 <Tag>1.0 候选</Tag>
          </div>
          <button className="help-button" onClick={() => setHelpOpen(true)}>
            <CircleHelp size={17} />
            使用指南
            <ChevronRight size={14} />
          </button>
          <div className="user">
            <div className="avatar">知</div>
            <span>
              <strong>
                {session.name ||
                  session.user?.display_name ||
                  session.user?.name ||
                  "项目成员"}
              </strong>
              <small>
                {session.user?.instance_admin ? "实例管理员" : "项目成员"}
              </small>
            </span>
            <button
              className="icon-button"
              title="退出会话"
              aria-label="退出会话"
              onClick={() => {
                void api("/auth/session", { method: "DELETE" })
                  .then(() => {
                    setSession({ authenticated: false });
                    setCsrf("");
                  })
                  .catch((e) => notify(e.message, "error"));
              }}
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      <div className="app-body">
        <header className="topbar">
          <div className="breadcrumb">
            <span>工作空间</span>
            <ChevronRight size={13} />
            <strong>{w?.project.name || "我的项目"}</strong>
            <ChevronRight size={13} />
            <span>{active.title}</span>
          </div>
          <div className="topbar-actions">
            <button
              className="global-search"
              onClick={() => setSearchOpen(true)}
            >
              <Search size={16} />
              <span>搜索业务、接口、文档…</span>
              <kbd>⌘ K</kbd>
            </button>
            <div className="baseline-select">
              <Layers3 size={15} />
              <select
                aria-label="知识基线"
                value={baseline}
                onChange={(e) => {
                  if (hasUnsavedChanges) {
                    notify("文档草稿尚未保存，请先保存或还原本地修改", "error");
                    return;
                  }
                  setBaseline(e.target.value);
                  setSelected("");
                }}
              >
                <option value="">默认基线</option>
                {w?.analysis_sets
                  .filter((a) => !["released", "frozen"].includes(a.state))
                  .map((a) => (
                    <option key={a.id} value={`draft:${a.id}`}>
                      草稿 · {a.name}
                    </option>
                  ))}
                {w?.releases.map((r) => (
                  <option key={r.id} value={`release:${r.id}`}>
                    已发布 · {r.name || r.release_no}
                  </option>
                ))}
              </select>
              <ChevronDown size={13} />
            </div>
            <span className="topbar-avatar">知</span>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="page-eyebrow">
                PROJECT KNOWLEDGE /{" "}
                {String(nav.findIndex((n) => n.id === view) + 1).padStart(
                  2,
                  "0",
                )}
              </div>
              <h1>
                {view === "overview"
                  ? w?.project.name || "我的项目"
                  : active.title}
              </h1>
              <p>{active.description}</p>
            </div>
            <div className="heading-actions">
              <RenderBoundary
                key={`${projectId}:${baseline}:${view}`}
                fallback={
                  <Panel>
                    <Empty
                      title="当前页面暂时无法显示"
                      description="请重新加载页面；也可以从左侧导航继续查看其他内容。"
                      action={
                        <Button onClick={() => window.location.reload()}>
                          重新加载页面
                        </Button>
                      }
                    />
                  </Panel>
                }
              >
                {view === "overview" ? (
                  <>
                    <Button
                      onClick={() => navigate("workbench")}
                      disabled={!w?.actions.length}
                    >
                      <Boxes size={16} />
                      进入业务工作台
                    </Button>
                    <Button
                      kind="primary"
                      onClick={() => setView("imports")}
                      disabled={
                        !projectId ||
                        !hasProjectPermission(
                          session,
                          projectId,
                          "source:import",
                        )
                      }
                    >
                      <Plus size={17} />
                      导入资料
                    </Button>
                  </>
                ) : (
                  <div className="version-chip">
                    <span className="status-dot" />
                    {w?.selected_analysis_set?.name || "尚未选择基线"}
                    <span className="divider" />
                    {label(w?.selected_analysis_set?.state || "draft")}
                  </div>
                )}
              </RenderBoundary>
            </div>
          </div>
          {error && (
            <Notice tone="red">
              <strong>未能加载项目</strong>
              <p>{error}</p>
              <Button
                onClick={() => {
                  setLoading(true);
                  void refresh()
                    .catch((e) => setError(e.message))
                    .finally(() => setLoading(false));
                }}
              >
                重新加载
              </Button>
            </Notice>
          )}
          {loading ? (
            <Spinner />
          ) : !projectId ? (
            session.user?.instance_admin ? (
              <Welcome onCreate={() => setCreateOpen(true)} />
            ) : (
              <Panel>
                <Empty
                  title="尚未获得项目授权"
                  description="请联系实例或项目管理员，将你的账户加入需要访问的项目。"
                />
              </Panel>
            )
          ) : w ? (
            <WorkspaceContext.Provider
              value={{
                w,
                session,
                selected,
                setSelected,
                explanation,
                navigateExplanation,
                navigate,
                refresh,
                notify,
                run,
                busy,
                baseline,
                setBaseline: (value) => {
                  if (hasUnsavedChanges) {
                    notify("请先保存或还原文档修改", "error");
                    return;
                  }
                  setBaseline(value);
                  setSelected("");
                },
                hasUnsavedChanges,
                setUnsavedChanges,
              }}
            >
              {view === "overview" ? (
                <Overview />
              ) : view === "imports" ? (
                <Imports />
              ) : view === "workbench" ? (
                <Workbench />
              ) : view === "documents" ? (
                <Documents />
              ) : view === "reviews" ? (
                <Reviews />
              ) : view === "graph" ? (
                <Graph />
              ) : view === "versions" ? (
                <Versions />
              ) : (
                <Settings />
              )}
            </WorkspaceContext.Provider>
          ) : null}
          <footer className="page-footer">
            <span>
              <ShieldCheck size={12} />
              所有知识均在所选版本中查询
            </span>
            <span>知境 VKS · 1.0 候选版</span>
          </footer>
        </main>
      </div>
      {toast && (
        <div role="status" className={`toast ${toast.tone}`}>
          {toast.tone === "error" ? <InfoIcon /> : <ShieldCheck size={18} />}
          <span>{toast.message}</span>
          <button aria-label="关闭提示" onClick={() => setToast(null)}>
            <X size={16} />
          </button>
        </div>
      )}
      {createOpen && (
        <CreateProject
          onClose={() => setCreateOpen(false)}
          onCreate={async (p) => {
            await loadProjects();
            setProjectId(p.id);
            setBaseline("");
            setView("overview");
            setCreateOpen(false);
          }}
        />
      )}
      {searchOpen && (
        <SearchDialog
          w={w}
          onClose={() => setSearchOpen(false)}
          onSelect={(e) => {
            if (hasUnsavedChanges) {
              notify("文档草稿尚未保存，请先保存或还原本地修改", "error");
              return;
            }
            setSelected(e.id);
            setView(
              ["document", "document_section", "section"].includes(e.type)
                ? "documents"
                : ["action", "ui_action"].includes(e.type)
                  ? "workbench"
                  : "graph",
            );
            setSearchOpen(false);
          }}
        />
      )}
      {helpOpen && (
        <Modal title="从第一份资料开始" onClose={() => setHelpOpen(false)}>
          <div className="guide-list">
            <div>
              <span>01</span>
              <section>
                <h3>创建项目，导入材料</h3>
                <p>
                  上传包含 manifest.json 的 VKS ZIP
                  包，或导入合成示例。先预检，再建立版本快照。
                </p>
              </section>
            </div>
            <div>
              <span>02</span>
              <section>
                <h3>检查关联与证据</h3>
                <p>
                  在工作台选择动作，在关联审核页查看来源。确认与拒绝都将留下审核记录。
                </p>
              </section>
            </div>
            <div>
              <span>03</span>
              <section>
                <h3>发布并共享上下文</h3>
                <p>
                  通过发布预检后，由人确认发布。MCP
                  客户端读取同一份固定版本知识。
                </p>
              </section>
            </div>
          </div>
          <Notice>
            当前为 VKS 1.0
            候选版。可完成资料接入、解析、审核与不可变发布；受控录制、OAuth
            和模型辅助按项目配置开放。真实业务试点与性能门禁仍待验收。
          </Notice>
        </Modal>
      )}
    </div>
  );
}
function LockIcon() {
  return <ShieldCheck size={14} />;
}
function InfoIcon() {
  return <CircleHelp size={18} />;
}
function Welcome({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="welcome">
      <div className="welcome-art">
        <Aperture size={52} />
        <span className="orb orb-one">
          <FileText size={24} />
        </span>
        <span className="orb orb-two">
          <Network size={24} />
        </span>
        <span className="orb orb-three">
          <Boxes size={24} />
        </span>
      </div>
      <span className="eyebrow">每个项目，都值得被理解</span>
      <h2>建立你的第一个项目知识空间</h2>
      <p>
        把分散的文档、页面与接口连接起来。
        <br />
        从一个小场景开始，逐步形成可追溯的项目知识。
      </p>
      <Button kind="primary" onClick={onCreate}>
        <Plus size={17} />
        创建第一个项目
      </Button>
      <div className="onboarding-cards">
        {[
          { n: "01", i: Upload, t: "接入资料", d: "保留原始文件与版本快照" },
          { n: "02", i: Link2, t: "审核关联", d: "每一条关系，都有来源依据" },
          {
            n: "03",
            i: BookOpen,
            t: "探索知识",
            d: "从业务操作找到准确的上下文",
          },
        ].map((s) => (
          <div key={s.n}>
            <span>{s.n}</span>
            <s.i size={23} />
            <h3>{s.t}</h3>
            <p>{s.d}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
function CreateProject({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (p: Project) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    try {
      const p = await api<Project>(
        "/projects",
        mutation("POST", {
          key: f.get("key"),
          name: f.get("name"),
          description: f.get("description"),
        }),
      );
      await onCreate(p);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="创建项目知识空间" onClose={onClose}>
      <p className="muted">
        一个空间管理一个项目的资料、场景、版本和知识关联。
      </p>
      <form
        onSubmit={(e) => {
          void submit(e);
        }}
      >
        <label>
          项目名称
          <input
            name="name"
            required
            autoFocus
            maxLength={120}
            placeholder="例如：云收费知识工程"
          />
        </label>
        <label>
          项目标识 <small>用于导入包中的 project_key</small>
          <input
            name="key"
            required
            pattern="[a-z][a-z0-9._\-]*"
            maxLength={96}
            placeholder="例如：charge-demo"
          />
        </label>
        <label>
          项目描述
          <textarea
            name="description"
            rows={3}
            placeholder="这个项目解决什么问题？"
          />
        </label>
        {error && <Notice tone="red">{error}</Notice>}
        <div className="modal-actions">
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" type="submit" disabled={busy}>
            {busy ? "正在创建…" : "创建项目"}
            <ArrowRight size={15} />
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function SearchDialog({
  w,
  onClose,
  onSelect,
}: {
  w: Workspace | null;
  onClose: () => void;
  onSelect: (e: Entity) => void;
}) {
  const [moduleNodes, setModuleNodes] = useState<ModuleNode[]>([]),
    [moduleId, setModuleId] = useState(""),
    [serviceKey, setServiceKey] = useState(""),
    [kind, setKind] = useState(""),
    [descendants, setDescendants] = useState(true);
  const [query, setQuery] = useState(""),
    [results, setResults] = useState<Entity[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    setModuleId("");
    setModuleNodes([]);
    setServiceKey("");
    setKind("");
    if (!w) return;
    const controller = new AbortController();
    void api<ModuleNode[]>(
      "/projects/" +
        w.project.id +
        "/modules?" +
        moduleBaseline(w.selected_analysis_set?.id, w.selected_release?.id),
      { signal: controller.signal },
    )
      .then(setModuleNodes)
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      });
    return () => controller.abort();
  }, [w?.project.id, w?.selected_analysis_set?.id, w?.selected_release?.id]);
  useEffect(() => {
    if (!w || !query.trim()) {
      setResults([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setBusy(true);
      setError("");
      const scope = w.selected_analysis_set?.id;
      void api<unknown>(`/projects/${w.project.id}/search`, {
        ...mutation("POST", {
          query,
          ...(w.selected_release
            ? { release_id: w.selected_release.id }
            : { analysis_set_id: scope }),
          limit: 30,
          ...(moduleId
            ? { module_id: moduleId, include_descendants: descendants }
            : {}),
          ...(serviceKey.trim() ? { service_key: serviceKey.trim() } : {}),
          ...(kind ? { kinds: [kind] } : {}),
        }),
        signal: controller.signal,
      })
        .then((r) => {
          const hits = (
            Array.isArray(r)
              ? r
              : (r as { items?: unknown[]; results?: unknown[] }).items ||
                (r as { results?: unknown[] }).results ||
                []
          ) as Array<{
            entity?: { entity_id?: string };
            entity_id?: string;
            id?: string;
          }>;
          setResults(
            hits
              .map((hit) =>
                w.entities.find(
                  (e) =>
                    e.id === (hit.entity?.entity_id || hit.entity_id || hit.id),
                ),
              )
              .filter((e): e is Entity => !!e),
          );
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        })
        .finally(() => setBusy(false));
    }, 280);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, w, moduleId, serviceKey, kind, descendants]);
  return (
    <Modal title="搜索项目知识" onClose={onClose} className="search-dialog">
      <div className="search-input">
        <Search size={21} />
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="输入业务、接口、路径或文档关键词…"
        />
      </div>
      <div className="search-filters">
        <label>
          模块范围
          <select
            value={moduleId}
            onChange={(e) => setModuleId(e.target.value)}
          >
            <option value="">全部模块</option>
            {flattenModules(moduleNodes).map((node) => (
              <option key={node.id} value={node.id}>
                {"　".repeat(node.depth) + node.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          服务标识
          <input
            value={serviceKey}
            onChange={(e) => setServiceKey(e.target.value)}
            placeholder="全部服务，例如 backend"
          />
        </label>
        <label>
          知识类型
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">全部类型</option>
            {[...new Set(w?.entities.map((entity) => entity.type) || [])].map(
              (type) => (
                <option key={type} value={type}>
                  {label(type)}
                </option>
              ),
            )}
          </select>
        </label>
      </div>
      {moduleId && (
        <label className="check-line">
          <input
            type="checkbox"
            checked={descendants}
            onChange={(e) => setDescendants(e.target.checked)}
          />
          同时搜索子模块
        </label>
      )}
      <p className="search-context">
        {w?.project.name || "尚未选择项目"}
        <ChevronRight size={12} />
        {w?.selected_analysis_set?.name || "当前基线"}
        <Tag>权限范围内检索</Tag>
      </p>
      {error && <Notice tone="red">{error}</Notice>}
      {busy ? (
        <Spinner />
      ) : results.length ? (
        <div className="search-results">
          {results.map((e) => (
            <button key={e.id} onClick={() => onSelect(e)}>
              {entityIcon(e.type, 22)}
              <span>
                <strong>{e.name}</strong>
                <small>
                  {label(e.type)} · {e.stable_key}
                </small>
              </span>
              <ArrowRight size={15} />
            </button>
          ))}
        </div>
      ) : query ? (
        <Empty
          title="当前基线没有找到相关知识"
          description="尝试精确路径或其他关键词，也可以补充当前版本的资料。"
        />
      ) : (
        <div className="search-suggestions">
          <Command size={24} />
          <p>用一句业务名称，或一条准确的 API 路径开始。</p>
        </div>
      )}
    </Modal>
  );
}
