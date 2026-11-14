import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  LogOut,
  Sparkles,
  LoaderCircle,
  Settings2,
  Folders,
} from "lucide-react";
import {
  RichMarkdown as Markdown,
  MarkdownReadingContext,
  type ReadingContext,
} from "./RichMarkdown";
import { api, mutation, setCsrf } from "../../web/src/api";
import { OriginalRuntime, isTarget } from "./runtime";
import type { Target, RuntimeEvent, RuntimeHandle } from "./runtime";
import "./style.css";
import { AISettings } from "./AISettings";
import { Projects } from "./Projects";
import { DocumentCache } from "./DocumentCache";
import { KnowledgeChat, useKnowledgeChat } from "./KnowledgeChat";
import { Materials } from "./Materials";
import { BindingsManager } from "./BindingsManager";
import { useFloatingWindow, type ResizeDirection } from "./useFloatingWindow";
import { NativeDocumentPane, NativeNetworkSettings } from "./NativeDocument";
import type { NativeDocument, NativeLocation } from "./NativeDocument";
import {
  DocumentLibrary,
  firstNavigationDocument,
  type DocumentNavigation,
} from "./DocumentNavigation";

type DocRef = {
  id: string;
  revision_id: string;
  name: string;
  path: string;
  namespace?: string;
  package_id?: string | null;
  native_package_id?: string | null;
  native_index_modified?: boolean;
};
type Chapter = {
  id: string;
  revision_id: string;
  name: string;
  content: string;
  start_line?: number;
  end_line?: number;
  render_start_line?: number;
  heading_level?: number;
};
type Document = DocRef & {
  sections: Chapter[];
  content?: string;
  partial?: boolean;
  native?: NativeDocument | null;
};
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
  navigation?: DocumentNavigation;
  analysis_set_id: string;
  release_id: string | null;
  selection_version: number;
  editable: boolean;
  packages: {
    id: string;
    kind: string;
    name: string;
    sha256: string;
    package_key?: string;
    revision_id?: string;
    format?: string;
    native_index_modified?: boolean;
  }[];
  primary_package_id?: string;
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
  package: string;
  book: string;
  reader: string;
};
const getRoute = (): Route =>
  Object.fromEntries(
    [
      "project",
      "analysis",
      "release",
      "view",
      "doc",
      "section",
      "binding",
      "package",
      "book",
      "reader",
    ].map((k) => [k, new URLSearchParams(location.hash.slice(1)).get(k) || ""]),
  ) as Route;
const sameTarget = (a: Target, b: Target) =>
  a.route === b.route &&
  a.selector === b.selector &&
  a.label === b.label &&
  a.state === b.state &&
  JSON.stringify(a.frame_path) === JSON.stringify(b.frame_path);
const text = (e: unknown) => (e instanceof Error ? e.message : String(e));

