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
  Sparkles,
  LoaderCircle,
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
import { AISettings } from "./AISettings";

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
  scope?: "module" | "control";
  origin?: string;
  model_id?: string;
};
type Control = {
  target: Target;
  context: {
    scope?: "module" | "control";
    page_title: string;
    section_title: string;
    nearby_labels: string[];
  };
};
type Assistance = {
  title: string;
  explanation: string;
  unknowns: string[];
  confidence: number;
  model_id: string;
  cached: boolean;
  saved: boolean;
  binding_id: string | null;
  sections: Citation[];
  selection_version: number;
};
type Related = {
  title: string;
  sections: Citation[];
  binding_id: string | null;
  excluded: boolean;
  origin: "retrieval" | "binding";
  model_available: boolean;
  selection_version: number;
  elapsed_ms: number;
  retrieval?: "keyword" | "hybrid" | "binding";
  vector_available?: boolean;
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
    loadEpoch = useRef(0),
    selectedBinding = useRef("");
  const [related, setRelated] = useState<Related | null>(null),
    [retrieving, setRetrieving] = useState(false);
  const [assistant, setAssistant] = useState<Assistance | null>(null),
    [assisting, setAssisting] = useState(false),
    [assistantMode, setAssistantMode] = useState<"documents" | "explain">(
      "explain",
    ),
    [assistantError, setAssistantError] = useState(""),
    [automatic, setAutomatic] = useState(true),
    [moduleTitle, setModuleTitle] = useState(""),
    [menu, setMenu] = useState<(Control & { x: number; y: number }) | null>(
      null,
    );
  const moduleControl = useRef<Control | null>(null),
    control = useRef<Control | null>(null),
    assistEpoch = useRef(0),
    confirmedEpoch = useRef(-1),
    assistAbort = useRef<AbortController | null>(null),
    automaticTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
      undefined,
    ),
    menuRef = useRef<HTMLDivElement>(null);
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
    assistEpoch.current++;
    assistAbort.current?.abort();
    clearTimeout(automaticTimer.current);
    control.current = null;
    moduleControl.current = null;
    setModuleTitle("");
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssisting(false);
    setAssistantError("");
    setMenu(null);
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
    if (!route.binding) {
      selectedBinding.current = "";
      return;
    }
    const changed = selectedBinding.current !== route.binding;
    selectedBinding.current = route.binding;
    const b = bindings.find((b) => b.id === route.binding);
    if (
      b &&
      (changed ||
        !control.current ||
        sameTarget(control.current.target, b.target))
    ) {
      setActive(b);
      setTarget(b.target);
      control.current = {
        target: b.target,
        context: {
          scope: b.target.selector === "body" ? "module" : "control",
          page_title: b.title,
          section_title: "",
          nearby_labels: [],
        },
      };
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
  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const dismiss = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenu(null);
    };
    window.addEventListener("pointerdown", dismiss);
    const resize = () => setMenu(null);
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("resize", resize);
    };
  }, [menu]);
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMenu(null);
        return;
      }
      if (
        route.view !== "prototype" ||
        !e.altKey ||
        e.ctrlKey ||
        e.metaKey ||
        e.repeat ||
        e.isComposing ||
        (e.target as HTMLElement)?.closest(
          "input,textarea,select,[contenteditable=true]",
        )
      )
        return;
      if (e.code === "KeyD" || e.code === "KeyA") {
        e.preventDefault();
        prototype.current?.send("knowledge.request", {
          mode: e.code === "KeyD" ? "documents" : "explain",
        });
      }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, [route.view]);
  function chooseControl(c: Control) {
    assistEpoch.current++;
    assistAbort.current?.abort();
    clearTimeout(automaticTimer.current);
    control.current = c;
    setAssistantMode("explain");
    setTarget(c.target);
    setActive(
      bindings.find(
        (b) =>
          b.package_id === p?.id &&
          sameTarget(b.target, c.target) &&
          !b.excluded,
      ) || null,
    );
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssistantError("");
    setAssisting(false);
  }
  function switchKnowledge(mode: "documents" | "explain") {
    setAssistantMode(mode);
    if (!control.current) return;
    if (mode === "documents" && (related || active)) return;
    if (
      mode === "explain" &&
      (assistant || assisting || active?.origin === "model")
    )
      return;
    void assist(control.current, mode);
  }
  async function assist(
    c: Control,
    mode: "documents" | "explain",
    refresh = false,
  ) {
    if (editing || !p) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    chooseControl(c);
    setAssistantMode(mode);
    setMenu(null);
    setKnowledge(true);
    setFolded(false);
    const existing = bindings.find(
      (b) => b.package_id === p.id && sameTarget(b.target, c.target),
    );
    if (existing?.excluded) {
      setAssistantError(
        "此关联已被移除，自动关联不会恢复它；可在关联管理中恢复。",
      );
      return;
    }
    if (
      existing &&
      !refresh &&
      (mode === "documents" || existing.origin === "model")
    )
      return;
    const epoch = assistEpoch.current,
      controller = new AbortController();
    assistAbort.current = controller;
    const payload = {
      ...(route.release
        ? { release_id: route.release }
        : { analysis_set_id: route.analysis }),
      package_id: p.id,
      target: c.target,
      context: c.context,
      mode,
      refresh,
    };
    let matches: Related | null = existing
      ? {
          title: existing.title,
          sections: existing.sections,
          binding_id: existing.id,
          excluded: false,
          origin: "binding",
          model_available: true,
          selection_version: catalog?.selection_version || 0,
          elapsed_ms: 0,
        }
      : null;
    if (matches) setRelated(matches);
    else {
      setRetrieving(true);
      try {
        matches = await api<Related>(`${prefix}/related`, {
          ...mutation("POST", payload),
          signal: controller.signal,
        });
        if (epoch !== assistEpoch.current) return;
        setRelated(matches);
        if (matches.vector_available && matches.origin !== "binding") {
          void api<Related>(`${prefix}/semantic`, {
            ...mutation("POST", payload),
            signal: controller.signal,
          })
            .then((result) => {
              if (
                epoch === assistEpoch.current &&
                confirmedEpoch.current !== epoch
              )
                setRelated(result);
            })
            .catch(() => {});
        }
      } catch (e) {
        if (epoch === assistEpoch.current && !controller.signal.aborted)
          setAssistantError(text(e));
        return;
      } finally {
        if (epoch === assistEpoch.current) setRetrieving(false);
      }
    }
    if (matches.excluded) {
      setAssistantError("此关联已被移除，自动关联不会恢复它。");
      return;
    }
    if (
      mode === "documents" &&
      (!automatic || !matches.model_available || !matches.sections.length)
    )
      return;
    const runModel = async () => {
      if (epoch !== assistEpoch.current || controller.signal.aborted) return;
      setAssisting(true);
      try {
        const result = await api<Assistance>(`${prefix}/assist`, {
          ...mutation("POST", payload),
          signal: controller.signal,
        });
        if (epoch !== assistEpoch.current) return;
        confirmedEpoch.current = epoch;
        setAssistant(result);
        setAssisting(false);
        if (result.sections.length && (!existing || result.saved))
          setRelated((previous) =>
            previous
              ? {
                  ...previous,
                  title: result.title,
                  sections: result.sections,
                  binding_id: result.binding_id,
                  origin: result.saved ? "binding" : "retrieval",
                }
              : previous,
          );
        const updated = await reload();
        if (epoch !== assistEpoch.current) return;
        setActive(
          updated?.b.find((b) => b.id === result.binding_id && !b.excluded) ||
            existing ||
            null,
        );
      } catch (e) {
        if (epoch === assistEpoch.current && !controller.signal.aborted)
          setAssistantError(text(e));
      } finally {
        if (epoch === assistEpoch.current) setAssisting(false);
      }
    };
    if (mode === "documents") {
      setAssisting(true);
      automaticTimer.current = setTimeout(() => void runModel(), 350);
    } else await runModel();
  }
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
    if (event.type === "context.dismiss") {
      setMenu(null);
      return;
    }
    if (
      ["knowledge.request", "context.menu", "control.activated"].includes(
        event.type,
      )
    ) {
      const c = event.payload as Control & {
        mode?: string;
        x?: number;
        y?: number;
      };
      if (
        !c ||
        !isTarget(c.target) ||
        !c.context ||
        typeof c.context.page_title !== "string" ||
        typeof c.context.section_title !== "string" ||
        !Array.isArray(c.context.nearby_labels)
      )
        return;
      if (editing) {
        if (event.type !== "control.activated")
          setNotice("请先保存或取消当前编辑");
        return;
      }
      if (event.type === "context.menu") {
        chooseControl(c);
        setMenu({
          ...c,
          x: Math.max(8, Math.min(c.x || 8, innerWidth - 292)),
          y: Math.max(8, Math.min(c.y || 8, innerHeight - 230)),
        });
      } else if (event.type === "knowledge.request")
        void assist(c, c.mode === "documents" ? "documents" : "explain");
      else if (!pinned) {
        chooseControl(c);
        if (
          automatic &&
          (route.view || "prototype") === "prototype" &&
          !bindings.some((b) => sameTarget(b.target, c.target))
        )
          void assist(c, "documents");
      }
      return;
    }
    if (event.type === "context.changed") {
      assistEpoch.current++;
      assistAbort.current?.abort();
      clearTimeout(automaticTimer.current);
      setAssisting(false);
      const c = event.payload as {
        route?: string;
        frame_path?: string[];
        module?: Control;
      };
      const moduleChanged = !!(
        c.module &&
        moduleControl.current &&
        !sameTarget(c.module.target, moduleControl.current.target)
      );
      if (c.module && isTarget(c.module.target)) {
        moduleControl.current = c.module;
        setModuleTitle(c.module.target.label);
      }
      if (moduleChanged && route.binding) navigate({ binding: "" }, true);
      if (
        !pinned &&
        target &&
        c.route !== target.route &&
        JSON.stringify(c.frame_path) === JSON.stringify(target.frame_path)
      ) {
        setAssistant(null);
        setRelated(null);
        setRetrieving(false);
        setAssistantError("");
        control.current = null;
        setActive(null);
        setTarget(null);
      }
      setMenu(null);
      if (
        c.module &&
        isTarget(c.module.target) &&
        !pinned &&
        !editing &&
        (!route.binding || moduleChanged) &&
        !pendingReveal.current
      ) {
        chooseControl(c.module);
        if (automatic && (route.view || "prototype") === "prototype")
          void assist(c.module, "documents");
      }
    }
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
    setMenu(null);
    if (!isTarget(t)) return;
    if (event.type === "target.selected") {
      assistEpoch.current++;
      assistAbort.current?.abort();
      clearTimeout(automaticTimer.current);
      setAssistant(null);
      setRelated(null);
      setRetrieving(false);
      setAssistantError("");
      setAssisting(false);
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
      if (!target || !sameTarget(target, t)) {
        assistEpoch.current++;
        assistAbort.current?.abort();
        clearTimeout(automaticTimer.current);
        setAssistant(null);
        setRelated(null);
        setRetrieving(false);
        setAssistantError("");
        setAssisting(false);
        control.current = null;
      }
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
    assistEpoch.current++;
    assistAbort.current?.abort();
    clearTimeout(automaticTimer.current);
    setAssisting(false);
    setPicking(true);
    prototype.current?.send("picker.start");
    navigate({ view: "prototype" });
    setNotice("请点击需要关联的原型控件；按 Esc 或点击取消结束");
  }
  function openDoc(c: Citation, b?: Pick<Binding, "id">) {
    setOriginalBook(false);
    navigate({
      view: "documents",
      doc: c.document_revision_id,
      section: c.revision_id,
      binding: b?.id || "",
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
    assistEpoch.current++;
    assistAbort.current?.abort();
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssistantError("");
    setAssisting(false);
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
  const documentBindings = bindings.filter(
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
            ["ai", "AI 设置"],
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
                : currentView === "ai"
                  ? "AI 设置"
                  : "关联管理"}
          </span>
          <ChevronRight size={13} />
          <span>
            {currentView === "prototype"
              ? target?.label || "在原页面操作，即可查看相关知识"
              : currentView === "documents"
                ? viewDoc?.name || "独立阅读与章节关联"
                : currentView === "ai"
                  ? "配置本项目的模型连接"
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
      <main
        className={
          "workspace " +
          (knowledge && docked && currentView !== "ai" ? "with-dock" : "")
        }
      >
        <section
          className="prototype-space"
          hidden={currentView !== "prototype"}
        >
          {p ? (
            <>
              <div className="assistant-toolbar">
                <span>
                  <Sparkles size={14} /> 当前模块或指向控件：<kbd>Alt D</kbd>{" "}
                  文档 · <kbd>Alt A</kbd> AI 讲解 · 右键更多
                </span>
                <button
                  disabled={!moduleTitle}
                  title={moduleTitle || "等待模块加载"}
                  onClick={() =>
                    prototype.current?.send("knowledge.request", {
                      scope: "module",
                      mode: "documents",
                    })
                  }
                >
                  <BookOpen size={14} />
                  模块知识
                </button>
                <label>
                  <input
                    type="checkbox"
                    checked={automatic}
                    onChange={(e) => {
                      setAutomatic(e.target.checked);
                      clearTimeout(automaticTimer.current);
                    }}
                  />
                  自动关联模块与控件
                </label>
              </div>
              <OriginalRuntime
                key={p.id + scope}
                url={`${prefix}/packages/${p.id}/content?${scope}`}
                digest={p.sha256}
                kind="prototype"
                handle={prototype}
                onEvent={onRuntime}
              />
            </>
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
              {documentBindings.length ? (
                documentBindings.map((b) => (
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
        {currentView === "ai" && (
          <AISettings
            key={route.project}
            project={route.project}
            analysis={route.analysis}
            release={route.release}
          />
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
      {menu && (
        <div
          ref={menuRef}
          className="control-menu"
          role="menu"
          aria-label="控件知识菜单"
          style={{ left: menu.x, top: menu.y }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setMenu(null);
              return;
            }
            const buttons = Array.from(
              e.currentTarget.querySelectorAll("button"),
            );
            const i = buttons.indexOf(
              document.activeElement as HTMLButtonElement,
            );
            if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
              e.preventDefault();
              buttons[
                e.key === "Home"
                  ? 0
                  : e.key === "End"
                    ? buttons.length - 1
                    : (i + (e.key === "ArrowDown" ? 1 : buttons.length - 1)) %
                      buttons.length
              ]?.focus();
            }
          }}
        >
          <div className="control-menu-title">
            <MousePointer2 size={14} />
            {menu.target.label}
          </div>
          <button
            role="menuitem"
            onClick={() => void assist(menu, "documents")}
          >
            <BookOpen size={16} />
            查看关联文档<kbd>Alt D</kbd>
          </button>
          <button role="menuitem" onClick={() => void assist(menu, "explain")}>
            <Sparkles size={16} />
            AI 讲解此功能<kbd>Alt A</kbd>
          </button>
          <button
            role="menuitem"
            onClick={() => void assist(menu, "explain", true)}
          >
            <Link2 size={16} />
            重新检索与讲解
          </button>
          <hr />
          <button
            role="menuitem"
            disabled={!catalog?.editable}
            onClick={() => {
              const b = bindings.find((b) => sameTarget(b.target, menu.target));
              setMenu(null);
              if (b) editBinding(b);
              else {
                setTitle(menu.target.label);
                setSummary("");
                setEditDocId("");
                setSectionId("");
                setEditing(true);
                setKnowledge(true);
                setFolded(false);
              }
            }}
          >
            手动补充关联
          </button>
        </div>
      )}
      {knowledge && currentView !== "ai" && (
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
                  {editing
                    ? "维护关联"
                    : target?.selector === "body"
                      ? "模块知识"
                      : "控件知识"}
                </span>
              </div>
              {!editing && target && (
                <div
                  className="knowledge-tabs"
                  role="tablist"
                  aria-label="知识内容"
                >
                  <button
                    role="tab"
                    aria-selected={assistantMode === "documents"}
                    onClick={() => switchKnowledge("documents")}
                  >
                    相关文档
                  </button>
                  <button
                    role="tab"
                    aria-selected={assistantMode === "explain"}
                    onClick={() => switchKnowledge("explain")}
                  >
                    AI 讲解
                  </button>
                </div>
              )}
              <div className="knowledge-body">
                {!editing && retrieving && !related && (
                  <div className="assistant-loading" role="status">
                    <LoaderCircle size={18} className="spinning" />
                    <b>正在查找本地章节…</b>
                  </div>
                )}
                {!editing && assisting && (
                  <div className="assistant-background" role="status">
                    <LoaderCircle size={14} className="spinning" />
                    {assistantMode === "documents"
                      ? "相关章节已显示，AI 正在后台核对关联…"
                      : "AI 正在生成讲解，可先切换查看相关文档…"}
                  </div>
                )}
                {!editing &&
                  related &&
                  (assistantMode === "documents" || !assistant) && (
                    <section
                      className="related-results"
                      aria-label="相关文档检索结果"
                    >
                      <span className="eyebrow">
                        {related.origin === "binding"
                          ? "已保存的准确引用"
                          : related.retrieval === "hybrid"
                            ? "RAG 向量 + 关键词匹配 · 待 AI 核对"
                            : "本地章节匹配 · 待 AI 核对"}
                      </span>
                      <h2>{related.title}</h2>
                      {!related.sections.length && (
                        <p>尚未找到相关章节，可在文档页搜索或补充资料。</p>
                      )}
                      {related.sections.map((c) => (
                        <button
                          key={c.revision_id}
                          className="citation"
                          onClick={() =>
                            openDoc(
                              c,
                              related.binding_id
                                ? { id: related.binding_id }
                                : undefined,
                            )
                          }
                        >
                          <FileText size={18} />
                          <span>
                            {c.document_name}
                            <small>{c.name}</small>
                          </span>
                          <ArrowUpRight size={15} />
                        </button>
                      ))}
                      {related.sections.length > 0 && (
                        <details className="inline-preview">
                          <summary>
                            {related.origin === "retrieval"
                              ? "查看匹配片段"
                              : "在小窗查看原文"}
                          </summary>
                          {related.sections.map((c) => (
                            <div key={c.revision_id}>
                              <h4>{c.name}</h4>
                              <Markdown
                                documentId={
                                  catalog?.documents.find(
                                    (d) =>
                                      d.revision_id === c.document_revision_id,
                                  )?.id
                                }
                                revisionId={c.document_revision_id}
                              >
                                {c.content}
                              </Markdown>
                            </div>
                          ))}
                        </details>
                      )}
                    </section>
                  )}
                {!editing && assistantError && (
                  <div
                    className={
                      related?.sections.length
                        ? "assistant-error assistant-note"
                        : "assistant-error"
                    }
                    role="alert"
                  >
                    <b>暂未完成关联</b>
                    <p>{assistantError}</p>
                    <button
                      onClick={() =>
                        control.current &&
                        void assist(control.current, "explain", true)
                      }
                    >
                      重试
                    </button>
                    <button onClick={() => navigate({ view: "ai" })}>
                      AI 设置
                    </button>
                  </div>
                )}
                {!editing && assistant && assistantMode === "explain" && (
                  <section
                    className="assistant-answer"
                    aria-label="AI 功能讲解"
                  >
                    <span className="eyebrow">
                      <Sparkles size={13} /> AI 讲解 ·{" "}
                      {assistant.model_id || "章节检索"}
                      {assistant.cached ? " · 已缓存" : ""}
                    </span>
                    <h2>{assistant.title}</h2>
                    {assistantMode === "explain" && (
                      <Markdown>{assistant.explanation}</Markdown>
                    )}
                    {assistant.unknowns.length > 0 && (
                      <details open>
                        <summary>尚需确认</summary>
                        <ul>
                          {assistant.unknowns.map((u, i) => (
                            <li key={i}>{u}</li>
                          ))}
                        </ul>
                      </details>
                    )}
                    <p className="assistant-provenance">
                      {assistant.saved
                        ? "关联已保存，可从文档跳回此控件"
                        : active
                          ? "本次讲解保留原有关联，未覆盖已保存的知识"
                          : assistant.confidence < 0.7
                            ? "匹配依据不足，未自动保存关联"
                            : "当前版本只读，讲解未写入"}
                    </p>
                    <h4>
                      相关原文 <span>{assistant.sections.length}</span>
                    </h4>
                    {assistant.sections.map((c) => (
                      <button
                        key={c.revision_id}
                        className="citation"
                        onClick={() =>
                          openDoc(
                            c,
                            assistant.binding_id
                              ? { id: assistant.binding_id }
                              : undefined,
                          )
                        }
                      >
                        <FileText size={18} />
                        <span>
                          {c.document_name}
                          <small>{c.name}</small>
                        </span>
                        <ArrowUpRight size={15} />
                      </button>
                    ))}
                    {assistant.sections.length > 0 && (
                      <details className="inline-preview" open={false}>
                        <summary>在小窗查看原文</summary>
                        {assistant.sections.map((c) => (
                          <div key={c.revision_id}>
                            <h4>{c.name}</h4>
                            <Markdown
                              documentId={
                                catalog?.documents.find(
                                  (d) =>
                                    d.revision_id === c.document_revision_id,
                                )?.id
                              }
                              revisionId={c.document_revision_id}
                            >
                              {c.content}
                            </Markdown>
                          </div>
                        ))}
                      </details>
                    )}
                  </section>
                )}
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
                ) : assistant ||
                  assisting ||
                  related ||
                  retrieving ? null : active && !active.excluded ? (
                  <>
                    <div className="knowledge-title">
                      <span className="eyebrow">
                        {active.origin === "model"
                          ? "AI 自动关联 · " + active.model_id
                          : "业务知识"}
                      </span>
                      <h2>{active.title}</h2>
                    </div>
                    {assistantMode === "explain" && (
                      <Markdown>{active.summary}</Markdown>
                    )}
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
                    <details
                      className="inline-preview"
                      open={assistantMode === "documents"}
                    >
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
                      {target?.selector === "body"
                        ? "这个模块还没有关联知识"
                        : target
                          ? "这个操作还没有关联知识"
                          : "边操作，边查看知识"}
                    </h3>
                    <p>
                      {target
                        ? target.selector === "body"
                          ? "AI 会检索模块职责、主要业务流程及相关文档；也可以右键页面空白处呼出。"
                          : "右键此控件，或按 Alt + A，让 AI 检索文档并讲解功能。"
                        : "鼠标指向按钮后，按 Alt + D 查看文档，按 Alt + A 呼出 AI 讲解，也可以右键选择。"}
                    </p>
                    {target && (
                      <button
                        className="primary"
                        onClick={() =>
                          control.current &&
                          void assist(control.current, "explain")
                        }
                      >
                        <Sparkles size={15} />
                        AI 自动关联并讲解
                      </button>
                    )}
                    <button disabled={!catalog?.editable} onClick={startPicker}>
                      <Plus size={15} />
                      手动补充关联
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
                    {target && (
                      <button
                        disabled={assisting}
                        onClick={() =>
                          control.current &&
                          void assist(control.current, "explain")
                        }
                      >
                        <Sparkles size={14} /> AI 讲解
                      </button>
                    )}
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

const root = createRoot(document.getElementById("root")!);
root.render(<App />);
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount());
