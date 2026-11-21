import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DetachedPortal, useDetachedAssistant } from "./DetachedAssistant";
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
  MessageSquareText,
  LoaderCircle,
  Settings2,
  Folders,
  Copy,
} from "lucide-react";
import {
  RichMarkdown as Markdown,
  MarkdownReadingContext,
  type MarkdownSection,
  type ReadingContext,
} from "./RichMarkdown";
import { api, mutation, observeUnauthenticated, setCsrf } from "../../web/src/api";
import { OriginalRuntime, isTarget, samePage } from "./runtime";
import type {
  Target,
  RuntimeEvent,
  RuntimeHandle,
  PrototypePosition,
  RevealResult,
} from "./runtime";
import "./style.css";
import { AISettings } from "./AISettings";
import { Projects } from "./Projects";
import { DocumentCache } from "./DocumentCache";
import {
  KnowledgeChat,
  SourcePreview,
  useKnowledgeChat,
} from "./KnowledgeChat";
import { DocumentAssistant } from "./DocumentAssistant";
import { chatSourceFingerprint } from "./chatSourceFingerprint";
import { Materials } from "./Materials";
import { BindingsManager } from "./BindingsManager";
import { BatchAssociations } from "./BatchAssociations";
import { ProjectTokenUsage } from "./ProjectTokenUsage";
import { PrototypePreviewAction } from "./PrototypePreviewAction";
import { AssociationReview, type ReviewTarget } from "./AssociationReview";
import { BatchExplanations } from "./BatchExplanations";
import {
  DocumentAssociation,
  type AssociationChapter,
} from "./DocumentAssociation";
import {
  DocumentPrototypeEntries,
  documentPrototypeEntries,
} from "./DocumentPrototypeEntries";
import { ReferenceDisclosure } from "./ReferenceDisclosure";
import {
  HandbookTemplate,
  type HandbookSearchResult,
} from "./HandbookTemplate";
import type { Presentation } from "./DocumentAppearance";
import { useFloatingWindow, type ResizeDirection } from "./useFloatingWindow";
import { useTransientNotice } from "./useTransientNotice";
import { NativeDocumentPane, NativeNetworkSettings } from "./NativeDocument";
import type { NativeDocument, NativeLocation } from "./NativeDocument";
import {
  DocumentLibrary,
  firstNavigationDocument,
  type DocumentNavigation,
} from "./DocumentNavigation";
import {
  exactLocationUrl,
  getRoute,
  sameWorkspace,
  useStudioNavigation,
  type Route,
} from "./studioNavigation";

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
  preview_id?: string | null;
  can_confirm?: boolean;
};
type Related = {
  title: string;
  sections: Citation[];
  related_sections?: Citation[];
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
  source_fingerprint?: string;
  presentation?: Presentation;
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
type RegisteredPage = Pick<Target, "route" | "frame_path"> & {
  package_artifact_id: string;
  page_ref: string;
};
type ReadingPosition = {
  route: Route;
  top: number;
  chapter: string;
  chapterOffset?: number;
};
const documentScrollRoot = (reader: HTMLElement | null) =>
  reader?.closest<HTMLElement>(".hb-main") ?? reader;
const sameTarget = (a: Target, b: Target) =>
  samePage(a, b) &&
  a.selector === b.selector &&
  a.label === b.label &&
  a.state === b.state;
const bindingForTarget = (
  bindings: Binding[],
  packageId: string | undefined,
  target: Target,
) =>
  bindings.find(
    (binding) =>
      binding.package_id === packageId && sameTarget(binding.target, target),
  );
const text = (e: unknown) => (e instanceof Error ? e.message : String(e));
const missingLocation = "当前版本中无法读取该位置";

function App() {
  const navigation = useStudioNavigation();
  const { route, hash, external, invalid, navigate, retry } = navigation;
  const [accountId, setAccountId] = useState("");
  const [sessionGeneration, setSessionGeneration] = useState(0);
  const [checking, setChecking] = useState(true);
  const [signingIn, setSigningIn] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [username, setUsername] = useState("owner");
  const [password, setPassword] = useState("");
  const activeSession = useRef({ accountId: "", csrf: "" });
  const authRequest = useRef<AbortController | null>(null);
  const checkedHash = useRef("");
  const exactSavedTarget = !!route.release && (
    route.view === "documents" && !!route.doc ||
    (!route.view || route.view === "prototype") &&
      !!(route.binding || route.package || route.page_ref)
  );
  const checkDestination = !!accountId && !invalid && external &&
    exactSavedTarget && checkedHash.current !== hash;

  const updateSession = useCallback((actorId: string, value?: string) => {
    const csrf = actorId && value ? value : "";
    if (activeSession.current.accountId !== actorId || activeSession.current.csrf !== csrf) {
      retry();
      setSessionGeneration((generation) => generation + 1);
    }
    activeSession.current = { accountId: actorId, csrf };
    setCsrf(csrf);
    setAccountId(actorId);
  }, [retry]);

  const checkSession = useCallback(async (afterUnauthorized = false) => {
    if (authRequest.current) return;
    const controller = new AbortController();
    authRequest.current = controller;
    const requestedHash = location.hash;
    setChecking(true);
    try {
      const session = await api<{ authenticated: boolean; actor_id?: string; csrf_token?: string }>(
        "/auth/session", { cache: "no-store", signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const actorId = (session.authenticated && session.actor_id) || "";
      checkedHash.current = requestedHash === location.hash ? requestedHash : "";
      updateSession(actorId, session.csrf_token);
      if (!actorId && afterUnauthorized) setLoginError("会话已失效，请重新登录。");
    } catch {
      if (!controller.signal.aborted) updateSession("");
    } finally {
      if (authRequest.current === controller) {
        authRequest.current = null;
        setChecking(false);
      }
    }
  }, [updateSession]);

  useEffect(() => {
    const unsubscribe = observeUnauthenticated(() => {
      if (activeSession.current.accountId) void checkSession(true);
    });
    void checkSession();
    return () => {
      unsubscribe();
      authRequest.current?.abort();
      authRequest.current = null;
    };
  }, [checkSession]);
  useEffect(() => {
    if (checkDestination && !checking) void checkSession(true);
    if (!external && checkedHash.current !== hash) checkedHash.current = "";
  }, [checkDestination, checking, external, hash, checkSession]);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    if (authRequest.current) return;
    const controller = new AbortController();
    authRequest.current = controller;
    setSigningIn(true);
    setLoginError("");
    try {
      const session = await api<{ csrf_token: string; actor_id: string }>(
        "/auth/login",
        { ...mutation("POST", { username, password }), signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      checkedHash.current = location.hash;
      updateSession(session.actor_id, session.csrf_token);
      setPassword("");
    } catch (error) {
      if (!controller.signal.aborted) setLoginError(text(error));
    } finally {
      if (authRequest.current === controller) {
        authRequest.current = null;
        setSigningIn(false);
      }
    }
  }

  async function logout() {
    if (authRequest.current) return;
    const controller = new AbortController();
    authRequest.current = controller;
    setChecking(true);
    try {
      await api("/auth/session", { ...mutation("DELETE", {}), signal: controller.signal });
    } catch {
      // Always clear the local workspace after logout is requested.
    } finally {
      if (authRequest.current === controller) {
        updateSession("");
        setLoginError("");
        authRequest.current = null;
        setChecking(false);
      }
    }
  }

  if (invalid)
    return (
      <main className="login">
        <section className="navigation-invalid" role="alert">
          <h1>无法打开此链接</h1>
          <p>链接参数不完整或格式无效。</p>
          <button
            className="primary"
            onClick={() => {
              history.replaceState({}, "", location.pathname);
              navigate({ view: "projects" }, true);
            }}
          >
            打开项目目录
          </button>
        </section>
      </main>
    );
  if (checkDestination) return <div className="splash">正在确认登录状态…</div>;
  if (accountId)
    return (
      <Workspace
        key={sessionGeneration}
        accountId={accountId}
        navigation={navigation}
        checkingSession={checking}
        onLogout={logout}
      />
    );
  if (checking) return <div className="splash">正在打开知境 Studio…</div>;
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
        {loginError && (
          <p role="alert" className="error">
            {loginError}
          </p>
        )}
        <button className="primary" type="submit" disabled={signingIn}>
          打开交互工作台 <ArrowUpRight size={16} />
        </button>
        <small>VKS 3.0 · 原始交互与知识关联预览</small>
      </form>
    </main>
  );
}

function Workspace({
  accountId, navigation, checkingSession, onLogout,
}: {
  accountId: string;
  navigation: ReturnType<typeof useStudioNavigation>;
  checkingSession: boolean;
  onLogout: () => Promise<void>;
}) {
  const {
    route, invalid, generation, pageBinding, directory, external, navigate, isCurrent,
    report: reportNavigation, status: navigationStatus, retry: retryNavigation,
  } = navigation;
  const inPrototype = !route.view || route.view === "prototype";
  const inDocuments = route.view === "documents";
  const bindingId = inPrototype ? route.binding || pageBinding : "";
  const pageRef = inPrototype ? route.page_ref : "";
  const hasDestination =
    (inPrototype && !!(bindingId || pageRef || route.package)) ||
    (inDocuments && !!route.doc);
  const [workspaceReady, setWorkspaceReady] = useState(""),
    [loadedCatalog, setCatalog] = useState<Catalog | null>(null),
    loadedScope = useRef(""),
    authorizedGeneration = useRef(-1),
    authorizingGeneration = useRef(-1),
    [loadedBindings, setBindings] = useState<Binding[]>([]),
    [bindingsReady, setBindingsReady] = useState(false),
    [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const prefix = `/api/v3/projects/${route.project}/interactive`,
    scope = route.release
      ? `release_id=${encodeURIComponent(route.release)}`
      : `analysis_set_id=${encodeURIComponent(route.analysis)}`;
  const currentScope = useRef(prefix + scope);
  currentScope.current = prefix + scope;
  const needsFreshCatalog = external && hasDestination && !!route.release && generation > 0;
  const catalog =
    (!needsFreshCatalog || authorizedGeneration.current === generation) &&
    !invalid && loadedScope.current === prefix + scope &&
    loadedCatalog &&
    (route.release
      ? loadedCatalog.release_id === route.release
      : !!route.analysis && loadedCatalog.analysis_set_id === route.analysis)
      ? loadedCatalog
      : null;
  const bindings = catalog ? loadedBindings : [];
  const migrationScope = route.project && route.analysis && !route.release
    ? `${route.project}:${route.analysis}`
    : "";
  const [migratingScope, setMigratingScope] = useState("");
  const [applyingMaterials, setApplyingMaterials] = useState<Record<string, number>>({});
  const associationBlocked =
    migrationScope && (migratingScope === migrationScope || applyingMaterials[migrationScope])
      ? "当前草稿正在迁移资料引用，可在资料管理查看进度。"
      : "";
  useEffect(() => {
    if (!catalog || !migrationScope) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function update() {
      try {
        const status = await api<{ migrating: boolean }>(
          `/api/v3/projects/${route.project}/interactive/reassociate/status?analysis_set_id=${encodeURIComponent(route.analysis)}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted)
          setMigratingScope(status.migrating ? migrationScope : "");
      } catch {
        // Keep the last known status until the next successful read.
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(update, 3000);
      }
    }
    void update();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [catalog?.editable, catalog?.selection_version, migrationScope, route.project, route.analysis]);
  const documentRef = catalog?.documents.find(
    (doc) => doc.revision_id === route.doc,
  );
  const presentation =
    catalog?.presentation ||
    ({
      mode: "auto",
      template_id: "handbook",
      template_version: "1.0.0",
    } as Presentation);
  const readerChoice = route.reader || presentation.mode;
  const handbookReader = readerChoice === "handbook";
  const nativeRead =
    readerChoice !== "standard" &&
    !handbookReader &&
    !!documentRef?.native_package_id;
  const prototypePackages =
    catalog?.packages.filter((p) => p.kind === "prototype") || [];
  const [resolvedPage, setResolvedPage] = useState<
    { generation: number; page: RegisteredPage } | null
  >(null);
  const page = resolvedPage?.generation === generation ? resolvedPage.page : null;
  const linkedBinding = bindings.find(
    (binding) => binding.id === (route.binding || pageBinding) && !binding.excluded,
  );
  // Keep the background package mounted across view switches. A specified
  // package or binding may never fall back to a different visible package.
  const packageChoice = route.package
    ? prototypePackages.find((pack) => (pack.package_key || pack.id) === route.package)
    : prototypePackages.find((pack) => pack.id === linkedBinding?.package_id) ||
      (bindingId ? undefined : (prototypePackages.find(
        (pack) => (pack.package_key || pack.id) === catalog?.primary_package_id,
      ) || prototypePackages[0]));
  const p = packageChoice &&
    (!bindingId || packageChoice.id === linkedBinding?.package_id) &&
    (!pageRef || packageChoice.id === page?.package_artifact_id)
    ? packageChoice : undefined;
  const documentCache = useMemo(
    () =>
      new DocumentCache<Document>((id, native, signal) =>
        api<Document>(
          `${prefix}/documents/${encodeURIComponent(id)}?${scope}&native=${native}`,
          { signal },
        ),
      ),
    [accountId, catalog, prefix, scope],
  );
  useEffect(() => () => documentCache.dispose(), [documentCache]);
  const [notice, setNotice] = useTransientNotice(!!catalog);
  const [error, setError] = useState(""),
    [failedScope, setFailedScope] = useState(""),
    [scopeRetry, setScopeRetry] = useState(0),
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
    !directory &&
    (!route.doc || !!documentRef) &&
    !handbookReader &&
    readerChoice !== "standard" &&
    ((!route.doc &&
      (!nativePackageModified ||
        ["source", "original"].includes(readerChoice))) ||
      (nativeDetail
        ? nativeDetail.matches_index ||
          ["source", "original"].includes(readerChoice)
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
    [editSession, setEditSession] = useState<{
      scope: string;
      packageId: string;
      target: Target;
      binding?: Binding;
    } | null>(null),
    [picking, setPicking] = useState(false),
    [saving, setSaving] = useState(false),
    [pinned, setPinned] = useState(false),
    [docked, setDocked] = useState(false),
    [assistantOptions, setAssistantOptions] = useState(false);
  const editing = !!editSession;
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
  const detached = useDetachedAssistant(
    `vks.assistant.detached:${accountId}:${route.project}:${scope}`,
    setNotice,
  );
  const floating = useFloatingWindow(docked);
  const { ref: windowRef, setPosition } = floating;
  const prototype = useRef<RuntimeHandle | null>(null),
    pendingReveal = useRef<{
      generation: number;
      packageId: string;
      target: Pick<Target, "route" | "frame_path">;
      binding?: Binding;
      pageOnly: boolean;
      instanceId?: string;
      requestId?: string;
      timeout: ReturnType<typeof setTimeout>;
    } | null>(null),
    readyRuntime = useRef(""),
    documentReveal = useRef(false),
    reader = useRef<HTMLElement>(null),
    docEpoch = useRef(0),
    editEpoch = useRef(0),
    loadEpoch = useRef(0),
    settledReveal = useRef(-1),
    initialBinding = useRef(route.binding),
    assistantTrigger = useRef<HTMLButtonElement>(null);
  const [previewPosition, setPreviewPosition] = useState<
    { instanceId: string; packageId: string; scope: string; position: PrototypePosition | null } | null
  >(null);
  const [bindingPageFallback, setBindingPageFallback] = useState(-1);
  const [runtimeRestart, setRuntimeRestart] = useState(0);
  const bindingPageOnly = !!pageBinding || bindingPageFallback === generation;
  const [prototypeChapter, setPrototypeChapter] = useState<string | null>(null);
  const [documentOrigin, setDocumentOrigin] = useState<ReadingPosition | null>(
    null,
  );
  const [related, setRelated] = useState<Related | null>(null),
    [retrieving, setRetrieving] = useState(false);
  const [assistant, setAssistant] = useState<Assistance | null>(null),
    [explanationStale, setExplanationStale] = useState(false),
    [assisting, setAssisting] = useState(false),
    [assistantMode, setAssistantMode] = useState<
      "documents" | "explain" | "chat"
    >("documents"),
    [assistantError, setAssistantError] = useState(""),
    [associationReviewTarget, setAssociationReviewTarget] = useState<{
      items?: ReviewTarget[];
      mode?: "missing" | "all";
      prototype: boolean;
      addedChapter?: { name: string; document: string };
    } | null>(null),
    [automatic, setAutomatic] = useState(
      () => localStorage.getItem("vks.studio.auto-associate") !== "off",
    ),
    [moduleTitle, setModuleTitle] = useState(""),
    [menu, setMenu] = useState<
      (Control & { packageId: string; x: number; y: number }) | null
    >(null);
  const moduleControl = useRef<Control | null>(null),
    control = useRef<Control | null>(null),
    controlIdentity = useRef(""),
    explanations = useRef(
      new Map<string, { answer: Assistance; point: string | null }>(),
    ),
    explanationRequest = useRef<{
      key: string;
      controller: AbortController;
    } | null>(null),
    lastAssist = useRef<{
      control: Control;
      mode: "documents" | "explain";
    } | null>(null),
    assistEpoch = useRef(0),
    confirmedEpoch = useRef(-1),
    assistAbort = useRef<AbortController | null>(null),
    automaticTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
      undefined,
    ),
    menuRef = useRef<HTMLDivElement>(null);
  const [citationChat, setCitationChat] = useState<{
    scope: string;
    key: string;
    documentChat: boolean;
    context: Control | null;
    documentId?: string;
    packageId: string;
    sourceFingerprint: string;
  } | null>(null);
  const chatSources = useMemo(() => chatSourceFingerprint(catalog), [catalog]);
  const [sourcePreview, setSourcePreview] = useState<{
    reference: Citation;
    binding?: Pick<Binding, "id">;
  } | null>(null);
  useEffect(
    () => setSourcePreview(null),
    [
      accountId,
      route.project,
      scope,
      chatSources,
      target,
      assistantMode,
      knowledge,
      editing,
      detached.open,
    ],
  );
  function previewSource(reference: Citation, binding?: Pick<Binding, "id">) {
    setSourcePreview({ reference, binding });
  }
  useEffect(() => {
    const previous = explanations.current.get(controlIdentity.current);
    if (previous && assistant === previous.answer)
      setExplanationStale(
        previous.point !== (active?.point_revision_id || null),
      );
  }, [active?.point_revision_id, assistant]);
  const citationOrigin =
    route.view === "documents" &&
    citationChat?.scope === route.project + scope &&
    citationChat.sourceFingerprint === chatSources
      ? citationChat
      : null;
  const [associationChapter, setAssociationChapter] =
    useState<AssociationChapter | null>(null);
  const documentChat =
    citationOrigin?.documentChat ?? route.view === "documents";
  useEffect(() => {
    if (route.view === "documents" || route.view === "prototype")
      detached.controller.show(documentChat);
  }, [detached.open, documentChat, route.view, detached.controller]);
  async function detachAssistant() {
    if (await detached.controller.open()) {
      detached.controller.show(documentChat);
      setKnowledge(true);
    }
  }

  const prototypeAssistantMode = useRef(assistantMode),
    inDocumentChat = useRef(false);
  const documentChatLabel =
    citationOrigin?.context?.target.label ||
    documentRef?.name ||
    projects.find((project) => project.id === route.project)?.name ||
    "项目知识文档";
  const chatContext: Control | null = citationOrigin
    ? citationOrigin.context
    : documentChat
      ? {
          target: {
            route: "/documents",
            frame_path: [],
            selector: "body",
            label: documentChatLabel,
            state: "",
            open_steps: [],
          },
          context: {
            scope: "module",
            page_title: documentChatLabel,
            section_title: "",
            nearby_labels: [],
          },
        }
      : control.current;
  const chatKey =
    citationOrigin?.key ||
    JSON.stringify([
      accountId,
      route.project,
      scope,
      p?.id,
      documentChat ? documentRef?.id || "project-documents" : null,
      !documentChat &&
        chatContext && {
          target: chatContext.target,
          context: chatContext.context,
        },
    ]);
  const chatPackage = citationOrigin?.packageId ?? p?.id ?? "";
  const chatDocument = documentChat
    ? {
        revision_id: citationOrigin
          ? citationOrigin.documentId
          : documentRef?.revision_id,
      }
    : undefined;
  const chat = useKnowledgeChat(
    chatKey,
    prefix,
    route.release
      ? { release_id: route.release }
      : { analysis_set_id: route.analysis },
    chatPackage,
    chatContext,
    chatDocument,
    chatSources,
    async (operations) => {
      const [c, b] = await Promise.all([
        api<Catalog>(`${prefix}/catalog?${scope}`),
        api<Binding[]>(`${prefix}/bindings?${scope}`),
      ]);
      const changed = operations.find(
        (operation) => operation.kind === "document",
      );
      if (
        changed?.revision_id &&
        documentChat &&
        documentRef?.id === changed.document_id
      )
        navigate({ doc: changed.revision_id, section: "" }, true);
      setCitationChat((previous) =>
        previous
          ? {
              ...previous,
              sourceFingerprint: chatSourceFingerprint(c),
              documentId:
                changed?.revision_id &&
                previous.documentId === documentRef?.revision_id
                  ? changed.revision_id
                  : previous.documentId,
            }
          : null,
      );
      setCatalog(c);
      setBindings(b);
      setBindingsReady(true);
      if (operations.some((operation) => operation.kind === "binding")) {
        if (assistant) setExplanationStale(true);
        setRelated(null);
      }
      if (active)
        setActive(b.find((binding) => binding.id === active.id) || null);
    },
  );
  useEffect(() => {
    setAssociationChapter(null);
    setPrototypeChapter(null);
  }, [route.project, scope, route.doc, route.view]);
  const reload = useCallback(async (destinationGeneration?: number) => {
    if (currentScope.current !== prefix + scope) return;
    const epoch = ++loadEpoch.current;
    if (destinationGeneration !== undefined)
      authorizingGeneration.current = destinationGeneration;
    setBindingsReady(false);
    const [c, b] = await Promise.all([
      api<Catalog>(`${prefix}/catalog?${scope}`).then((catalog) => {
        if (epoch === loadEpoch.current) {
          // The catalog can paint before links on initial load. During a
          // refresh retain the current links until their replacement arrives,
          // so a binding-only bookmark keeps the same mounted package.
          loadedScope.current = prefix + scope;
          setCatalog(catalog);
        }
        return catalog;
      }),
      api<Binding[]>(`${prefix}/bindings?${scope}`),
    ]);
    if (epoch !== loadEpoch.current) return;
    if (destinationGeneration !== undefined)
      authorizedGeneration.current = destinationGeneration;
    setBindings(b);
    setBindingsReady(true);
    return { c, b };
  }, [prefix, scope]);
  useEffect(() => {
    if (invalid) return;
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
      .catch((e) => {
        if (!cancelled) {
          setFailedScope(prefix + scope);
          setError(text(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [invalid, route.project, route.analysis, route.release, navigate, scopeRetry]);
  useEffect(() => {
    if (invalid || !route.project || (!route.analysis && !route.release)) return;
    if (
      !route.release &&
      workspaceReady !== `${route.project}:${route.analysis}`
    )
      return;
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    control.current = null;
    controlIdentity.current = "";
    explanations.current.clear();
    explanationRequest.current = null;
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
    setAssociationReviewTarget(null);
    setMenu(null);
    setKnowledge(false);
    setAssistantOptions(false);
    setCatalog(null);
    setBindings([]);
    setBindingsReady(false);
    setActive(null);
    setTarget(null);
    setEditSession(null);
    setPicking(false);
    cancelPendingReveal();
    settledReveal.current = -1;
    setPreviewPosition(null);
    setViewDoc(null);
    setBookMounted(false);
    setPrototypeMounted(false);
    setSourcePackage("");
    pendingNativeLocation.current = null;
    setError("");
    setFailedScope("");
    let cancelled = false;
    void reload(generation).catch((e) => {
      if (!cancelled) {
        setFailedScope(prefix + scope);
        setError(text(e));
      }
    });
    return () => {
      cancelled = true;
      loadEpoch.current++;
    };
  }, [
    route.project,
    route.analysis,
    route.release,
    invalid,
    reload,
    workspaceReady,
    scopeRetry,
  ]);
  useEffect(() => {
    if (invalid || !needsFreshCatalog || loadedScope.current !== prefix + scope ||
        !loadedCatalog || authorizingGeneration.current === generation ||
        authorizedGeneration.current === generation) return;
    setFailedScope("");
    void reload(generation).catch(() => {
      if (isCurrent(generation)) setFailedScope(prefix + scope);
    });
  }, [invalid, generation, needsFreshCatalog, loadedCatalog, prefix, scope, reload]);
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
      !directory &&
      !handbookReader &&
      catalog?.documents.length &&
      (readerChoice === "standard" ||
        (nativePackageModified && route.reader !== "source") ||
        !catalog.documents.some((doc) => doc.native_package_id)) &&
      !route.doc &&
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
    directory,
    readerChoice,
    handbookReader,
    catalog,
    nativePackage?.id,
    nativePackageModified,
    navigate,
  ]);
  useEffect(() => {
    if (!catalog || !pageRef || !packageChoice) return;
    const controller = new AbortController();
    const params = new URLSearchParams(scope);
    params.set("package", route.package);
    params.set("page_ref", pageRef);
    api<{ items: RegisteredPage[] }>(`${prefix}/previews/pages?${params}`, {
      signal: controller.signal,
    }).then(({ items }) => {
      if (!isCurrent(generation) || controller.signal.aborted) return;
      const match = items.length === 1 && items[0];
      if (match && match.page_ref === pageRef && match.package_artifact_id === packageChoice.id)
        setResolvedPage({ generation, page: match });
      else reportNavigation(generation, { phase: "failed", message: missingLocation });
    }).catch(() => {
      if (!controller.signal.aborted)
        reportNavigation(generation, { phase: "failed", message: missingLocation });
    });
    return () => controller.abort();
  }, [catalog, pageRef, packageChoice?.id, route.package, prefix, scope, generation]);
  useEffect(() => {
    return () => {
      if (pendingReveal.current?.generation === generation) cancelPendingReveal();
    };
  }, [generation]);
  useEffect(() => {
    if (!inPrototype || !catalog) return;
    if ((route.package && !packageChoice) ||
        (bindingId && bindingsReady && (!linkedBinding || !p))) {
      cancelPendingReveal();
      reportNavigation(generation, { phase: "failed", message: missingLocation });
      return;
    }
    if (bindingId && !bindingsReady) return;
    if (pendingReveal.current || settledReveal.current === generation || !p) return;
    if (bindingId && linkedBinding) {
      startReveal(p.id, linkedBinding.target, bindingPageOnly, linkedBinding);
      if (!bindingPageOnly && !history.state?.vksFromDocument &&
          initialBinding.current !== route.binding) setKnowledge(true);
      initialBinding.current = "";
    } else if (page) {
      startReveal(p.id, page, true);
    }
  }, [
    inPrototype, generation,
    bindingId, bindingPageOnly, route.package,
    catalog,
    bindingsReady,
    linkedBinding,
    packageChoice?.id,
    p?.id,
    page,
  ]);
  useEffect(() => {
    if (!bindingsReady || editing || !p || !control.current) return;
    const binding = bindingForTarget(bindings, p.id, control.current.target);
    setActive(binding && !binding.excluded ? binding : null);
  }, [bindingsReady, bindings, p?.id, editing]);
  useEffect(() => {
    if (!catalog || !documentRef) return;
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
  }, [documentCache, route.doc, !!documentRef, nativeRead, documentRetry]);
  useEffect(() => {
    if (!inDocuments || !catalog || !route.doc) return;
    if (!documentRef || documentError || (viewDoc && route.section &&
        !viewDoc.sections.some((section) => section.revision_id === route.section)))
      reportNavigation(generation, { phase: "failed", message: missingLocation });
  }, [catalog, inDocuments, route.doc, route.section, documentRef, viewDoc, documentError, generation]);
  useEffect(() => {
    if (!viewDoc || !inDocuments || originalBook || (route.section &&
        !viewDoc.sections.some((section) => section.revision_id === route.section))) return;
    let observer: IntersectionObserver | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const frame = requestAnimationFrame(() => {
      if (!isCurrent(generation)) return;
      const container = documentScrollRoot(reader.current);
      const saved = history.state?.vksReadingPosition as
        | ReadingPosition
        | undefined;
      if (
        container &&
        saved &&
        sameWorkspace(saved.route, route) &&
        saved.route.doc === route.doc &&
        saved.route.section === route.section &&
        saved.route.reader === route.reader &&
        saved.route.book === route.book
      ) {
        const chapter = document.getElementById("section-" + saved.chapter);
        const top =
          chapter && saved.chapterOffset !== undefined
            ? container.scrollTop +
              chapter.getBoundingClientRect().top -
              container.getBoundingClientRect().top -
              saved.chapterOffset
            : saved.top;
        container.scrollTo({ top, behavior: "instant" });
        reportNavigation(generation, { phase: "located", message: "已恢复文档阅读位置" });
        return;
      }
      const position = route.section
        ? document.getElementById("section-" + route.section)
        : reader.current?.querySelector(".document-title");
      if (route.section) position?.scrollIntoView({ block: "start", behavior: "instant" });
      else container?.scrollTo({ top: 0, behavior: "instant" });
      if (position && window.IntersectionObserver) {
        observer = new IntersectionObserver(([entry]) => {
          if (!entry.isIntersecting) return;
          observer?.disconnect();
          clearTimeout(timeout);
          reportNavigation(generation, {
            phase: "located",
            message: route.section ? "已定位到指定章节" : "已打开指定文档修订",
          });
        });
        observer.observe(position);
      }
      timeout = setTimeout(() => reportNavigation(generation, {
        phase: "failed", message: "未能在阅读区定位指定位置",
      }), 5000);
    });
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      clearTimeout(timeout);
    };
  }, [viewDoc?.revision_id, route.section, inDocuments, originalBook, handbookReader, generation]);
  useEffect(() => {
    if (route.view !== "prototype") {
      documentReveal.current = false;
      setDocumentOrigin(null);
    }
  }, [route.view, route.project, scope]);
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
    menuRef.current
      ?.querySelector<HTMLButtonElement>("button")
      ?.focus({ preventScroll: true });
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
    if (saving) return;
    if (detached.open) detached.controller.attach();
    setEditSession(null);
    setKnowledge(false);
    setAssistantOptions(false);
    setAssociationReviewTarget(null);
    requestAnimationFrame(() =>
      assistantTrigger.current?.focus({ preventScroll: true }),
    );
  }
  function toggleAssistant() {
    if (detached.open) {
      detached.controller.open();
      setKnowledge(true);
      return;
    }
    if (knowledge) {
      collapseAssistant();
      return;
    }
    setKnowledge(true);
    setAssistantOptions(false);
    if (documentChat) {
      switchKnowledge("chat");
      return;
    }
    setAssistantMode("documents");
    // Opening the panel during a bookmark restore must keep the saved binding
    // until the runtime has reported the corresponding target.
    if (
      !control.current &&
      moduleControl.current &&
      (!route.binding || settledReveal.current === generation)
    )
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
  function explanationIdentity(c: Control) {
    return JSON.stringify([
      accountId,
      route.project,
      scope,
      p?.id,
      chatSources,
      c.target.route,
      c.target.frame_path,
      c.target.selector,
      c.target.label,
      c.target.state,
    ]);
  }
  function chooseControl(
    c: Control,
    preserveMode = false,
    keepExplanation = false,
    keepBindingRoute = false,
  ) {
    const identity = explanationIdentity(c);
    if (
      control.current &&
      controlIdentity.current === identity &&
      sameTarget(control.current.target, c.target)
    ) {
      // Nearby labels and repeated runtime notices are not a new target.
      control.current = c;
      return;
    }
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    control.current = c;
    controlIdentity.current = identity;
    if (!preserveMode) setAssistantMode("documents");
    lastAssist.current = null;
    setTarget(c.target);
    const matchingBinding = bindingForTarget(bindings, p?.id, c.target);
    const selectedBinding = matchingBinding?.excluded
      ? null
      : matchingBinding || null;
    setActive(selectedBinding);
    if (
      !keepBindingRoute &&
      route.binding &&
      selectedBinding?.id !== route.binding
    )
      navigate({ binding: "" }, true);
    const previousExplanation = explanations.current.get(identity);
    if (previousExplanation) {
      setAssistant(previousExplanation.answer);
      setExplanationStale(
        previousExplanation.point !==
          (selectedBinding?.point_revision_id || null),
      );
    } else if (!keepExplanation) {
      setAssistant(null);
      setExplanationStale(false);
    }
    setRelated(null);
    setRetrieving(false);
    setAssistantError("");
    setAssociationReviewTarget(null);
    setAssisting(false);
  }
  function switchKnowledge(mode: "documents" | "explain" | "chat") {
    setAssistantMode(mode);
    if (mode === "chat") {
      if (
        explanationRequest.current &&
        !explanationRequest.current.controller.signal.aborted
      )
        return;
      assistEpoch.current++;
      assistAbort.current?.abort();
      cancelAutomatic();
      setAssisting(false);
      setRetrieving(false);
      return;
    }
    if (!control.current) return;
    if (mode === "documents" && related) return;
    if (mode === "explain" && (assistant || assisting)) return;
    void assist(
      control.current,
      mode,
      mode === "documents" && active?.validity_status !== "valid",
    );
  }
  useEffect(() => {
    if (!documentChat) {
      if (inDocumentChat.current) {
        inDocumentChat.current = false;
        setAssistantMode(prototypeAssistantMode.current);
      }
      return;
    }
    if (!inDocumentChat.current) prototypeAssistantMode.current = assistantMode;
    inDocumentChat.current = true;
    // Document questions use their own scope, even when the assistant remained
    // open while navigating away from a prototype control.
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    setAssistantMode("chat");
    setAssisting(false);
    setRetrieving(false);
  }, [documentChat]);
  async function assist(
    c: Control,
    mode: "documents" | "explain",
    refresh = false,
    revealPanel = true,
  ) {
    if (mode === "explain" && associationBlocked) {
      setNotice(associationBlocked);
      return;
    }
    if (editing || !p) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    const identity = explanationIdentity(c);
    if (
      mode === "explain" &&
      !refresh &&
      explanationRequest.current?.key === identity &&
      !explanationRequest.current.controller.signal.aborted
    ) {
      setAssistantMode("explain");
      if (revealPanel) setKnowledge(true);
      return;
    }
    const sameControl =
      !!control.current &&
      sameTarget(control.current.target, c.target) &&
      controlIdentity.current === identity;
    chooseControl(c, false, sameControl);
    lastAssist.current = { control: c, mode };
    setAssistantMode((previous) =>
      !revealPanel && previous === "chat" ? "chat" : mode,
    );
    setMenu(null);
    if (revealPanel) {
      setKnowledge(true);
      setAssistantOptions(false);
    }
    const existing = bindingForTarget(bindings, p.id, c.target);
    if (existing?.excluded) {
      setAssistantError(
        "此关联已被移除，自动关联不会恢复它；可在关联管理中恢复。",
      );
      return;
    }
    const previousExplanation = explanations.current.get(identity);
    if (mode === "explain" && !refresh && previousExplanation) {
      setAssistant(previousExplanation.answer);
      setExplanationStale(
        previousExplanation.point !== (existing?.point_revision_id || null),
      );
      return;
    }
    assistAbort.current?.abort();
    cancelAutomatic();
    const epoch = ++assistEpoch.current,
      controller = new AbortController();
    assistAbort.current = controller;
    if (mode === "explain") {
      explanationRequest.current = { key: identity, controller };
      setAssisting(true);
    }
    const lookupPayload = {
      ...(route.release
        ? { release_id: route.release }
        : { analysis_set_id: route.analysis }),
      package_id: p.id,
      target: c.target,
      context: c.context,
      mode,
      refresh,
    };
    const payload = lookupPayload;
    let matches: Related | null =
      existing &&
      existing.validity_status === "valid" &&
      existing.sections.length > 0
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
          ...mutation("POST", lookupPayload),
          signal: controller.signal,
        });
        if (epoch !== assistEpoch.current) return;
        setRelated(matches);
        if (matches.vector_available && matches.origin !== "binding" && !existing && !matches.binding_id) {
          void api<Related>(`${prefix}/semantic`, {
            ...mutation("POST", lookupPayload),
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
        if (explanationRequest.current?.controller === controller)
          explanationRequest.current = null;
        if (epoch === assistEpoch.current && !controller.signal.aborted) {
          setAssistantError(text(e));
          setAssisting(false);
        }
        return;
      } finally {
        if (epoch === assistEpoch.current) setRetrieving(false);
      }
    }
    if (matches.excluded) {
      if (explanationRequest.current?.controller === controller)
        explanationRequest.current = null;
      setAssistantError("此关联已被移除，自动关联不会恢复它。");
      setAssisting(false);
      return;
    }
    if (
      mode === "documents" &&
      // The authorized related response can arrive before the much larger
      // binding list. Reuse that exact saved identity rather than generating
      // again because this render's local list is still empty.
      (existing || matches.binding_id)
    )
      return;
    if (
      mode === "documents" &&
      (associationBlocked || !automatic || !matches.model_available || !matches.sections.length)
    )
      return;
    const runModel = async () => {
      if (epoch !== assistEpoch.current || controller.signal.aborted) return;
      setAssisting(true);
      try {
        const result = await api<Assistance>(
          `${prefix}/${mode === "explain" ? "explain" : "assist"}`,
          {
            ...mutation("POST", payload),
            signal: controller.signal,
          },
        );
        if (epoch !== assistEpoch.current) return;
        confirmedEpoch.current = epoch;
        if (mode === "explain") {
          explanations.current.set(identity, {
            answer: result,
            point: existing?.point_revision_id || null,
          });
          while (explanations.current.size > 32)
            explanations.current.delete(
              explanations.current.keys().next().value!,
            );
          setAssistant(result);
          setExplanationStale(false);
        }
        if (result.saved && assistant) setExplanationStale(true);
        if (mode === "explain") return;
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
        // Read back the committed binding without clearing the current list
        // or temporarily replacing it with the pre-request binding.
        ++loadEpoch.current;
        const [c, b] = await Promise.all([
          api<Catalog>(`${prefix}/catalog?${scope}`),
          api<Binding[]>(`${prefix}/bindings?${scope}`),
        ]);
        if (epoch !== assistEpoch.current) return;
        const savedBinding = b.find(
          (binding) => binding.id === result.binding_id && !binding.excluded,
        );
        setCatalog(c);
        setBindings(b);
        setBindingsReady(true);
        setActive(savedBinding || existing || null);
      } catch (e) {
        if (epoch === assistEpoch.current && !controller.signal.aborted)
          setAssistantError(text(e));
      } finally {
        if (explanationRequest.current?.controller === controller)
          explanationRequest.current = null;
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
  function openReassociation() {
    const selected = control.current;
    if (!selected || !p || !catalog?.editable || !bindingsReady || associationBlocked) return;
    const current = bindingForTarget(bindings, p.id, selected.target);
    if (current?.excluded) return;
    setAssistantMode("documents");
    setAssistantOptions(false);
    setAssistantError("");
    setAssociationReviewTarget({
      prototype: true,
      items: [
        {
          package_id: p.id,
          target: selected.target,
          context: selected.context,
          binding_id: current?.id,
          binding_version: current?.binding_version,
          point_revision_id: current?.point_revision_id,
          title: current?.title || selected.target.label,
        },
      ],
    });
  }
  async function associationSaved() {
    const selected = associationReviewTarget;
    const loaded = await reload();
    if (!loaded)
      throw new Error("关联已保存，但最新内容尚未读回，请重新读取。");
    const item = selected?.items?.length === 1 ? selected.items[0] : null;
    const match = item
      ? bindingForTarget(loaded.b, item.package_id, {
          ...item.target,
          state: item.target.state || "",
          open_steps: item.target.open_steps || [],
        })
      : null;
    const saved = match?.excluded ? null : match;
    if (item && !saved)
      throw new Error("关联已保存，但最新内容尚未读回，请刷新关联列表。");
    if (
      saved &&
      saved.package_id === p?.id &&
      control.current &&
      sameTarget(saved.target, control.current.target)
    ) {
      setActive(saved);
      setRelated({
        title: saved.title,
        sections: saved.sections,
        binding_id: saved.id,
        excluded: false,
        origin: "binding",
        model_available: true,
        selection_version: loaded.c.selection_version,
        elapsed_ms: 0,
      });
      if (assistant) setExplanationStale(true);
    }
    setAssociationReviewTarget(null);
    setNotice("已确认保存关联并读取最新内容");
  }
  function sendPendingReveal() {
    const runtime = prototype.current;
    const pending = pendingReveal.current;
    if (
      runtime &&
      readyRuntime.current === runtime.instanceId &&
      pending &&
      isCurrent(pending.generation) &&
      pending.packageId === p?.id &&
      pending.instanceId !== runtime.instanceId
    ) {
      pending.instanceId = runtime.instanceId;
      pending.requestId = runtime.send("target.reveal", {
        target: pending.target,
        page_only: pending.pageOnly,
      });
      reportNavigation(pending.generation, { phase: "locating", message: "正在定位原型…" });
    }
  }
  function cancelPendingReveal() {
    const runtime = prototype.current;
    if (runtime && pendingReveal.current?.instanceId === runtime.instanceId)
      runtime.send("target.cancel");
    clearTimeout(pendingReveal.current?.timeout);
    pendingReveal.current = null;
    documentReveal.current = false;
  }
  function onRuntime(event: RuntimeEvent) {
    if (prototype.current?.instanceId !== event.runtime_instance_id) return;
    if (event.type === "prototype.position" && p) {
      setPreviewPosition({
        instanceId: event.runtime_instance_id,
        packageId: p.id,
        scope: prefix + scope,
        position: event.payload as PrototypePosition | null,
      });
      return;
    }
    if (event.type === "context.changed") readyRuntime.current = event.runtime_instance_id;
    if (!isCurrent(generation)) return;
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
      if (route.view && route.view !== "prototype") return;
      if (pendingReveal.current && event.type === "control.activated") return;
      if (editing) {
        if (event.type !== "control.activated")
          setNotice("请先保存或取消当前编辑");
        return;
      }
      if (event.type === "context.menu" && p) {
        chooseControl(c);
        setMenu({
          ...c,
          packageId: p.id,
          x: Math.max(8, Math.min(c.x || 8, innerWidth - 292)),
          y: Math.max(8, Math.min(c.y || 8, innerHeight - 230)),
        });
      } else if (event.type === "knowledge.request")
        void assist(c, c.mode === "documents" ? "documents" : "explain");
      else if (!pinned) {
        chooseControl(c);
        if (
          automatic &&
          !documentReveal.current &&
          (route.view || "prototype") === "prototype" &&
          !existingBindingStopsAutomatic(c.target)
        )
          void assist(c, "documents", false, false);
      }
      return;
    }
    if (event.type === "context.changed") {
      sendPendingReveal();
      const unchanged = event.payload as {
        module?: Control;
        route?: string;
        frame_path?: string[];
      };
      if (
        !picking &&
        !editing &&
        control.current &&
        controlIdentity.current === explanationIdentity(control.current) &&
        ((unchanged.module &&
          moduleControl.current &&
          isTarget(unchanged.module.target) &&
          sameTarget(unchanged.module.target, moduleControl.current.target)) ||
          (!unchanged.module &&
            unchanged.route === control.current.target.route &&
            JSON.stringify(unchanged.frame_path) ===
              JSON.stringify(control.current.target.frame_path)))
      ) {
        if (unchanged.module) {
          moduleControl.current = unchanged.module;
          setModuleTitle(unchanged.module.target.label);
        }
        return;
      }
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
      if (
        pendingReveal.current ||
        (inPrototype && (bindingId || pageRef) && settledReveal.current !== generation)
      )
        return;
      assistEpoch.current++;
      assistAbort.current?.abort();
      cancelAutomatic();
      setAssisting(false);
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
        setAssociationReviewTarget(null);
        control.current = null;
        setActive(null);
        setTarget(null);
      }
      setMenu((previous) =>
        previous &&
        JSON.stringify(previous.target.frame_path) ===
          JSON.stringify(c.frame_path) &&
        previous.target.route !== c.route
          ? null
          : previous,
      );
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
        if (
          automatic &&
          !documentReveal.current &&
          (route.view || "prototype") === "prototype" &&
          !existingBindingStopsAutomatic(c.module.target)
        )
          void assist(c.module, "documents", false, false);
      }
    }
    if (event.type === "picker.cancelled") setPicking(false);
    if (
      event.type === "command.result" &&
      pendingReveal.current?.requestId === event.request_id &&
      pendingReveal.current.instanceId === event.runtime_instance_id &&
      pendingReveal.current.packageId === p?.id &&
      isCurrent(pendingReveal.current.generation)
    ) {
      const pending = pendingReveal.current;
      settledReveal.current = pending.generation;
      cancelPendingReveal();
      const result = event.payload as RevealResult | null;
      const matchingPage = !!result?.route && Array.isArray(result.frame_path) &&
        samePage(pending.target, { route: result.route, frame_path: result.frame_path });
      if (!matchingPage || !result?.navigation_level) {
        reportNavigation(pending.generation, {
          phase: "failed", message: result?.message || "原型定位没有收到有效回执",
        });
      } else if (pending.pageOnly && result.ok && result.navigation_level === "page") {
        reportNavigation(pending.generation, {
          phase: pageRef || pageBinding ? "located" : "degraded",
          message: result.message,
        });
      } else {
        const binding = pending.binding;
        const controlLocated = !!(binding && result.ok && isTarget(result.target) &&
          samePage(binding.target, result.target) &&
          binding.target.selector === result.target.selector &&
          binding.target.label === result.target.label);
        const exactState = binding?.target.state
          ? result.state_verified && result.navigation_level === "state" &&
            result.observed_state === binding.target.state
          : !result.observed_state;
        if (controlLocated && binding) {
          // A saved binding still identifies the requested knowledge even
          // when the visible control's current state cannot be verified.
          chooseControl(controlForTarget(binding.target), false, false, true);
          setActive(binding);
        }
        reportNavigation(pending.generation, {
          phase: controlLocated && exactState ? "located" : "degraded",
          message: controlLocated && !exactState
            ? "控件可见，无法核验指定状态"
            : result.message,
        });
      }
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
      setPicking(false);
      if (!p) return;
      beginBindingEdit(p.id, t, bindingForTarget(bindings, p.id, t));
      setNotice("已选中控件，请选择文档章节并保存说明");
    } else if (!pinned && !editing && !pendingReveal.current) {
      // Editable elements emit action.activated without control.activated.
      // Keep the displayed target and the assistant/chat context together.
      // Compare the synchronous ref so a richer preceding event is preserved.
      if (!control.current || !sameTarget(control.current.target, t))
        chooseControl(controlForTarget(t));
      else {
        control.current = controlForTarget(t);
        setTarget(t);
        const binding = bindingForTarget(bindings, p?.id, t);
        setActive(binding?.excluded ? null : binding || null);
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
    cancelPendingReveal();
    setAssistantOptions(false);
    setKnowledge(false);
    prototype.current?.send("picker.start");
    navigate({ view: "prototype" });
    setNotice("请点击需要关联的原型控件；按 Esc 或点击取消结束");
  }
  const openDoc = useCallback(
    (c: Citation, b?: Pick<Binding, "id">) => {
      pendingNativeLocation.current = null;
      const relatedBinding = b && bindings.find((item) =>
        item.id === b.id && !item.excluded && item.package_id === p?.id,
      );
      navigate({
        view: "documents",
        doc: c.document_revision_id,
        section: c.revision_id,
        reader: "",
        ...(relatedBinding && p && {
          binding: relatedBinding.id,
          package: p.package_key || p.id,
          page_ref: "",
        }),
      });
      setKnowledge(detached.open);
      if (detached.open) window.focus();
    },
    [navigate, detached.open, bindings, p],
  );
  function existingBindingStopsAutomatic(target: Target) {
    const existing = bindingForTarget(bindings, p?.id, target);
    return !!existing;
  }
  function startReveal(
    packageId: string, target: Pick<Target, "route" | "frame_path">,
    pageOnly: boolean, binding?: Binding,
  ) {
    reportNavigation(generation, { phase: "runtime", message: "正在等待原型运行环境…" });
    pendingReveal.current = {
      generation,
      packageId,
      target,
      binding,
      pageOnly,
      timeout: setTimeout(() => {
        if (pendingReveal.current?.generation !== generation || !isCurrent(generation)) return;
        settledReveal.current = generation;
        cancelPendingReveal();
        reportNavigation(generation, {
          phase: "failed", message: "原型定位超时，请重试或打开原型包目录",
        });
      }, 15000),
    };
    sendPendingReveal();
  }
  function reveal(b: Binding, fromDocument?: { pageOnly: boolean }) {
    const destination = prototypePackages.find(
      (pack) => pack.id === b.package_id,
    );
    if (!destination || b.excluded) return;
    setEditSession(null);
    setKnowledge(!fromDocument);
    setAssistantOptions(false);
    const binding = fromDocument?.pageOnly ? "" : b.id;
    navigate({
      view: "prototype",
      binding,
      package: destination.package_key || destination.id,
      page_ref: "",
    }, false, {
      ...(fromDocument && { vksFromDocument: true }),
      ...(fromDocument?.pageOnly && { vksPageBinding: b.id }),
    });
    documentReveal.current = !!fromDocument;
  }
  function openPrototypeFromChapter(
    b: Binding,
    pageOnly: boolean,
    chapterId: string,
  ) {
    if (editing) {
      setNotice("请先保存或取消当前编辑");
      return;
    }
    const container = documentScrollRoot(reader.current);
    const chapter = document.getElementById("section-" + chapterId);
    const position = {
      route: getRoute(),
      top: container?.scrollTop ?? 0,
      chapter: chapterId,
      chapterOffset:
        container && chapter
          ? chapter.getBoundingClientRect().top -
            container.getBoundingClientRect().top
          : undefined,
    };
    history.replaceState(
      { ...history.state, vksReadingPosition: position },
      "",
    );
    setDocumentOrigin(position);
    setPrototypeChapter(null);
    reveal(b, { pageOnly });
  }
  function returnToReading() {
    if (!documentOrigin || !sameWorkspace(documentOrigin.route, route)) return;
    navigate(documentOrigin.route, false, {
      vksReadingPosition: documentOrigin,
    });
  }
  function editBinding(b: Binding) {
    beginBindingEdit(b.package_id, b.target, b);
  }
  function beginBindingEdit(
    packageId: string,
    target: Target,
    binding?: Binding,
  ) {
    if (!checkBindingsReady()) return;
    if (binding?.excluded) {
      setNotice("此关联已被移除，请先在关联管理中恢复");
      return;
    }
    const destination = prototypePackages.find((pack) => pack.id === packageId);
    if (!destination) return;
    if (packageId !== p?.id)
      navigate({
        package: destination.package_key || destination.id,
        binding: "",
      });
    assistEpoch.current++;
    assistAbort.current?.abort();
    cancelAutomatic();
    setAssistant(null);
    setRelated(null);
    setRetrieving(false);
    setAssistantError("");
    setAssociationReviewTarget(null);
    setAssisting(false);
    control.current = controlForTarget(target, packageId === p?.id);
    setActive(binding || null);
    setTarget(target);
    setTitle(binding?.title || target.label);
    setSummary(binding?.summary || "");
    setEditDocId(binding?.sections[0]?.document_revision_id || "");
    setSectionId(binding?.sections[0]?.revision_id || "");
    setEditSession({ scope, packageId, target, binding });
    setKnowledge(true);
    setAssistantOptions(false);
    if (detached.open) {
      detached.controller.show(false);
      detached.controller.open();
    }
  }
  async function save() {
    if (
      !editSession ||
      editSession.scope !== scope ||
      !catalog?.editable ||
      !sectionId ||
      !summary.trim() ||
      !title.trim()
    )
      return;
    const { target, packageId, binding } = editSession;
    const remainingSections = binding?.sections.slice(1) || [];
    if (
      remainingSections.some((section) => section.revision_id === sectionId)
    ) {
      setError("该章节已被后续引用使用，请为第 1 条原文选择其他章节。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      if (!prototypePackages.some((pack) => pack.id === packageId))
        throw new Error("编辑使用的原型包已变化，请重新打开要编辑的关联");
      const result = await api<{ binding_id: string }>(
        `${prefix}/bindings`,
        mutation(
          "POST",
          {
            analysis_set_id: route.analysis,
            package_id: packageId,
            target,
            point: { title, summary },
            section_revision_ids: [
              sectionId,
              ...remainingSections.map((section) => section.revision_id),
            ],
            ...(binding
              ? {
                  binding_id: binding.id,
                  binding_version: binding.binding_version,
                  point_revision_id: binding.point_revision_id,
                }
              : {}),
          },
          catalog.selection_version,
        ),
      );
      if (!sameWorkspace(getRoute(), route)) return;
      const updated = await reload();
      const b = updated?.b.find((b) => b.id === result.binding_id);
      setActive(b || null);
      setEditSession(null);
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
    if ((e.target as HTMLElement).closest("button") || docked || detached.open)
      return;
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
    // Cover the prototype before the first move, including while its iframe
    // is navigating. Keep a parent-document fallback if capture is unavailable.
    const shield = document.createElement("div");
    shield.className = "assistant-drag-shield";
    shield.setAttribute("aria-hidden", "true");
    document.body.append(shield);
    try {
      button.setPointerCapture(pointer);
    } catch {
      /* The shield and global listeners still receive the drag. */
    }
    const move = (event: PointerEvent) => {
      if (
        event.pointerId !== pointer ||
        (Math.hypot(event.clientX - x, event.clientY - y) < 5 &&
          !launcherMoved.current)
      )
        return;
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
      shield.remove();
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
  const markdownLinkRequest = useRef(0);
  const markdownReading = useMemo<ReadingContext>(
    () => ({
      projectId: route.project,
      analysisSetId: route.release ? undefined : route.analysis,
      releaseId: route.release || undefined,
      selectionVersion: catalog?.selection_version,
      documents: catalog?.documents || [],
      onDocument: (document, fragment) => {
        const request = ++markdownLinkRequest.current;
        const openDocument = (section = "") => {
          if (request !== markdownLinkRequest.current || !isCurrent(generation)) return;
          navigate({ view: "documents", doc: document.revision_id, section });
          if (fragment && !section) setNotice("已打开文档，未找到链接指定的章节");
        };
        if (!fragment) {
          openDocument();
          return;
        }
        const target = document.revision_id === viewDoc?.revision_id
          ? Promise.resolve(viewDoc) : documentCache.load(document.revision_id);
        void target.then((content) => {
          const exact = content.sections.find((section) => section.revision_id === fragment);
          const headings = exact ? [] : content.sections.filter((section) =>
            section.name.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, "")
              .replace(/\s+/g, "-") === fragment.toLowerCase(),
          );
          openDocument(exact?.revision_id || (headings.length === 1 ? headings[0].revision_id : ""));
        }).catch((error) => {
          if (!(error instanceof DOMException && error.name === "AbortError") &&
              request === markdownLinkRequest.current && isCurrent(generation))
            setNotice(text(error));
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
      generation,
      isCurrent,
      navigate,
      openDoc,
      setNotice,
    ],
  );
  const chapterEntries = useMemo(
    () =>
      viewDoc &&
      documentPrototypeEntries(
        viewDoc.revision_id,
        viewDoc.sections,
        bindings,
        catalog?.packages || [],
      ),
    [viewDoc, bindings, catalog?.packages],
  );
  const chapterTools = useCallback(
    (section: MarkdownSection) => {
      const hasEntries =
        chapterEntries && chapterEntries.has(section.revision_id);
      if (!hasEntries && !catalog?.editable) return null;
      return (
        <div className="chapter-actions">
          {hasEntries && (
            <button
              type="button"
              onClick={() => setPrototypeChapter(section.revision_id)}
            >
              <MousePointer2 size={13} /> 查看原型入口
            </button>
          )}
          {catalog?.editable && (
            <button
              type="button"
              className="chapter-association-edit"
              disabled={!!associationBlocked}
              title={associationBlocked || undefined}
              onClick={() => {
                if (editing) {
                  setNotice("请先保存或取消当前编辑");
                  return;
                }
                navigate({ section: section.revision_id });
                setAssociationChapter({
                  revision: section.revision_id,
                  name: section.name,
                  document: viewDoc!.name,
                });
              }}
            >
              <Link2 size={13} /> 将本章关联到原型
            </button>
          )}
        </div>
      );
    },
    [chapterEntries, catalog?.editable, editing, navigate, viewDoc?.name, associationBlocked],
  );
  const titleChapter = viewDoc?.sections.find(
    (section) =>
      section.name === viewDoc.name &&
      (section.heading_level === 0 || /^ {0,3}#\s/.test(section.content)),
  );
  const documentArticle = useMemo(
    () =>
      !originalBook && route.view === "documents" ? (
        <article className="document-reader" ref={reader}>
          <div className="reader-label">
            工作区文档 <span>· 准确修订</span>
          </div>
          <div className="document-title chapter-heading">
            <h1>
              {viewDoc?.name || documentRef?.name || "选择一篇文档开始阅读"}
            </h1>
            {titleChapter && chapterTools(titleChapter)}
          </div>
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
              sectionHeadingActions={chapterTools}
              sectionActions={(s, position) =>
                position === "before" &&
                s.heading_level === 0 &&
                titleChapter?.revision_id !== s.revision_id
                  ? chapterTools(s)
                  : null
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
      editing,
      navigate,
      titleChapter,
      chapterTools,
    ],
  );
  if (checkingSession) return <div className="splash">正在确认登录状态…</div>;
  const handbookPackages =
    catalog?.packages.filter(
      (p) =>
        p.kind === "handbook" &&
        [
          "charge-handbook",
          "standalone-handbook",
          "html-document",
          "static-site",
        ].includes(p.format || ""),
    ) || [];
  function openProject(id: string) {
    navigate({
      project: id,
      analysis: "",
      release: "",
      doc: "",
      section: "",
      binding: "",
      page_ref: "",
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
      page_ref: "",
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
  const scopeFailed = failedScope === prefix + scope;
  const trackingDestination = !!(bindingId || pageRef || inDocuments && route.doc);
  const destinationFeedback = hasDestination && scopeFailed
    ? { phase: "failed", message: missingLocation }
    : navigationStatus || (trackingDestination
      ? { phase: "loading", message: "正在读取指定版本与位置…" }
      : undefined);
  const knownRuntimePosition = previewPosition?.instanceId === prototype.current?.instanceId &&
    previewPosition?.packageId === p?.id && previewPosition?.scope === prefix + scope
    ? previewPosition?.position : undefined;
  const observedBinding = p && active && !active.excluded && active.package_id === p.id &&
    !bindingPageOnly && !pageRef &&
    knownRuntimePosition && samePage(knownRuntimePosition, active.target) &&
    knownRuntimePosition.state === active.target.state &&
    (!route.binding || bindingId === active.id && destinationFeedback?.phase === "located")
    ? active : undefined;
  const canCopyDocument = inDocuments && route.section && documentRef &&
    destinationFeedback?.phase === "located";
  const canCopyBinding = inPrototype && observedBinding;
  const canCopyPage = inPrototype && page && knownRuntimePosition &&
    samePage(page, knownRuntimePosition) &&
    destinationFeedback?.phase === "located";
  function copyPosition() {
    let exact: Partial<Route>;
    if (canCopyDocument)
      exact = { view: "documents", doc: route.doc, section: route.section, reader: "standard" };
    else if (canCopyBinding && p && active)
      exact = { view: "prototype", package: p.package_key || p.id, binding: active.id };
    else if (canCopyPage && p)
      exact = { view: "prototype", package: p.package_key || p.id, page_ref: route.page_ref };
    else return;
    void navigator.clipboard.writeText(exactLocationUrl(route, exact))
      .then(() => setNotice("已复制准确位置"))
      .catch(() => setNotice("无法写入剪贴板，请检查浏览器权限"));
  }
  function openVersionDirectory() {
    navigate({
      binding: "", page_ref: "", package: "",
      doc: "", section: "", reader: "", book: "",
    }, false, { vksVersionDirectory: true });
  }
  function retryPosition() {
    retryNavigation();
    if (scopeFailed) setScopeRetry((attempt) => attempt + 1);
    if (inDocuments) setDocumentRetry((attempt) => attempt + 1);
    if (inPrototype && (!prototype.current || readyRuntime.current !== prototype.current.instanceId))
      setRuntimeRestart((attempt) => attempt + 1);
  }
  const assistantPage =
    currentView === "prototype" || currentView === "documents";
  const inlineKnowledgeVisible =
    assistantPage || (currentView === "links" && editing);
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
  const canReturnToReading =
    currentView === "prototype" &&
    documentOrigin &&
    sameWorkspace(documentOrigin.route, route);
  const selectedEntries =
    prototypeChapter && chapterEntries && chapterEntries.get(prototypeChapter);
  const selectedChapterName = viewDoc?.sections.find(
    (section) => section.revision_id === prototypeChapter,
  )?.name;

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
                if (v === "documents") {
                  setCitationChat(null);
                  if (canReturnToReading) {
                    returnToReading();
                    return;
                  }
                }
                navigate({ view: v });
                if (
                  v === "documents" &&
                  !handbookReader &&
                  !route.doc &&
                  catalog?.documents[0] &&
                  (readerChoice === "standard" ||
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
            <span title={route.release || undefined}>
              {route.release ? `知识快照 · ${route.release.slice(0, 8)}` : "当前草稿"}
            </span>
          </span>
          <button
            aria-label="退出登录"
            className="icon"
            onClick={() => void onLogout()}
          >
            <LogOut size={16} />
          </button>
        </div>
      </header>
      <div className="contextbar">
        <div>
          {canReturnToReading && (
            <button type="button" onClick={returnToReading}>
              <BookOpen size={14} /> 返回文档
            </button>
          )}
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
              disabled={editing}
              value={p?.package_key || p?.id || ""}
              onChange={(e) => {
                cancelPendingReveal();
                setMenu(null);
                navigate({ package: e.target.value, binding: "", page_ref: "" });
              }}
            >
              {!p && <option value="">选择原型包</option>}
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
                ? "返回文档"
                : nativeDetail && !nativeDetail.matches_index
                  ? "查看导入原件"
                  : "查看原文"}
            </button>
          )}
          {currentView === "documents" && nativePackage && accountId && (
            <details className="document-advanced-tools">
              <summary>高级</summary>
              <NativeNetworkSettings
                account={accountId}
                project={route.project}
                packageId={nativePackage.package_key || nativePackage.id}
                onOrigins={(origins) =>
                  setNetwork({ key: networkKey, origins })
                }
              />
            </details>
          )}
          {currentView === "documents" && originalBook && (
            <button onClick={toggleAssistant}>
              <Link2 size={14} />
              关联知识
            </button>
          )}
          {route.release && (canCopyDocument || canCopyBinding || canCopyPage) && (
            <button type="button" onClick={copyPosition}>
              <Copy size={14} /> 复制准确位置
            </button>
          )}
          {inPrototype && p && knownRuntimePosition && (
            <PrototypePreviewAction
              projectId={route.project}
              scope={route.release ? { release_id: route.release } : { analysis_set_id: route.analysis }}
              packageId={p.id}
              route={knownRuntimePosition.route}
              framePath={knownRuntimePosition.frame_path}
              state={knownRuntimePosition.state}
              bindingId={observedBinding?.id}
            />
          )}
          <span className="subtle">
            {catalog ? `${catalog.documents.length} 篇文档` : "正在加载…"}
          </span>
        </div>
      </div>
      {destinationFeedback && (
        <div
          className="navigation-feedback"
          data-phase={destinationFeedback.phase}
          role={destinationFeedback.phase === "failed" ? "alert" : "status"}
        >
          <span>{destinationFeedback.message}</span>
          {(destinationFeedback.phase === "failed" || destinationFeedback.phase === "degraded") && (
            <>
              <button type="button" onClick={retryPosition}>重试定位</button>
              {inPrototype && linkedBinding && p && !bindingPageOnly && (
                <button type="button" onClick={() => setBindingPageFallback(retryNavigation())}>
                  仅打开页面
                </button>
              )}
              <button type="button" onClick={openVersionDirectory}>打开版本目录</button>
            </>
          )}
        </div>
      )}
      {error && !(hasDestination && scopeFailed) && (
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
          (currentView === "links" ? "links-workspace " : "") +
          (knowledge &&
          docked &&
          !detached.open &&
          !documentChat &&
          inlineKnowledgeVisible
            ? "with-dock"
            : "")
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
                  key={p.id + scope + `:${runtimeRestart}` + (pageRef ? `:${generation}` : "")}
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
                  onFailure={(message) => {
                    if (!trackingDestination || !isCurrent(generation)) return;
                    settledReveal.current = generation;
                    cancelPendingReveal();
                    reportNavigation(generation, { phase: "failed", message });
                  }}
                />
              )}
            </>
          ) : !hasDestination && (
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
        {currentView === "documents" && handbookReader && (
          <HandbookTemplate
            key={documentCache.id}
            projectName={
              projects.find((project) => project.id === route.project)?.name ||
              "项目知识库"
            }
            documents={catalog?.documents || []}
            navigation={catalog?.navigation}
            selectedDocument={route.doc}
            selectedSection={route.section}
            currentDocument={viewDoc}
            scopeKey={route.project + ":" + scope}
            selectionVersion={catalog?.selection_version}
            loadDocument={(revision) => documentCache.load(revision)}
            searchDocuments={(query, signal) =>
              api<HandbookSearchResult[]>(
                `/api/v3/projects/${route.project}/document-presentation/search`,
                {
                  ...mutation("POST", {
                    ...(route.release
                      ? { release_id: route.release }
                      : { analysis_set_id: route.analysis }),
                    query,
                  }),
                  signal,
                },
              )
            }
            onOpen={(doc, section = "") =>
              navigate({ view: "documents", doc, section, reader: "handbook" })
            }
            onOverview={() =>
              navigate({ doc: "", section: "", reader: "handbook" })
            }
            onAskAI={() => {
              if (editing) {
                setNotice("请先保存或取消当前编辑");
                return;
              }
              setCitationChat(null);
              setKnowledge(true);
              setAssistantOptions(false);
              switchKnowledge("chat");
            }}
            onUpdate={() => navigate({ view: "materials" })}
            readOnly={!catalog?.editable}
            renderReader={documentArticle}
          />
        )}
        {currentView === "documents" && !handbookReader && (
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
              requestKey={`${route.project}:${scope}:${generation}`}
              onLocation={nativeLocation}
              onLocated={() => reportNavigation(generation, {
                phase: "located",
                message: route.section ? "已定位到原文章节" : "已打开指定原文",
              })}
              onFailure={(message) => {
                if (!isCurrent(generation)) return;
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
            onAssociations={() => navigate({ view: "links" })}
            onApplying={(active) =>
              setApplyingMaterials((scopes) => {
                const { [migrationScope]: count = 0, ...others } = scopes;
                if (active) return { ...others, [migrationScope]: count + 1 };
                if (count > 1) return { ...others, [migrationScope]: count - 1 };
                return others;
              })
            }
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
        <div
          hidden={currentView !== "links"}
          className="association-management-layout"
        >
          {catalog && (
            <div className="association-management-overview">
              <BatchAssociations
                key={"batch:" + route.project + scope}
                project={route.project}
                analysis={catalog.analysis_set_id}
                accountId={accountId || ""}
                visible={currentView === "links"}
                editable={!!catalog.editable && !route.release}
                associationBlocked={associationBlocked}
                onChanged={reload}
                refreshVersion={catalog.selection_version}
                onReview={(mode) =>
                  setAssociationReviewTarget({ mode, prototype: false })
                }
              />
              {currentView === "links" && route.project && (
                <ProjectTokenUsage
                  key={route.project}
                  project={route.project}
                  refreshVersion={catalog.selection_version}
                />
              )}
            </div>
          )}
          {currentView === "links" && (
            <BindingsManager
              key={"bindings:" + route.project + scope}
              bindings={bindings}
              packages={catalog?.packages}
              inventoryUrl={
                !route.release && route.analysis
                  ? `/api/v3/projects/${route.project}/associations/inventory?analysis_set_id=${encodeURIComponent(route.analysis)}`
                  : undefined
              }
              ready={bindingsReady}
              editable={!!catalog?.editable}
              associationBlocked={associationBlocked}
              canCreate={!!p}
              onCreate={startPicker}
              onReveal={reveal}
              onEdit={editBinding}
              onReview={(items) =>
                setAssociationReviewTarget({ items, prototype: false })
              }
              renderBatchExplanation={catalog?.editable && !route.release ? (bindingIds) => (
                <BatchExplanations
                  project={route.project}
                  analysis={catalog.analysis_set_id}
                  version={catalog.selection_version}
                  bindingIds={bindingIds}
                  blockedReason={associationBlocked}
                />
              ) : undefined}
              onToggle={(b) => void exclude(b)}
              onSource={(b, i) => openDoc(b.sections[i], b)}
              renderSummary={(b) => (
                <Markdown references={b.sections} hideCitationRevisionIds>{b.summary}</Markdown>
              )}
            />
          )}
        </div>
      </main>
      {currentView === "documents" &&
        !originalBook &&
        prototypeChapter &&
        selectedEntries &&
        selectedChapterName && (
          <DocumentPrototypeEntries
            chapter={selectedChapterName}
            entries={selectedEntries}
            onClose={() => setPrototypeChapter(null)}
            onOpen={(binding, pageOnly) =>
              openPrototypeFromChapter(binding, pageOnly, prototypeChapter)
            }
          />
        )}
      {menu && currentView === "prototype" && menu.packageId === p?.id && (
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
          <button role="menuitem" disabled={!!associationBlocked} title={associationBlocked || undefined}
            onClick={() => void assist(menu, "explain")}>
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
            disabled={!!associationBlocked}
            title={associationBlocked || undefined}
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
              const b = bindingForTarget(bindings, menu.packageId, menu.target);
              setMenu(null);
              beginBindingEdit(menu.packageId, menu.target, b);
            }}
          >
            <Link2 size={16} />
            手动补充关联
          </button>
        </div>
      )}
      {catalog && assistantPage && !(knowledge && docked && !detached.open) && (
        <button
          ref={assistantTrigger}
          className={
            "assistant-launcher" +
            (currentView === "documents" && originalBook
              ? " native-reader-launcher"
              : "") +
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
          aria-label={
            knowledge && !detached.open ? "收起知识助手" : "打开知识助手"
          }
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
          title={
            detached.open
              ? "打开独立窗口"
              : retrieving || assisting
                ? "正在整理相关知识"
                : "知识助手"
          }
        >
          {detached.open ? (
            <ArrowUpRight size={20} />
          ) : knowledge ? (
            <X size={20} />
          ) : (
            <MessageSquareText size={21} strokeWidth={1.75} />
          )}
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
            {detached.open ? "独立窗口" : knowledge ? "收起助手" : "知识助手"}
          </span>
        </button>
      )}
      {catalog && (
        <DocumentAssistant
          key={accountId + route.project + scope}
          visible={knowledge && documentChat && assistantPage}
          reading={currentView === "documents"}
          detachedWindow={detached.controller}
          detachedOpen={detached.open}
          onDetach={detachAssistant}
          scopeKey={accountId + route.project + scope + chatSources}
          prefix={prefix}
          baseline={
            route.release
              ? { release_id: route.release }
              : { analysis_set_id: route.analysis }
          }
          document={documentRef || undefined}
          documents={catalog.documents.length}
          onClose={collapseAssistant}
          onAskSelection={() => {
            setCitationChat(null);
            setKnowledge(true);
            setAssistantOptions(false);
            switchKnowledge("chat");
            if (detached.open) detached.controller.open();
          }}
          onSettings={() => navigate({ view: "ai" })}
          onIndex={() => navigate({ view: "ai" })}
          onCitation={openDoc}
          onUpdated={async (operations) => {
            const c = await api<Catalog>(`${prefix}/catalog?${scope}`);
            const changed = operations.find(
              (operation) => operation.kind === "document",
            );
            if (changed?.revision_id)
              navigate({ doc: changed.revision_id, section: "" }, true);
            setCatalog(c);
          }}
        />
      )}
      {knowledge && !documentChat && catalog && (
        <DetachedPortal
          target={detached.open ? detached.controller.prototypeMount : null}
        >
          <aside
            ref={windowRef}
            id="knowledge-assistant"
            aria-label="知识助手"
            hidden={!inlineKnowledgeVisible && !detached.open}
            className={"knowledge-window " + (docked ? "docked" : "")}
            style={floating.style}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
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
                  className="icon assistant-detach-open"
                  aria-label="弹出独立窗口"
                  onClick={detachAssistant}
                >
                  <ArrowUpRight size={16} />
                </button>
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
                  aria-label={editing ? "关闭关联编辑" : "收起助手面板"}
                  disabled={saving}
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
                  <span>
                    {documentChat
                      ? documentChatLabel
                      : target?.label || "请选择一个原型操作"}
                  </span>
                  <span className="pill">
                    {documentChat
                      ? "文档问答"
                      : editing
                        ? "维护关联"
                        : target?.selector === "body"
                          ? "模块知识"
                          : "控件知识"}
                  </span>
                </div>
                {!editing && (target || documentChat) && (
                  <div
                    className="knowledge-tabs"
                    role="tablist"
                    aria-label="知识内容"
                  >
                    {!documentChat && (
                      <button
                        role="tab"
                        aria-selected={assistantMode === "documents"}
                        onClick={() => switchKnowledge("documents")}
                      >
                        文档
                      </button>
                    )}
                    {!documentChat && (
                      <button
                        role="tab"
                        aria-selected={assistantMode === "explain"}
                        disabled={!!associationBlocked}
                        title={associationBlocked || undefined}
                        onClick={() => switchKnowledge("explain")}
                      >
                        AI 讲解
                      </button>
                    )}
                    <button
                      role="tab"
                      aria-selected={assistantMode === "chat"}
                      onClick={() => switchKnowledge("chat")}
                    >
                      回答
                    </button>
                  </div>
                )}
                <div
                  className={
                    "knowledge-body" +
                    (assistantMode === "chat" && !editing ? " chat-body" : "")
                  }
                >
                  {!editing &&
                    assistantMode === "chat" &&
                    (target || documentChat) && (
                      <KnowledgeChat
                        chat={chat}
                        prefix={prefix}
                        label={documentChat ? documentChatLabel : target!.label}
                        onCitation={(reference) => {
                          setCitationChat({
                            scope: route.project + scope,
                            key: chatKey,
                            documentChat,
                            context: chatContext,
                            documentId: chatDocument?.revision_id,
                            packageId: chatPackage,
                            sourceFingerprint: chatSources,
                          });
                          openDoc(reference);
                        }}
                        onSettings={() => navigate({ view: "ai" })}
                      />
                    )}
                  <div
                    hidden={
                      documentChat || (!editing && assistantMode === "chat")
                    }
                  >
                    {!editing &&
                      active &&
                      active.validity_status !== "valid" && (
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
                    {!editing && assistantError && (
                      <div
                        className={
                          related?.sections.length
                            ? "assistant-error assistant-note"
                            : "assistant-error"
                        }
                        role="alert"
                      >
                        <b>
                          {lastAssist.current?.mode === "explain"
                            ? "暂未完成讲解"
                            : "暂未完成关联"}
                        </b>
                        <p>{assistantError}</p>
                        <button
                          disabled={!!associationBlocked}
                          onClick={() => {
                            const previous = lastAssist.current;
                            if (previous)
                              void assist(
                                previous.control,
                                previous.mode,
                                true,
                                true,
                              );
                            else if (control.current)
                              void assist(control.current, "explain", true);
                          }}
                        >
                          重试
                        </button>
                        <button onClick={() => navigate({ view: "ai" })}>
                          AI 设置
                        </button>
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
                          {assistantMode === "documents" && active?.summary && (
                            <section aria-label="关联说明">
                              <span className="eyebrow">关联说明</span>
                              <Markdown
                                references={active.sections}
                                hideCitationRevisionIds
                                onReference={(reference) =>
                                  previewSource(reference, active)
                                }
                              >
                                {active.summary}
                              </Markdown>
                            </section>
                          )}
                          {!related.sections.length && (
                            <p>尚未找到相关章节，可在文档页搜索或补充资料。</p>
                          )}
                          {related.sections.length > 0 && (
                            <ReferenceDisclosure
                              key={`related:${JSON.stringify(target)}`}
                              count={related.sections.length}
                            >
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
                              {related.sections.map((c) => (
                                <button
                                  key={c.revision_id}
                                  className="citation"
                                  onClick={() =>
                                    previewSource(
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
                            </ReferenceDisclosure>
                          )}
                          {!!related.related_sections?.length && (
                            <details className="more-related-sections">
                              <summary>
                                更多本地相关章节 ·{" "}
                                {related.related_sections.length}
                              </summary>
                              <p className="assistant-provenance">
                                以下是检索候选，可查看核对；尚未保存为当前关联。
                              </p>
                              {related.related_sections.map((c) => (
                                <button
                                  key={c.revision_id}
                                  className="citation"
                                  onClick={() => previewSource(c)}
                                >
                                  <FileText size={18} />
                                  <span>
                                    {c.document_name}
                                    <small>{c.name}</small>
                                  </span>
                                  <ArrowUpRight size={15} />
                                </button>
                              ))}
                            </details>
                          )}
                        </section>
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
                        <div className="assistant-answer-title">
                          <h2>{assistant.title}</h2>
                        </div>
                        {explanationStale && (
                          <p className="assistant-background">
                            关联已更新，可点击“重新讲解”生成新的讲解。
                          </p>
                        )}
                        {assistantMode === "explain" && (
                          <Markdown
                            references={assistant.sections}
                            onReference={(reference) =>
                              previewSource(
                                reference,
                                assistant.binding_id
                                  ? { id: assistant.binding_id }
                                  : undefined,
                              )
                            }
                          >
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
                          {active
                            ? "根据已关联章节生成讲解"
                            : "根据当前检索章节生成讲解"}
                        </p>
                        {assistant.sections.length > 0 && (
                          <ReferenceDisclosure
                            key={`explain:${JSON.stringify(target)}`}
                            count={assistant.sections.length}
                          >
                            <details className="inline-preview">
                              <summary>在小窗查看原文</summary>
                              {assistant.sections.map((c) => (
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
                            {assistant.sections.map((c) => (
                              <button
                                key={c.revision_id}
                                className="citation"
                                onClick={() =>
                                  previewSource(
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
                          </ReferenceDisclosure>
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
                            maxLength={512000}
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
                          {active && active.sections.length > 1
                            ? "第 1 条原文"
                            : "准确章节"}
                          <select
                            aria-label="关联章节"
                            value={sectionId}
                            onChange={(e) => setSectionId(e.target.value)}
                          >
                            <option value="">请选择章节</option>
                            {editDoc?.sections.map((s) => (
                              <option
                                key={s.id}
                                value={s.revision_id}
                                disabled={active?.sections
                                  .slice(1)
                                  .some(
                                    (section) =>
                                      section.revision_id === s.revision_id,
                                  )}
                              >
                                {s.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        {active && active.sections.length > 1 && (
                          <details className="inline-preview">
                            <summary>
                              保留其余 {active.sections.length - 1} 条原文依据
                            </summary>
                            <ol start={2}>
                              {active.sections.slice(1).map((section) => (
                                <li key={section.revision_id}>
                                  {section.document_name} / {section.name}
                                </li>
                              ))}
                            </ol>
                          </details>
                        )}
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
                      retrieving ? null : assistantMode === "documents" &&
                      active &&
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
                        {assistantMode === "documents" && (
                          <Markdown
                            references={active.sections}
                            hideCitationRevisionIds
                            onReference={(reference) =>
                              previewSource(reference, active)
                            }
                          >
                            {active.summary}
                          </Markdown>
                        )}
                        <ReferenceDisclosure
                          key={`saved:${active.id}:${active.point_revision_id}`}
                          count={active.sections.length}
                        >
                          <details className="inline-preview">
                            <summary>在小窗查看原文</summary>
                            {active.sections.map((c) => (
                              <Markdown
                                key={c.revision_id}
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
                            ))}
                          </details>
                          {active.sections.map((c) => (
                            <button
                              key={c.revision_id}
                              className="citation"
                              onClick={() => previewSource(c, active)}
                            >
                              <FileText size={18} />
                              <span>
                                {c.document_name}
                                <small>{c.name}</small>
                              </span>
                              <ArrowUpRight size={15} />
                            </button>
                          ))}
                        </ReferenceDisclosure>
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
                        {target && assistantMode === "explain" && (
                          <button
                            className="primary"
                            disabled={!!associationBlocked}
                            title={associationBlocked || undefined}
                            onClick={() =>
                              control.current &&
                              void assist(control.current, "explain")
                            }
                          >
                            <Sparkles size={15} />
                            生成 AI 讲解
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
                      <button disabled={saving} onClick={collapseAssistant}>
                        取消
                      </button>
                      <button
                        className="primary"
                        disabled={
                          saving ||
                          !summary.trim() ||
                          !title.trim() ||
                          !sectionId
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
                        {documentChat
                          ? "根据项目文档回答"
                          : assistantMode === "explain"
                            ? assistant
                              ? "讲解已生成"
                              : "功能讲解"
                            : active
                              ? assistantError
                                ? "已有关联已保留"
                                : "关联已保存"
                              : "跟随原型操作"}
                      </span>
                      {!documentChat &&
                        target &&
                        assistantMode !== "chat" &&
                        (assistantMode === "explain" || catalog?.editable) && (
                          <button
                            disabled={
                              assisting || !bindingsReady || active?.excluded || !!associationBlocked
                            }
                            title={associationBlocked || undefined}
                            onClick={() =>
                              assistantMode === "explain"
                                ? control.current &&
                                  void assist(control.current, "explain", true)
                                : openReassociation()
                            }
                          >
                            <Sparkles size={14} />{" "}
                            {assistantMode === "explain"
                              ? "重新讲解"
                              : active
                                ? "AI 重新关联"
                                : "AI 关联"}
                          </button>
                        )}
                      {!documentChat &&
                        active &&
                        assistantMode === "documents" && (
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
                [
                  "n",
                  "s",
                  "e",
                  "w",
                  "nw",
                  "ne",
                  "sw",
                  "se",
                ] as ResizeDirection[]
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
            {sourcePreview && (
              <SourcePreview
                reference={sourcePreview.reference}
                returnLabel="返回"
                onClose={() => setSourcePreview(null)}
                onOpen={() => {
                  openDoc(sourcePreview.reference, sourcePreview.binding);
                  setSourcePreview(null);
                }}
              />
            )}
          </aside>
        </DetachedPortal>
      )}
      {(associationReviewTarget ||
        (route.review && currentView === "links" && catalog?.editable && !route.release)) && (
        <DetachedPortal
          target={
            associationReviewTarget?.prototype && detached.open
              ? detached.controller.prototypeMount
              : null
          }
        >
          <AssociationReview
            key={route.review || "new-review"}
            project={route.project}
            analysis={route.analysis}
            items={associationReviewTarget?.items}
            mode={associationReviewTarget?.mode}
            resumeJobId={route.review || undefined}
            blockedReason={associationBlocked}
            addedChapter={associationReviewTarget?.addedChapter}
            onClose={() => {
              setAssociationReviewTarget(null);
              if (route.review) navigate({ review: "" }, true);
            }}
            onSaved={associationSaved}
          />
        </DetachedPortal>
      )}
      {associationChapter &&
        catalog?.editable &&
        route.view === "documents" && (
          <DocumentAssociation
            key={route.project + scope + route.doc}
            project={route.project}
            analysis={route.analysis}
            chapter={associationChapter}
            onClose={() => setAssociationChapter(null)}
            onSelect={(target) => {
              setAssociationReviewTarget({
                prototype: false,
                items: [target],
                addedChapter: {
                  name: associationChapter.name,
                  document: associationChapter.document,
                },
              });
              setAssociationChapter(null);
            }}
          />
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