function App() {
  const [auth, setAuth] = useState<boolean | null>(null),
    [accountId, setAccountId] = useState(""),
    [username, setUsername] = useState("owner"),
    [password, setPassword] = useState("");
  const [route, setRoute] = useState(getRoute),
    [workspaceReady, setWorkspaceReady] = useState(""),
    [loadedCatalog, setCatalog] = useState<Catalog | null>(null),
    [loadedBindings, setBindings] = useState<Binding[]>([]),
    [bindingsReady, setBindingsReady] = useState(false),
    [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const catalog =
    loadedCatalog &&
    (route.release
      ? loadedCatalog.release_id === route.release
      : !!route.analysis && loadedCatalog.analysis_set_id === route.analysis)
      ? loadedCatalog
      : null;
  const bindings = catalog ? loadedBindings : [];
  const documentRef = catalog?.documents.find(
    (doc) => doc.revision_id === route.doc,
  );
  const nativeRead =
    route.reader !== "standard" && !!documentRef?.native_package_id;
  const prototypePackages =
    catalog?.packages.filter((p) => p.kind === "prototype") || [];
  const p =
    prototypePackages.find(
      (p) => p.package_key === (route.package || catalog?.primary_package_id),
    ) || prototypePackages[0];
  const prefix = `/api/v3/projects/${route.project}/interactive`,
    scope = route.release
      ? `release_id=${encodeURIComponent(route.release)}`
      : `analysis_set_id=${encodeURIComponent(route.analysis)}`;
  const documentCache = useMemo(
    () =>
      new DocumentCache<Document>((id, native, signal) =>
        api<Document>(
          `${prefix}/documents/${encodeURIComponent(id)}?${scope}&native=${native}`,
          { signal },
        ),
      ),
    [accountId, auth, catalog, prefix, scope],
  );
  useEffect(() => () => documentCache.dispose(), [documentCache]);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [loadedDocument, setViewDoc] = useState<{
      cacheId: string;
      native: boolean;
      document: Document;
    } | null>(null),
    [documentError, setDocumentError] = useState(""),
    [documentRetry, setDocumentRetry] = useState(0),
    [docQuery, setDocQuery] = useState("");
  const viewDoc =
    catalog &&
    catalog.documents.some((document) => document.revision_id === route.doc)
      ? documentCache.peek(route.doc, nativeRead) ||
        (loadedDocument?.cacheId === documentCache.id &&
        loadedDocument.native === nativeRead &&
        loadedDocument.document.revision_id === route.doc
          ? loadedDocument.document
          : null)
      : null;
  const [bookMounted, setBookMounted] = useState(false);
  const [prototypeMounted, setPrototypeMounted] = useState(false);
  const [sourcePackage, setSourcePackage] = useState("");
  const [network, setNetwork] = useState<{
    key: string;
    origins: string[];
  } | null>(null);
  const pendingNativeLocation = useRef<NativeLocation | null>(null);
  const nativePackage = catalog?.packages.find((pack) =>
    documentRef
      ? pack.id === documentRef.native_package_id
      : pack.kind === "handbook" &&
        (!route.book ||
          pack.id === route.book ||
          pack.package_key === route.book) &&
        catalog.documents.some((doc) => doc.native_package_id === pack.id),
  );
  const nativeDetail =
    viewDoc?.native?.package_id === nativePackage?.id ? viewDoc?.native : null;
  const nativePackageModified = !!nativePackage?.native_index_modified;
  const originalBook =
    !!nativePackage &&
    route.reader !== "standard" &&
    ((!route.doc && (!nativePackageModified || route.reader === "source")) ||
      (nativeDetail
        ? nativeDetail.matches_index || route.reader === "source"
        : !viewDoc && sourcePackage === nativePackage.id));
  const networkKey = `${accountId}:${route.project}:${nativePackage?.package_key || nativePackage?.id || ""}`;
  const networkOrigins =
    network?.key === networkKey
      ? network.origins
      : [
          "https://api.openai.com",
          "https://api.deepseek.com",
          "https://dashscope.aliyuncs.com",
        ];
  const [target, setTarget] = useState<Target | null>(null),
    [active, setActive] = useState<Binding | null>(null),
    [knowledge, setKnowledge] = useState(false),
    [editing, setEditing] = useState(false),
    [picking, setPicking] = useState(false),
    [saving, setSaving] = useState(false),
    [pinned, setPinned] = useState(false),
    [docked, setDocked] = useState(false),
    [assistantOptions, setAssistantOptions] = useState(false);
  const [title, setTitle] = useState(""),
    [summary, setSummary] = useState(""),
    [editDoc, setEditDoc] = useState<Document | null>(null),
    [editDocId, setEditDocId] = useState(""),
    [sectionId, setSectionId] = useState(""),
    [popupQuery, setPopupQuery] = useState("");
  const [launcherPosition, setLauncherPosition] = useState<{
    left: number;
    top: number;
  } | null>(() => {
    try {
      const saved = JSON.parse(
        localStorage.getItem("vks.studio.assistant-launcher-position.v1") ||
          "null",
      );
      return saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)
        ? {
            left: Math.max(8, Math.min(innerWidth - 62, saved.left)),
            top: Math.max(8, Math.min(innerHeight - 62, saved.top)),
          }
        : null;
    } catch {
      return null;
    }
  });
  const [launcherDragging, setLauncherDragging] = useState(false);
  const launcherMoved = useRef(false),
    launcherCleanup = useRef<(() => void) | null>(null);
  const floating = useFloatingWindow(docked);
  const { ref: windowRef, setPosition } = floating;
  const prototype = useRef<RuntimeHandle | null>(null),
    pendingReveal = useRef<Target | null>(null),
    docEpoch = useRef(0),
    editEpoch = useRef(0),
    loadEpoch = useRef(0),
    selectedBinding = useRef(route.binding),
    assistantTrigger = useRef<HTMLButtonElement>(null);
  const [related, setRelated] = useState<Related | null>(null),
    [retrieving, setRetrieving] = useState(false);
  const [assistant, setAssistant] = useState<Assistance | null>(null),
    [assisting, setAssisting] = useState(false),
    [assistantMode, setAssistantMode] = useState<
      "documents" | "explain" | "chat"
    >("explain"),
    [assistantError, setAssistantError] = useState(""),
    [automatic, setAutomatic] = useState(
      () => localStorage.getItem("vks.studio.auto-associate") !== "off",
    ),
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
  const chat = useKnowledgeChat(
    JSON.stringify([
      accountId,
      auth,
      route.project,
      scope,
      p?.id,
      catalog?.selection_version,
      control.current && {
        target: control.current.target,
        context: control.current.context,
      },
    ]),
    prefix,
    route.release
      ? { release_id: route.release }
      : { analysis_set_id: route.analysis },
    p?.id || "",
    control.current,
  );
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
    api<{ authenticated: boolean; csrf_token?: string; actor_id?: string }>(
      "/auth/session",
    )
      .then((s) => {
        setCsrf(s.csrf_token);
        setAccountId(s.actor_id || "");
        setAuth(s.authenticated);
      })
      .catch(() => setAuth(false));
  }, []);
  const reload = useCallback(async () => {
    const epoch = ++loadEpoch.current;
    setBindingsReady(false);
    const [c, b] = await Promise.all([
      api<Catalog>(`${prefix}/catalog?${scope}`).then((catalog) => {
        if (epoch === loadEpoch.current) {
          // The original page can start loading as soon as its authorized
          // catalog is ready. Links may arrive later without blocking reading.
          setCatalog(catalog);
          setBindings([]);
        }
        return catalog;
      }),
      api<Binding[]>(`${prefix}/bindings?${scope}`),
    ]);
    if (epoch !== loadEpoch.current) return;
    setBindings(b);
    setBindingsReady(true);
    return { c, b };
  }, [prefix, scope]);
  useEffect(() => {
    if (!auth) return;
    let cancelled = false;
    api<{ id: string; name: string }[]>("/projects?limit=100")
      .then(async (p) => {
        if (cancelled) return;
        setProjects(p);
        if (!route.project && p[0]) navigate({ project: p[0].id }, true);
        else if (!route.project) navigate({ view: "projects" }, true);
        else if (route.project && !route.release) {
          const w = await api<{ analysis_set_id: string }>(
            `/api/v3/projects/${route.project}/materials/open`,
            mutation("POST", { analysis_set_id: route.analysis || null }),
          );
          if (!cancelled) {
            if (!route.analysis)
              navigate(
                {
                  analysis: w.analysis_set_id,
                  view: route.view || "prototype",
                },
                true,
              );
            else setWorkspaceReady(`${route.project}:${route.analysis}`);
          }
        }
      })
      .catch((e) => setError(text(e)));
    return () => {
      cancelled = true;
    };
  }, [auth, route.project, route.analysis, route.release, navigate]);
  useEffect(() => {
    if (!auth || !route.project || (!route.analysis && !route.release)) return;
    if (
      !route.release &&
      workspaceReady !== `${route.project}:${route.analysis}`
    )
      return;
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    control.current = null;
    moduleControl.current = null;
    setModuleTitle("");
    setDocQuery("");
    setPinned(false);
    setDocked(false);
    setAutomatic(
      (localStorage.getItem(`vks.studio.${route.project}.auto-associate`) ||
        localStorage.getItem("vks.studio.auto-associate")) !== "off",
    );
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssisting(false);
    setAssistantError("");
    setMenu(null);
    setKnowledge(false);
    setAssistantOptions(false);
    setCatalog(null);
    setBindings([]);
    setBindingsReady(false);
    setActive(null);
    setTarget(null);
    setEditing(false);
    setPicking(false);
    setViewDoc(null);
    setBookMounted(false);
    setPrototypeMounted(false);
    setSourcePackage("");
    pendingNativeLocation.current = null;
    setError("");
    void reload().catch((e) => setError(text(e)));
    return () => {
      loadEpoch.current++;
    };
  }, [
    auth,
    route.project,
    route.analysis,
    route.release,
    reload,
    workspaceReady,
  ]);
  useEffect(() => {
    if (route.view === "documents" && originalBook && nativePackage) {
      setBookMounted(true);
      setSourcePackage(nativePackage.id);
    }
  }, [route.view, originalBook, nativePackage?.id]);
  useEffect(() => {
    if ((route.view || "prototype") === "prototype" && p)
      setPrototypeMounted(true);
  }, [route.view, p?.id]);
  useEffect(() => {
    const reported = pendingNativeLocation.current;
    if (!reported || !nativeDetail || reported.path !== nativeDetail.path)
      return;
    pendingNativeLocation.current = null;
    const chapter = nativeDetail.sections.find(
      (section) => section.anchor === reported.anchor,
    );
    if (chapter && route.section !== chapter.revision_id)
      navigate({ section: chapter.revision_id }, true);
  }, [nativeDetail, route.section, navigate]);
  useEffect(() => {
    if (
      route.view === "documents" &&
      catalog &&
      route.doc &&
      !catalog.documents.some((doc) => doc.revision_id === route.doc)
    ) {
      navigate({ doc: "", section: "", reader: "", book: "" }, true);
      return;
    }
    if (
      route.view === "documents" &&
      catalog?.documents.length &&
      (route.reader === "standard" ||
        (nativePackageModified && route.reader !== "source") ||
        !catalog.documents.some((doc) => doc.native_package_id)) &&
      (!route.doc ||
        !catalog.documents.some(
          (document) => document.revision_id === route.doc,
        )) &&
      (route.release
        ? catalog.release_id === route.release
        : catalog.analysis_set_id === route.analysis)
    )
      navigate(
        {
          doc:
            nativePackageModified && route.reader !== "source"
              ? catalog.documents.find(
                  (doc) =>
                    doc.native_package_id === nativePackage?.id &&
                    doc.native_index_modified,
                )?.revision_id ||
                firstNavigationDocument(catalog.navigation, catalog.documents)
              : firstNavigationDocument(catalog.navigation, catalog.documents),
          section: "",
        },
        true,
      );
  }, [
    route.view,
    route.doc,
    route.reader,
    catalog,
    nativePackage?.id,
    nativePackageModified,
    navigate,
  ]);
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
      if (changed) setKnowledge(true);
    }
  }, [route.binding, bindings]);
  useEffect(() => {
    if (!catalog || !route.doc) return;
    const epoch = ++docEpoch.current;
    setDocumentError("");
    documentCache
      .load(route.doc, nativeRead)
      .then((d) => {
        if (epoch === docEpoch.current) {
          setViewDoc({
            cacheId: documentCache.id,
            native: nativeRead,
            document: d,
          });
        }
      })
      .catch((e) => {
        if (epoch === docEpoch.current) setDocumentError(text(e));
      });
    return () => {
      docEpoch.current++;
    };
  }, [documentCache, route.doc, nativeRead, documentRetry]);
  useEffect(() => {
    if (
      !viewDoc ||
      !route.section ||
      viewDoc.sections.some((section) => section.revision_id === route.section)
    )
      return;
    const slug = (value: string) =>
      value
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, "")
        .replace(/\s+/g, "-");
    const section = viewDoc.sections.find(
      (section) => slug(section.name) === route.section.toLowerCase(),
    );
    if (section) navigate({ section: section.revision_id }, true);
  }, [viewDoc, route.section, navigate]);
  useEffect(() => {
    if (viewDoc && route.view === "documents")
      requestAnimationFrame(() => {
        if (route.section)
          document
            .getElementById("section-" + route.section)
            ?.scrollIntoView({ block: "start", behavior: "instant" });
        else
          document
            .querySelector(".document-reader")
            ?.scrollTo({ top: 0, behavior: "instant" });
      });
  }, [viewDoc?.revision_id, route.section, route.view]);
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
      setLauncherPosition((p) =>
        p
          ? {
              left: Math.max(8, Math.min(innerWidth - 62, p.left)),
              top: Math.max(8, Math.min(innerHeight - 62, p.top)),
            }
          : null,
      );
    window.addEventListener("resize", clamp);
    return () => {
      window.removeEventListener("resize", clamp);
      launcherCleanup.current?.();
    };
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
      if (e.key === "Escape") setMenu(null);
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, []);
  function cancelAutomatic() {
    clearTimeout(automaticTimer.current);
    automaticTimer.current = undefined;
  }
  function automaticPreference(enabled: boolean) {
    setAutomatic(enabled);
    localStorage.setItem(
      `vks.studio.${route.project}.auto-associate`,
      enabled ? "on" : "off",
    );
    if (automaticTimer.current !== undefined) {
      cancelAutomatic();
      setAssisting(false);
    }
  }
  function collapseAssistant() {
    if (editing) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    setKnowledge(false);
    setAssistantOptions(false);
    requestAnimationFrame(() =>
      assistantTrigger.current?.focus({ preventScroll: true }),
    );
  }
  function toggleAssistant() {
    if (knowledge) {
      collapseAssistant();
      return;
    }
    setKnowledge(true);
    setAssistantOptions(false);
    if (!target && moduleControl.current)
      void assist(moduleControl.current, "documents", false, false);
  }
  function controlForTarget(t: Target, samePackage = true): Control {
    const current = samePackage ? control.current : null;
    if (current && sameTarget(current.target, t))
      return { ...current, target: t };
    const module = samePackage ? moduleControl.current : null;
    const samePage =
      module?.target.route === t.route &&
      JSON.stringify(module?.target.frame_path) ===
        JSON.stringify(t.frame_path);
    // Legacy action/picker events carry only a locator. Keep that exact target
    // and use only known page context; never reuse a previous control's popup
    // heading or nearby labels for a newly selected input.
    return {
      target: t,
      context: {
        scope: t.selector === "body" ? "module" : "control",
        page_title: samePage && module ? module.context.page_title : t.label,
        section_title: "",
        nearby_labels: [],
      },
    };
  }
  function chooseControl(c: Control) {
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    control.current = c;
    setAssistantMode((previous) => (previous === "chat" ? "chat" : "explain"));
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
  function switchKnowledge(mode: "documents" | "explain" | "chat") {
    setAssistantMode(mode);
    if (mode === "chat") {
      assistEpoch.current++;
      assistAbort.current?.abort();
      cancelAutomatic();
      setAssisting(false);
      setRetrieving(false);
      return;
    }
    if (!control.current) return;
    if (mode === "documents" && (related || active)) return;
    if (
      mode === "explain" &&
      (assistant || assisting || active?.origin === "model")
    )
      return;
    void assist(control.current, mode, active?.validity_status !== "valid");
  }
  async function assist(
    c: Control,
    mode: "documents" | "explain",
    refresh = false,
    revealPanel = true,
  ) {
    if (editing || !p) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    chooseControl(c);
    setAssistantMode((previous) =>
      !revealPanel && previous === "chat" ? "chat" : mode,
    );
    setMenu(null);
    if (revealPanel) {
      setKnowledge(true);
      setAssistantOptions(false);
    }
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
      existing.validity_status === "valid" &&
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
    let matches: Related | null =
      existing && existing.validity_status === "valid"
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
      automaticTimer.current = setTimeout(() => {
        automaticTimer.current = undefined;
        void runModel();
      }, 350);
    } else await runModel();
  }
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const s = await api<{ csrf_token: string; actor_id: string }>(
        "/auth/login",
        mutation("POST", { username, password }),
      );
      setCsrf(s.csrf_token);
      setAccountId(s.actor_id);
      setPassword("");
      setAuth(true);
    } catch (e) {
      setError(text(e));
    }
  }
  function onRuntime(event: RuntimeEvent) {
    if (event.type === "material.loaded" && picking)
      prototype.current?.send("picker.start");
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
          !bindings.some(
            (b) =>
              sameTarget(b.target, c.target) &&
              (b.excluded || b.validity_status === "valid"),
          )
        )
          void assist(c, "documents", false, false);
      }
      return;
    }
    if (event.type === "context.changed") {
      assistEpoch.current++;
      assistAbort.current?.abort();
      cancelAutomatic();
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
        !picking &&
        (!route.binding || moduleChanged) &&
        !pendingReveal.current
      ) {
        chooseControl(c.module);
        if (automatic && (route.view || "prototype") === "prototype")
          void assist(c.module, "documents", false, false);
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
      if (!checkBindingsReady()) {
        setPicking(false);
        return;
      }
      chooseControl(controlForTarget(t));
      setPicking(false);
      const b = bindings.find(
        (b) => b.package_id === p?.id && sameTarget(b.target, t),
      );
      setActive(b || null);
      setTitle(b?.title || t.label);
      setSummary(b?.summary || "");
      setEditDocId(b?.sections[0]?.document_revision_id || "");
      setSectionId(b?.sections[0]?.revision_id || "");
      setEditing(true);
      setKnowledge(true);
      setAssistantOptions(false);
      setNotice("已选中控件，请选择文档章节并保存说明");
    } else if (!pinned && !editing) {
      // Editable elements emit action.activated without control.activated.
      // Keep the displayed target and the assistant/chat context together.
      // Compare the synchronous ref so a richer preceding event is preserved.
      if (!control.current || !sameTarget(control.current.target, t))
        chooseControl(controlForTarget(t));
      else {
        control.current = controlForTarget(t);
        setTarget(t);
        setActive(
          bindings.find(
            (b) =>
              b.package_id === p?.id && sameTarget(b.target, t) && !b.excluded,
          ) || null,
        );
      }
    }
  }
  function checkBindingsReady() {
    if (bindingsReady) return true;
    setNotice("正在加载关联，请稍后再编辑");
    return false;
  }
  function startPicker() {
    if (!checkBindingsReady()) return;
    if (editing) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    setAssisting(false);
    setRetrieving(false);
    setPicking(true);
    pendingReveal.current = null;
    setAssistantOptions(false);
    setKnowledge(false);
    prototype.current?.send("picker.start");
    navigate({ view: "prototype" });
    setNotice("请点击需要关联的原型控件；按 Esc 或点击取消结束");
  }
  const openDoc = useCallback(
    (c: Citation, b?: Pick<Binding, "id">) => {
      pendingNativeLocation.current = null;
      navigate({
        view: "documents",
        doc: c.document_revision_id,
        section: c.revision_id,
        binding: b?.id || "",
        reader: "",
      });
      setKnowledge(false);
    },
    [navigate],
  );
  function reveal(b: Binding) {
    chooseControl({
      target: b.target,
      context: {
        scope: b.target.selector === "body" ? "module" : "control",
        page_title: b.title,
        section_title: "",
        nearby_labels: [],
      },
    });
    setEditing(false);
    setKnowledge(true);
    setAssistantOptions(false);
    setActive(b);
    const destination = catalog?.packages.find((p) => p.id === b.package_id);
    navigate({
      view: "prototype",
      binding: b.id,
      package: destination?.package_key || "",
    });
    if (prototype.current && p?.id === b.package_id)
      prototype.current.send("target.reveal", { target: b.target });
    else pendingReveal.current = b.target;
  }
  function editBinding(b: Binding) {
    if (!checkBindingsReady()) return;
    const destination = catalog?.packages.find((p) => p.id === b.package_id);
    if (destination) navigate({ package: destination.package_key || "" });
    assistEpoch.current++;
    assistAbort.current?.abort();
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssistantError("");
    setAssisting(false);
    control.current = controlForTarget(b.target, b.package_id === p?.id);
    setActive(b);
    setTarget(b.target);
    setTitle(b.title);
    setSummary(b.summary);
    setEditDocId(b.sections[0]?.document_revision_id || "");
    setSectionId(b.sections[0]?.revision_id || "");
    setEditing(true);
    setKnowledge(true);
    setAssistantOptions(false);
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
  function drag(e: React.PointerEvent<HTMLElement>) {
    if ((e.target as HTMLElement).closest("button") || docked) return;
    floating.begin(e, "move");
  }
  function dragLauncher(e: React.PointerEvent<HTMLButtonElement>) {
    if (e.button !== 0 || !e.isPrimary) return;
    launcherCleanup.current?.();
    launcherMoved.current = false;
    const button = e.currentTarget,
      rect = button.getBoundingClientRect();
    const x = e.clientX,
      y = e.clientY,
      pointer = e.pointerId;
    let next = { left: rect.left, top: rect.top };
    let shield: HTMLDivElement | undefined;
    button.setPointerCapture(pointer);
    const move = (event: PointerEvent) => {
      if (
        event.pointerId !== pointer ||
        (Math.hypot(event.clientX - x, event.clientY - y) < 5 &&
          !launcherMoved.current)
      )
        return;
      if (!launcherMoved.current) {
        shield = document.createElement("div");
        shield.className = "assistant-drag-shield";
        shield.setAttribute("aria-hidden", "true");
        document.body.append(shield);
      }
      event.preventDefault();
      launcherMoved.current = true;
      setLauncherDragging(true);
      next = {
        left: Math.max(
          8,
          Math.min(innerWidth - rect.width - 8, rect.left + event.clientX - x),
        ),
        top: Math.max(
          8,
          Math.min(innerHeight - rect.height - 8, rect.top + event.clientY - y),
        ),
      };
      setLauncherPosition(next);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", end, true);
      window.removeEventListener("pointercancel", end, true);
      window.removeEventListener("blur", end);
      shield?.remove();
      launcherCleanup.current = null;
    };
    const end = (event: Event) => {
      if ("pointerId" in event && event.pointerId !== pointer) return;
      cleanup();
      setLauncherDragging(false);
      if (launcherMoved.current) {
        try {
          localStorage.setItem(
            "vks.studio.assistant-launcher-position.v1",
            JSON.stringify(next),
          );
        } catch {
          /* Optional UI preference. */
        }
      }
      if (event.type === "pointercancel") launcherMoved.current = false;
    };
    launcherCleanup.current = cleanup;
    window.addEventListener("pointermove", move, {
      capture: true,
      passive: false,
    });
    window.addEventListener("pointerup", end, true);
    window.addEventListener("pointercancel", end, true);
    window.addEventListener("blur", end);
  }
  const markdownReading = useMemo<ReadingContext>(
    () => ({
      projectId: route.project,
      analysisSetId: route.release ? undefined : route.analysis,
      releaseId: route.release || undefined,
      selectionVersion: catalog?.selection_version,
      documents: catalog?.documents || [],
      onDocument: (document, fragment) => {
        const section =
          document.revision_id === viewDoc?.revision_id
            ? viewDoc.sections.find(
                (s) =>
                  s.revision_id === fragment ||
                  s.name
                    .toLowerCase()
                    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
                    .replace(/\s+/g, "-") === fragment.toLowerCase(),
              )
            : undefined;
        navigate({
          view: "documents",
          doc: document.revision_id,
          section: section?.revision_id || fragment,
        });
      },
      onSection: (reference) => openDoc(reference),
      loadDocument: async (revision, signal) => {
        if (signal.aborted) throw new DOMException("Aborted", "AbortError");
        const document = await documentCache.load(revision);
        return (
          document.content ??
          document.sections.map((section) => section.content).join("\n\n")
        );
      },
    }),
    [
      route.project,
      route.analysis,
      route.release,
      catalog?.selection_version,
      catalog?.documents,
      viewDoc,
      documentCache,
      navigate,
      openDoc,
    ],
  );
  const documentArticle = useMemo(
    () =>
      !originalBook && route.view === "documents" ? (
        <article className="document-reader">
          <div className="reader-label">
            工作区文档 <span>· 准确修订</span>
          </div>
          <h1>
            {viewDoc?.name || documentRef?.name || "选择一篇文档开始阅读"}
          </h1>
          {!viewDoc &&
            documentRef &&
            (documentError ? (
              <div role="alert">
                <p>{documentError}</p>
                <button onClick={() => setDocumentRetry((v) => v + 1)}>
                  重新加载文档
                </button>
              </div>
            ) : (
              <p role="status" className="document-loading">
                <LoaderCircle size={18} className="spinning" />
                正在读取正文…
              </p>
            ))}
          {viewDoc &&
            viewDoc.sections.some((section) => section.heading_level !== 0) && (
              <details
                className="article-toc"
                key={viewDoc.revision_id}
                open={viewDoc.sections.length <= 6}
              >
                <summary>
                  <b>本篇目录</b>
                  <span>
                    {
                      viewDoc.sections.filter(
                        (section) => section.heading_level !== 0,
                      ).length
                    }{" "}
                    个章节
                  </span>
                </summary>
                <nav aria-label="本篇目录">
                  {viewDoc.sections.map((s, index) => {
                    if (s.heading_level === 0) return null;
                    const depth =
                      /^ {0,3}(#{1,6})\s/.exec(s.content)?.[1].length || 1;
                    if (index === 0 && depth === 1 && s.name === viewDoc.name)
                      return null;
                    return (
                      <a
                        key={s.id}
                        href={`#section-${s.revision_id}`}
                        style={{
                          paddingInlineStart: Math.max(0, depth - 2) * 14,
                        }}
                        onClick={(e) => {
                          e.preventDefault();
                          navigate({ section: s.revision_id });
                        }}
                      >
                        {s.name}
                      </a>
                    );
                  })}
                </nav>
              </details>
            )}
          {viewDoc && (
            <Markdown
              documentId={viewDoc.id}
              revisionId={viewDoc.revision_id}
              omitInitialTitle={viewDoc.name}
              sections={(() => {
                let line = 1;
                return viewDoc.sections.map((section) => {
                  const current = {
                    ...section,
                    render_start_line:
                      section.render_start_line ??
                      (viewDoc.content ? section.start_line : undefined) ??
                      line,
                  };
                  line += section.content.split(/\r\n?|\n/).length + 1;
                  return current;
                });
              })()}
              selectedSection={route.section}
              sectionActions={(s, position) =>
                position === "before" ? (
                  s.content.trim() === "# " + viewDoc.name ? null : (
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
                  )
                ) : (
                  bindings
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
                    ))
                )
              }
            >
              {viewDoc.content ??
                viewDoc.sections.map((s) => s.content).join("\n\n")}
            </Markdown>
          )}
        </article>
      ) : null,
    [
      originalBook,
      route.view,
      viewDoc,
      documentRef?.name,
      documentError,
      route.section,
      bindings,
      catalog,
      p?.id,
      navigate,
    ],
  );
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
  const handbookPackages =
    catalog?.packages.filter(
      (p) =>
        p.kind === "handbook" &&
        ["charge-handbook", "html-document", "static-site"].includes(
          p.format || "",
        ),
    ) || [];
  function openProject(id: string) {
    navigate({
      project: id,
      analysis: "",
      release: "",
      doc: "",
      section: "",
      binding: "",
      package: "",
      book: "",
      reader: "",
      view: "prototype",
    });
  }
  async function materialsChanged(result: { analysis_set_id: string }) {
    if (
      getRoute().project !== route.project ||
      getRoute().analysis !== route.analysis
    )
      return;
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    setKnowledge(false);
    setAssistantOptions(false);
    setActive(null);
    setTarget(null);
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssisting(false);
    control.current = null;
    navigate({
      analysis: result.analysis_set_id,
      release: "",
      binding: "",
      doc: "",
      section: "",
      package: "",
      book: "",
      reader: "",
    });
    if (result.analysis_set_id === route.analysis) await reload();
  }
  function refreshProjects() {
    void api<{ id: string; name: string }[]>("/projects?limit=100")
      .then(setProjects)
      .catch((e) => setError(text(e)));
  }
  const currentView = route.view || "prototype";
  function nativeLocation(location: NativeLocation) {
    if (!nativePackage || !originalBook || currentView !== "documents") return;
    const candidates =
      catalog?.documents.filter(
        (doc) => doc.native_package_id === nativePackage.id,
      ) || [];
    const document =
      candidates.find(
        (doc) =>
          doc.path.replace(/^\/+/, "") === location.path.replace(/^\/+/, ""),
      ) ||
      (nativePackage.format === "html-document" &&
      !location.path.replace(/^\/+/, "") &&
      candidates.length === 1
        ? candidates[0]
        : undefined);
    if (!document) return;
    // Native events are positions, never authority to load another package or
    // request model work. Resolve them only through the authorized catalog.
    pendingNativeLocation.current = { ...location, path: document.path };
    const chapter =
      document.revision_id === viewDoc?.revision_id
        ? nativeDetail?.sections.find(
            (section) => section.anchor === location.anchor,
          )
        : null;
    if (
      document.revision_id !== route.doc ||
      (chapter && chapter.revision_id !== route.section)
    )
      navigate(
        {
          doc: document.revision_id,
          section: chapter?.revision_id || "",
          reader: "source",
        },
        true,
      );
  }
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

  const body = (
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
        <button
          className="project-center-trigger"
          aria-label="项目中心"
          onClick={() => navigate({ view: "projects" })}
          disabled={editing}
        >
          <Folders size={18} />
        </button>
        <span className="top-divider" />
        <select
          aria-label="当前项目"
          disabled={editing}
          value={route.project}
          onChange={(e) => openProject(e.target.value)}
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
            ["materials", "资料管理"],
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
                if (
                  v === "documents" &&
                  !route.doc &&
                  catalog?.documents[0] &&
                  (route.reader === "standard" ||
                    !catalog.documents.some((doc) => doc.native_package_id))
                )
                  navigate({
                    view: v,
                    doc: firstNavigationDocument(
                      catalog.navigation,
                      catalog.documents,
                    ),
                  });
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
            aria-label="退出登录"
            className="icon"
            onClick={() =>
              void api("/auth/session", mutation("DELETE", {})).finally(() => {
                setAuth(false);
                setAccountId("");
                setCatalog(null);
                setBindingsReady(false);
                setProjects([]);
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
                  : currentView === "materials"
                    ? "资料管理"
                    : currentView === "projects"
                      ? "项目中心"
                      : "关联管理"}
          </span>
          <ChevronRight size={13} />
          <span>
            {currentView === "prototype"
              ? target?.label || "在原页面操作，即可查看相关知识"
              : currentView === "documents"
                ? viewDoc?.name ||
                  (originalBook ? nativePackage?.name : "独立阅读与章节关联")
                : currentView === "ai"
                  ? "配置本项目的模型连接"
                  : currentView === "materials"
                    ? "安装、更新与恢复项目资料"
                    : currentView === "projects"
                      ? "每个项目独立维护原型与知识"
                      : `${bindings.filter((b) => !b.excluded).length} 条有效关联`}
          </span>
        </div>
        <div>
          {currentView === "prototype" && prototypePackages.length > 1 && (
            <select
              aria-label="当前原型包"
              value={p?.package_key || p?.id}
              onChange={(e) =>
                navigate({ package: e.target.value, binding: "" })
              }
            >
              {prototypePackages.map((p) => (
                <option key={p.id} value={p.package_key || p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          {currentView === "documents" && handbookPackages.length > 1 && (
            <select
              aria-label="原始文档包"
              value={nativePackage?.id || ""}
              onChange={(e) => {
                navigate({
                  book: e.target.value,
                  doc: "",
                  section: "",
                  reader: "source",
                });
              }}
            >
              {!nativePackage && <option value="">选择原样资料包</option>}
              {handbookPackages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          {currentView === "prototype" && picking && (
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
          )}
          {currentView === "documents" && nativePackage && (
            <button
              onClick={() => {
                navigate({ reader: originalBook ? "standard" : "source" });
              }}
            >
              <FileText size={14} />
              {originalBook
                ? "标准阅读"
                : nativeDetail && !nativeDetail.matches_index
                  ? "查看导入原件"
                  : "原样文档"}
            </button>
          )}
          {currentView === "documents" && nativePackage && accountId && (
            <NativeNetworkSettings
              account={accountId}
              project={route.project}
              packageId={nativePackage.package_key || nativePackage.id}
              onOrigins={(origins) => setNetwork({ key: networkKey, origins })}
            />
          )}
          {currentView === "documents" && originalBook && (
            <button onClick={toggleAssistant}>
              <Link2 size={14} />
              关联知识
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
      {currentView === "documents" &&
        nativeDetail &&
        !nativeDetail.matches_index && (
          <div className="banner native-source-notice" role="status">
            {originalBook
              ? "正在查看导入原件；工作区正文已有修改，准确关联与最新内容请使用标准阅读。"
              : "工作区正文已有修改，当前显示准确修订。可选择“查看导入原件”阅读来源页面。"}
          </div>
        )}
      {currentView === "documents" &&
        originalBook &&
        !nativeDetail &&
        nativePackageModified && (
          <div className="banner native-source-notice" role="status">
            正在查看导入原件；此资料包的工作区正文已有修改，最新内容与准确关联请使用标准阅读。
          </div>
        )}
      {notice && catalog && (
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
              {(currentView === "prototype" || prototypeMounted) && (
                <OriginalRuntime
                  key={p.id + scope}
                  url={`${prefix}/packages/${p.id}/content?${scope}`}
                  digest={p.sha256}
                  kind="prototype"
                  storageScope={{
                    accountId,
                    projectId: route.project,
                    packageId: p.package_key || p.id,
                  }}
                  handle={prototype}
                  onEvent={onRuntime}
                />
              )}
            </>
          ) : (
            <div className="empty">
              <BookOpen />
              <h2>
                {catalog ? "当前版本没有可运行的原型包" : "正在打开项目…"}
              </h2>
              <p>为这个项目导入 HTML 原型，保留菜单、表单和弹窗交互。</p>
              {catalog && (
                <button
                  className="primary"
                  onClick={() => navigate({ view: "materials" })}
                >
                  导入项目资料
                </button>
              )}
            </div>
          )}
        </section>
        {currentView === "documents" && (
          <DocumentLibrary
            key={documentCache.id}
            navigation={catalog?.navigation}
            documents={catalog?.documents || []}
            selectedDocument={route.doc}
            selectedSection={route.section}
            currentDocument={viewDoc}
            scopeKey={route.project + ":" + scope}
            selectionVersion={catalog?.selection_version}
            query={docQuery}
            onQuery={setDocQuery}
            onOpen={(revision, section = "") => {
              navigate({
                view: "documents",
                doc: revision,
                section,
                reader: "standard",
              });
            }}
            loadDocument={(revision) => documentCache.load(revision)}
            linkedDocuments={bindings
              .filter((binding) => !binding.excluded)
              .flatMap((binding) =>
                binding.sections.map((section) => section.document_revision_id),
              )}
            hidden={originalBook}
          >
            {documentArticle}
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
          </DocumentLibrary>
        )}
        {nativePackage &&
          accountId &&
          (bookMounted || (currentView === "documents" && originalBook)) && (
            <NativeDocumentPane
              key={nativePackage.id + scope}
              pack={nativePackage}
              native={nativeDetail || null}
              section={route.section}
              account={accountId}
              project={route.project}
              contentUrl={`${prefix}/packages/${nativePackage.id}/content?${scope}`}
              origins={networkOrigins}
              visible={currentView === "documents" && originalBook}
              requestKey={`${route.project}:${scope}:${route.doc}:${route.section}:${route.view}:${route.reader}`}
              onLocation={nativeLocation}
              onFailure={(message) => {
                setNotice(message);
                navigate({ reader: "standard" }, true);
              }}
            />
          )}
        {currentView === "projects" && (
          <Projects onOpen={openProject} onUpdate={refreshProjects} />
        )}
        {currentView === "materials" && route.project && route.analysis && (
          <Materials
            key={route.project + route.analysis}
            project={route.project}
            analysis={route.analysis}
            onChanged={materialsChanged}
            onAI={() => navigate({ view: "ai" })}
          />
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
          <BindingsManager
            key={route.project + scope}
            bindings={bindings}
            ready={bindingsReady}
            editable={!!catalog?.editable}
            canCreate={!!p}
            onCreate={startPicker}
            onReveal={reveal}
            onEdit={editBinding}
            onToggle={(b) => void exclude(b)}
            onSource={(b, i) => openDoc(b.sections[i], b)}
            renderSummary={(b) => (
              <Markdown references={b.sections}>{b.summary}</Markdown>
            )}
          />
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
            查看关联文档
          </button>
          <button role="menuitem" onClick={() => void assist(menu, "explain")}>
            <Sparkles size={16} />
            AI 讲解此功能
          </button>
          <button
            role="menuitem"
            onClick={() => {
              chooseControl(menu);
              setMenu(null);
              setAssistantMode("chat");
              setKnowledge(true);
              setAssistantOptions(false);
            }}
          >
            <Sparkles size={16} />向 AI 提问
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
            disabled={!catalog?.editable || !bindingsReady}
            onClick={() => {
              if (!checkBindingsReady()) return;
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
                setAssistantOptions(false);
              }
            }}
          >
            手动补充关联
          </button>
        </div>
      )}
      {catalog &&
        ["prototype", "documents", "links"].includes(currentView) &&
        !(currentView === "documents" && originalBook) &&
        !(knowledge && docked) && (
          <button
            ref={assistantTrigger}
            className={
              "assistant-launcher" +
              (knowledge ? " expanded" : "") +
              (launcherDragging ? " dragging" : "")
            }
            style={
              launcherPosition
                ? {
                    left: launcherPosition.left,
                    top: launcherPosition.top,
                    right: "auto",
                    bottom: "auto",
                  }
                : undefined
            }
            aria-label={knowledge ? "收起知识助手" : "打开知识助手"}
            aria-expanded={knowledge}
            aria-controls="knowledge-assistant"
            onPointerDown={dragLauncher}
            onClick={(e) => {
              if (launcherMoved.current && e.detail !== 0) {
                launcherMoved.current = false;
                e.preventDefault();
                return;
              }
              launcherMoved.current = false;
              toggleAssistant();
            }}
            title={retrieving || assisting ? "正在整理相关知识" : "知识助手"}
          >
            {knowledge ? <X size={23} /> : <Sparkles size={24} />}
            {!knowledge &&
              (related?.sections.length ||
                active?.sections.length ||
                assistant?.sections.length ||
                0) > 0 && (
                <span className="assistant-badge">
                  {Math.min(
                    9,
                    related?.sections.length ||
                      active?.sections.length ||
                      assistant?.sections.length ||
                      0,
                  )}
                </span>
              )}
            {!knowledge && (retrieving || assisting) && (
              <span className="assistant-activity" aria-label="正在整理知识" />
            )}
            <span className="assistant-launcher-label">
              {knowledge ? "收起助手" : "知识助手"}
            </span>
          </button>
        )}
      {knowledge && catalog && currentView !== "ai" && (
        <aside
          ref={windowRef}
          id="knowledge-assistant"
          aria-label="知识助手"
          className={"knowledge-window " + (docked ? "docked" : "")}
          style={floating.style}
          onKeyDown={(e) => {
            if (e.key === "Escape" && !editing) {
              e.stopPropagation();
              if (assistantOptions) setAssistantOptions(false);
              else collapseAssistant();
            }
          }}
        >
          <div className="knowledge-header" onPointerDown={drag}>
            <div>
              <Sparkles size={17} />
              <b>{editing ? "补充关联" : "知识助手"}</b>
              <GripHorizontal size={14} />
            </div>
            <div>
              <button
                className={"icon " + (assistantOptions ? "enabled" : "")}
                aria-label="助手选项"
                aria-pressed={assistantOptions}
                disabled={editing}
                onClick={() => setAssistantOptions(!assistantOptions)}
              >
                <Settings2 size={16} />
              </button>
              <button
                className="icon"
                aria-label="收起助手面板"
                onClick={collapseAssistant}
              >
                <X size={16} />
              </button>
            </div>
          </div>
          {assistantOptions && (
            <section className="assistant-options" aria-label="助手选项">
              <div className="assistant-options-heading">
                <span className="eyebrow">偏好与工具</span>
                <h3>让助手按你的方式工作</h3>
              </div>
              <button
                className="assistant-option-link"
                aria-label="模块知识"
                disabled={!moduleTitle}
                onClick={() =>
                  prototype.current?.send("knowledge.request", {
                    scope: "module",
                    mode: "documents",
                  })
                }
              >
                <BookOpen size={18} />
                <span>
                  <b>模块知识</b>
                  <small>
                    {moduleTitle
                      ? moduleTitle.split(" · ")[0]
                      : "等待当前模块加载"}
                  </small>
                </span>
                <ChevronRight size={16} />
              </button>
              <label className="assistant-option-toggle">
                <span>
                  <b>自动关联模块与控件</b>
                  <small>在后台整理，收起时以图标提示</small>
                </span>
                <input
                  type="checkbox"
                  aria-label="自动关联模块与控件"
                  checked={automatic}
                  onChange={(e) => automaticPreference(e.target.checked)}
                />
              </label>
              <label className="assistant-option-toggle">
                <span>
                  <b>
                    <Pin size={13} /> 固定当前知识
                  </b>
                  <small>保留当前内容，暂停跟随页面操作</small>
                </span>
                <input
                  type="checkbox"
                  aria-label="固定当前知识"
                  checked={pinned}
                  onChange={(e) => setPinned(e.target.checked)}
                />
              </label>
              <div className="assistant-layout">
                <span>展开方式</span>
                <div>
                  <button
                    aria-pressed={!docked}
                    onClick={() => {
                      setDocked(false);
                      setPosition(null);
                    }}
                  >
                    浮动小窗
                  </button>
                  <button
                    aria-pressed={docked}
                    onClick={() => {
                      setDocked(true);
                      setPosition(null);
                    }}
                  >
                    <PanelRight size={14} />
                    侧边展开
                  </button>
                </div>
              </div>
              <button
                className="assistant-options-back"
                onClick={() => {
                  floating.reset();
                  setDocked(false);
                }}
              >
                恢复默认位置与大小
              </button>
              <div className="assistant-option-tools">
                <button
                  onClick={() => {
                    navigate({ view: "ai" });
                    collapseAssistant();
                  }}
                >
                  <Settings2 size={16} />
                  模型与知识库设置
                  <ChevronRight size={15} />
                </button>
                <button
                  onClick={() => {
                    navigate({ view: "links" });
                    collapseAssistant();
                  }}
                >
                  <Link2 size={16} />
                  关联管理
                  <ChevronRight size={15} />
                </button>
                <button
                  disabled={!catalog?.editable || !p || !bindingsReady}
                  onClick={startPicker}
                >
                  <MousePointer2 size={16} />
                  手动补充关联
                  <ChevronRight size={15} />
                </button>
              </div>
              <p className="assistant-option-help">
                操作原型时，助手会跟随当前模块和按钮。右键页面或控件，可直接查看文档或呼出
                AI 讲解。
              </p>
              <button
                className="assistant-options-back"
                onClick={() => setAssistantOptions(false)}
              >
                返回知识内容
                <ChevronRight size={14} />
              </button>
            </section>
          )}
          {!assistantOptions && (
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
                  <button
                    role="tab"
                    aria-selected={assistantMode === "chat"}
                    onClick={() => switchKnowledge("chat")}
                  >
                    AI 问答
                  </button>
                </div>
              )}
              <div
                className={
                  "knowledge-body" +
                  (assistantMode === "chat" && !editing ? " chat-body" : "")
                }
              >
                {!editing && assistantMode === "chat" && target && (
                  <KnowledgeChat
                    chat={chat}
                    label={target.label}
                    onCitation={openDoc}
                    onSettings={() => navigate({ view: "ai" })}
                  />
                )}
                <div hidden={!editing && assistantMode === "chat"}>
                  {!editing && active && active.validity_status !== "valid" && (
                    <p className="assistant-background">
                      资料已更新，原有关联正在重新核对；可先查看检索结果。
                    </p>
                  )}
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
                        {related.sections.length > 0 && (
                          <details
                            className="inline-preview"
                            open={assistantMode === "documents"}
                          >
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
                                        d.revision_id ===
                                        c.document_revision_id,
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
                        <Markdown references={assistant.sections}>
                          {assistant.explanation}
                        </Markdown>
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
                    retrieving ? null : active &&
                    !active.excluded &&
                    active.validity_status === "valid" ? (
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
                        <Markdown references={active.sections}>
                          {active.summary}
                        </Markdown>
                      )}
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
                            : "右键这个控件，或点击 AI 讲解，查看相关文档与功能说明。"
                          : "在原型中打开模块或点击按钮，助手会自动整理相关知识。也可以右键选择查看文档或 AI 讲解。"}
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
                    </div>
                  )}
                </div>
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
                    {target && assistantMode !== "chat" && (
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
                        disabled={!catalog?.editable || !bindingsReady}
                        onClick={() => editBinding(active)}
                      >
                        编辑知识
                      </button>
                    )}
                  </>
                )}
              </footer>
            </>
          )}
          {!docked &&
            (
              ["n", "s", "e", "w", "nw", "ne", "sw", "se"] as ResizeDirection[]
            ).map((direction) => (
              <button
                key={direction}
                type="button"
                className={"knowledge-resize knowledge-resize-" + direction}
                tabIndex={direction === "se" ? 0 : -1}
                aria-label={
                  direction === "se"
                    ? "调整助手大小"
                    : `调整助手大小 ${direction}`
                }
                title="按住拖动调整大小，或使用方向键"
                onPointerDown={(event) => floating.begin(event, direction)}
                onKeyDown={(event) => {
                  const delta: Record<string, [number, number]> = {
                    ArrowLeft: [-20, 0],
                    ArrowRight: [20, 0],
                    ArrowUp: [0, -20],
                    ArrowDown: [0, 20],
                  };
                  if (delta[event.key]) {
                    event.preventDefault();
                    floating.keyboardResize(...delta[event.key]);
                  }
                }}
              />
            ))}
        </aside>
      )}
    </div>
  );
  return (
    <MarkdownReadingContext.Provider value={markdownReading}>
      {body}
    </MarkdownReadingContext.Provider>
  );
}

const root = createRoot(document.getElementById("root")!);
root.render(<App />);
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount());
