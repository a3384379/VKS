import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import { ApiError, api } from "./api";
import {
  PrototypeBindingEditor,
  PrototypeStateEditor,
  type StatePreview,
} from "./PrototypeMaintenance";
import { Button, Notice, Panel } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import {
  prototypeApi,
  prototypeManifest,
  prototypeScope,
  type PrototypeAnchor,
  type PrototypeContext,
  type PrototypeManifest,
  type PrototypeState,
} from "./prototype";
import type { Entity } from "./types";
import "./prototype-explorer.css";

type SurfaceView = { fit: boolean; hotspots: boolean; x: number; y: number };
const initialView = { fit: true, hotspots: true, x: 0, y: 0 };
const drawerMedia = "(min-width: 760px) and (width < 1100px)";

function savedView(key: string): SurfaceView {
  try {
    return {
      ...initialView,
      ...JSON.parse(sessionStorage.getItem(key) || "{}"),
    };
  } catch {
    return initialView;
  }
}

function PrototypeSurface({
  state,
  url,
  viewKey,
  actions,
  selected,
  preview,
  selectedAnchor,
  onSelect,
  onDraw,
  onRetry,
}: {
  state: PrototypeState;
  url: string;
  viewKey: string;
  actions: Map<string, Entity>;
  selected?: string;
  preview?: StatePreview;
  selectedAnchor?: string;
  onSelect: (anchor: PrototypeAnchor) => void;
  onDraw?: (rect: PrototypeAnchor["rect"]) => void;
  onRetry: () => void;
}) {
  const [view, setView] = useState(() => savedView(viewKey));
  const [image, setImage] = useState<"loading" | "loaded" | "failed">(
    "loading",
  );
  const scroll = useRef<HTMLDivElement>(null);
  const start = useRef<[number, number] | null>(null);
  const [drawingRect, setDrawingRect] = useState<
    PrototypeAnchor["rect"] | null
  >(null);
  function position(event: PointerEvent<HTMLDivElement>): [number, number] {
    const bounds = event.currentTarget.getBoundingClientRect();
    const clamp = (value: number) => Math.max(0, Math.min(1, value));
    return [
      clamp((event.clientX - bounds.left) / bounds.width),
      clamp((event.clientY - bounds.top) / bounds.height),
    ];
  }
  useLayoutEffect(() => {
    if (scroll.current && image === "loaded") {
      const saved = savedView(viewKey);
      scroll.current.scrollTo(saved.x, saved.y);
    }
  }, [image, viewKey]);
  function save(next: SurfaceView) {
    try {
      sessionStorage.setItem(viewKey, JSON.stringify(next));
    } catch {
      // Browsing still works when storage is disabled.
    }
  }
  function change(next: Partial<SurfaceView>) {
    const value = {
      ...view,
      ...next,
      x: scroll.current?.scrollLeft || 0,
      y: scroll.current?.scrollTop || 0,
    };
    setView(value);
    save(value);
  }
  const viewport = preview?.image ? preview.viewport : state.viewport;
  const anchors =
    preview?.anchors ||
    state.anchors
      .filter((anchor) => actions.has(anchor.actionKey))
      .map((anchor) => ({
        ...anchor,
        label: actions.get(anchor.actionKey)!.name,
      }));
  function dragRect(event: PointerEvent<HTMLDivElement>) {
    const [x, y] = start.current!;
    const [left, top] = position(event);
    return [
      Math.min(left, x),
      Math.min(top, y),
      Math.abs(left - x),
      Math.abs(top - y),
    ] as PrototypeAnchor["rect"];
  }
  return (
    <>
      {url && (
        <div className="prototype-v3-tools" role="group" aria-label="画布显示">
          <Button
            aria-pressed={!view.fit}
            onClick={() => change({ fit: false })}
          >
            原尺寸
          </Button>
          <Button aria-pressed={view.fit} onClick={() => change({ fit: true })}>
            适应宽度
          </Button>
          <Button
            aria-pressed={view.hotspots}
            onClick={() => change({ hotspots: !view.hotspots })}
          >
            显示热区
          </Button>
        </div>
      )}
      {!url || !viewport ? (
        <Notice>该状态画面待补充</Notice>
      ) : image === "failed" ? (
        <Notice tone="red">
          原型画面无法读取。 <Button onClick={onRetry}>重试画面</Button>
        </Notice>
      ) : (
        <div
          className="prototype-v3-scroll"
          aria-label="原型画布"
          role="region"
          tabIndex={0}
          ref={scroll}
          onScroll={(event) =>
            save({
              ...view,
              x: event.currentTarget.scrollLeft,
              y: event.currentTarget.scrollTop,
            })
          }
        >
          <div
            className="prototype-v3-frame"
            style={{
              width: viewport.width,
              maxWidth: view.fit ? "100%" : undefined,
              aspectRatio: viewport.width + " / " + viewport.height,
            }}
          >
            <img
              src={url}
              alt={state.name + "原型画面"}
              onLoad={() => setImage("loaded")}
              onError={() => setImage("failed")}
            />
            {image === "loading" && <p role="status">正在加载原型画面…</p>}
            {image === "loaded" && onDraw && (
              <div
                className="prototype-v3-draw"
                aria-label="拖拽绘制热区"
                onPointerDown={(event) => {
                  if (!event.isPrimary || event.button !== 0) return;
                  event.currentTarget.setPointerCapture(event.pointerId);
                  start.current = position(event);
                }}
                onPointerMove={(event) => {
                  if (start.current && event.isPrimary)
                    setDrawingRect(dragRect(event));
                }}
                onPointerCancel={() => {
                  start.current = null;
                  setDrawingRect(null);
                }}
                onPointerUp={(event) => {
                  if (!start.current || !event.isPrimary) return;
                  const rect = dragRect(event);
                  start.current = null;
                  setDrawingRect(null);
                  if (rect[2] > 0.003 && rect[3] > 0.003) onDraw(rect);
                }}
              >
                {drawingRect && (
                  <div
                    className="prototype-v3-draw-preview"
                    style={{
                      left: drawingRect[0] * 100 + "%",
                      top: drawingRect[1] * 100 + "%",
                      width: drawingRect[2] * 100 + "%",
                      height: drawingRect[3] * 100 + "%",
                    }}
                  />
                )}
              </div>
            )}
            {image === "loaded" &&
              !onDraw &&
              (view.hotspots || preview) &&
              anchors.map((anchor) => {
                const [x, y, width, height] = anchor.rect.map(
                  (value) => value * 100,
                );
                return (
                  <button
                    type="button"
                    key={anchor.anchorKey}
                    aria-label={anchor.label || "未命名热区"}
                    aria-pressed={
                      preview
                        ? selectedAnchor === anchor.anchorKey
                        : selected === anchor.actionKey
                    }
                    className="prototype-v3-hotspot"
                    style={{
                      left: x + "%",
                      top: y + "%",
                      width: width + "%",
                      height: height + "%",
                    }}
                    onClick={() => onSelect(anchor)}
                  />
                );
              })}
          </div>
        </div>
      )}
      {anchors.length > 0 && (
        <nav className="prototype-v3-tools" aria-label="当前状态的动作">
          {anchors.map((anchor) => (
            <Button
              key={anchor.anchorKey}
              aria-pressed={
                preview
                  ? selectedAnchor === anchor.anchorKey
                  : selected === anchor.actionKey
              }
              onClick={() => onSelect(anchor)}
            >
              {anchor.label || "未命名热区"}
            </Button>
          ))}
        </nav>
      )}
    </>
  );
}

export function PrototypeExplorer({
  page,
  manifest,
}: {
  page: Entity;
  manifest: PrototypeManifest;
}) {
  const {
    w,
    session,
    explanation,
    navigateExplanation,
    navigate,
    notify,
    refresh,
    setUnsavedChanges,
  } = useWorkspace();
  const readScope = useReadScope();
  const canEdit =
    usePermission("knowledge:edit") &&
    !w.selected_release &&
    w.selected_analysis_set?.state === "draft";
  const [editor, setEditor] = useState(false);
  const [stateDirty, setStateDirty] = useState(false);
  const [bindingDirty, setBindingDirty] = useState(false);
  const [preview, setPreview] = useState<StatePreview>();
  const [selectedAnchor, setSelectedAnchor] = useState("");
  const [pendingState, setPendingState] = useState<{
    pageId: string;
    key: string;
  }>();
  const [contextAttempt, setContextAttempt] = useState(0);
  const [imageAttempt, setImageAttempt] = useState(0);
  const [drawerMode, setDrawerMode] = useState(
    () => matchMedia(drawerMedia).matches,
  );
  const [showExplanation, setShowExplanation] = useState(!drawerMode);
  const drawerOpen = drawerMode && showExplanation;
  const contextId = useId();
  const contextPanel = useRef<HTMLDivElement>(null);
  const contextToggle = useRef<HTMLButtonElement>(null);
  const drawerClose = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const media = matchMedia(drawerMedia);
    function resize() {
      setDrawerMode(media.matches);
      setShowExplanation(!media.matches);
    }
    media.addEventListener("change", resize);
    return () => media.removeEventListener("change", resize);
  }, []);
  const [drawing, setDrawing] = useState(false);
  const [drawn, setDrawn] = useState<{
    surface: string;
    rect: PrototypeAnchor["rect"];
  } | null>(null);
  const [pageSearch, setPageSearch] = useState("");
  const [showOutdated, setShowOutdated] = useState(false);
  const [response, setResponse] = useState<{
    key: string;
    data?: PrototypeContext;
    error?: string;
  } | null>(null);
  const focusState = useRef<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const state =
    manifest.states.find((row) => row.stateKey === explanation.stateKey) ||
    manifest.states.find((row) => row.stateKey === manifest.defaultStateKey)!;
  const actions = new Map(
    w.actions
      .filter(
        (row) =>
          row.metadata.prototype_action &&
          row.metadata.page_key === page.stable_key &&
          row.metadata.status !== "removed",
      )
      .map((row) => [row.stable_key, row]),
  );
  const selectedAction = [...actions.values()].find(
    (row) => row.id === explanation.actionId,
  );
  const trigger = manifest.states.find(
    (row) => row.stateKey === explanation.triggerStateKey,
  );
  const anchor = trigger?.anchors.find(
    (row) => row.actionKey === selectedAction?.stable_key,
  );
  const target = manifest.states.find(
    (row) => row.stateKey === anchor?.targetStateKey,
  );
  const selected =
    anchor && (state === trigger || (state === target && target?.surface))
      ? selectedAction
      : undefined;
  const releaseId = w.selected_release?.id;
  const analysisId = w.selected_analysis_set?.id;
  const root = prototypeApi(w.project.id);
  const contextQuery = prototypeScope(w);
  contextQuery.set("page_key", page.stable_key);
  contextQuery.set("trigger_state_key", trigger?.stateKey || "");
  contextQuery.set("policy", "all");
  const contextUrl =
    selected && trigger && (releaseId || analysisId)
      ? `${root}/actions/${encodeURIComponent(selected.stable_key)}/context?${contextQuery}`
      : "";
  const contextKey = JSON.stringify([contextUrl, readScope, contextAttempt]);
  const context = response?.key === contextKey ? response : null;
  const points =
    context?.data?.points.filter(
      (point) => showOutdated || point.validity === "valid",
    ) || [];
  const documents =
    context?.data?.documents.filter(
      (source) => showOutdated || source.validity === "valid",
    ) || [];
  const hasOutdated =
    context?.data?.points.some(
      (point) =>
        point.validity !== "valid" ||
        point.sources.some((source) => source.validity !== "valid"),
    ) || context?.data?.documents.some((source) => source.validity !== "valid");
  useEffect(() => {
    if (!contextUrl) return;
    const request = new AbortController();
    api<PrototypeContext>(contextUrl, { signal: request.signal }).then(
      (data) => {
        if (!request.signal.aborted) setResponse({ key: contextKey, data });
      },
      (error: Error) => {
        if (!request.signal.aborted) {
          setResponse({ key: contextKey, error: error.message });
          if (
            error instanceof ApiError &&
            [401, 403, 404].includes(error.status)
          )
            void refresh().catch(() => {});
        }
      },
    );
    return () => request.abort();
  }, [contextKey, contextUrl, refresh]);
  const imageQuery = prototypeScope(w);
  imageQuery.set("page_revision_id", page.entity_revision_id);
  const url =
    state.surface && (releaseId || analysisId)
      ? `${root}/pages/${encodeURIComponent(page.stable_key)}/states/${encodeURIComponent(state.stateKey)}/image?${imageQuery}`
      : "";
  const showSource = (id: string, revisionId: string) => {
    const row = w.entities.find(
      (row) => row.id === id && row.entity_revision_id === revisionId,
    );
    if (row) navigate("documents", row);
    else notify("当前版本无法定位原文章节。可从版本记录查阅历史出处。", "info");
  };
  const viewKey =
    "vks-prototype-view:" +
    JSON.stringify([
      session.actor_id,
      w.project.id,
      releaseId || analysisId,
      page.entity_revision_id,
      state.stateKey,
    ]);
  const stateEditorKey = JSON.stringify([page.id, state.stateKey]);
  const bindingEditorKey = JSON.stringify([selected?.id, trigger?.stateKey]);
  const statePreview = canEdit && editor ? preview : undefined;
  const dirty = canEdit && editor && (stateDirty || (selected && bindingDirty));
  useLayoutEffect(() => {
    setUnsavedChanges(!!dirty);
    return () => setUnsavedChanges(false);
  }, [dirty, setUnsavedChanges]);
  useLayoutEffect(() => {
    setDrawn(null);
    setDrawing(false);
    setPreview(undefined);
    setSelectedAnchor("");
    setStateDirty(false);
  }, [stateEditorKey]);
  useLayoutEffect(() => {
    setBindingDirty(false);
    setShowOutdated(false);
  }, [bindingEditorKey]);
  useEffect(() => {
    if (!pendingState) return;
    if (pendingState.pageId !== page.id || pendingState.key === state.stateKey)
      setPendingState(undefined);
    else if (
      !dirty &&
      manifest.states.some((row) => row.stateKey === pendingState.key)
    ) {
      setPendingState(undefined);
      navigateExplanation({
        pageId: page.id,
        stateKey: pendingState.key,
        actionId: "",
      });
    }
  }, [
    pendingState,
    dirty,
    manifest,
    page.id,
    state.stateKey,
    navigateExplanation,
  ]);
  useLayoutEffect(() => {
    if (focusState.current === state.stateKey) {
      focusState.current = null;
      heading.current?.focus({ preventScroll: true });
    }
  }, [state.stateKey]);
  useLayoutEffect(() => {
    const panel = contextPanel.current;
    if (!drawerOpen || !panel) return;
    const previous = document.activeElement;
    drawerClose.current?.focus();
    function keepFocus(event: FocusEvent) {
      if (event.target instanceof Node && !panel!.contains(event.target))
        drawerClose.current?.focus();
    }
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setShowExplanation(false);
      }
      if (event.key !== "Tab") return;
      const buttons = [
        ...panel!.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary",
        ),
      ].filter(
        (node) => node.getClientRects().length && !node.matches(":disabled"),
      );
      const first = buttons[0];
      const last = buttons.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    document.addEventListener("focusin", keepFocus);
    document.addEventListener("keydown", handleKey, true);
    return () => {
      document.removeEventListener("focusin", keepFocus);
      document.removeEventListener("keydown", handleKey, true);
      const target =
        previous instanceof HTMLElement && previous.isConnected
          ? previous
          : contextToggle.current;
      target?.focus({ preventScroll: true });
    };
  }, [drawerOpen]);
  function activate(row: PrototypeAnchor) {
    if (editor) setSelectedAnchor(row.anchorKey);
    const action = actions.get(row.actionKey);
    if (!action) return;
    if (editor && (dirty || statePreview?.creating)) return;
    const changed = navigateExplanation({
      pageId: page.id,
      stateKey: state.stateKey,
      triggerStateKey: state.stateKey,
      actionId: action.id,
      followTarget: !editor,
    });
    if (!changed) return;
    setShowExplanation(true);
    if (
      !editor &&
      manifest.states.some(
        (candidate) =>
          candidate.stateKey === row.targetStateKey &&
          candidate.stateKey !== state.stateKey &&
          candidate.surface,
      )
    )
      focusState.current = row.targetStateKey!;
  }
  const groupLabels = new Map(
    Object.entries(w.prototype_settings?.menu_groups || {}),
  );
  const showPageSearch =
    w.pages.length > 10 ||
    w.pages.some((row) => groupLabels.has(row.stable_key));
  const filter = showPageSearch ? pageSearch.trim().toLocaleLowerCase() : "";
  const groupedPages = new Map<string, Entity[]>();
  const pageNameCounts = new Map<string, number>();
  for (const row of w.pages) {
    pageNameCounts.set(row.name, (pageNameCounts.get(row.name) || 0) + 1);
    const group = groupLabels.get(row.stable_key) || "";
    const aliases = prototypeManifest(row)?.routeAliases?.join(" ") || "";
    if (
      row.id !== page.id &&
      ![row.name, row.stable_key, group, aliases]
        .join(" ")
        .toLocaleLowerCase()
        .includes(filter)
    )
      continue;
    const rows = groupedPages.get(group);
    if (rows) rows.push(row);
    else groupedPages.set(group, [row]);
  }
  function pageOption(row: Entity) {
    return (
      <option key={row.id} value={row.id}>
        {row.name}
        {(pageNameCounts.get(row.name) || 0) > 1
          ? ` · ${prototypeManifest(row)?.routeAliases?.[0] || row.stable_key}`
          : ""}
      </option>
    );
  }
  return (
    <div
      className={`prototype-v3${showExplanation ? "" : " prototype-v3-wide"}`}
    >
      <div>
        <div className="prototype-v3-tools">
          {showPageSearch && (
            <label>
              筛选页面
              <input
                value={pageSearch}
                onChange={(event) => setPageSearch(event.target.value)}
              />
            </label>
          )}
          <label>
            原型页面{" "}
            <select
              aria-label="原型页面"
              value={page.id}
              onChange={(event) =>
                navigateExplanation({
                  pageId: event.target.value,
                  stateKey: "",
                  actionId: "",
                  scenarioId: "",
                })
              }
            >
              {[...groupedPages].map(([label, rows]) =>
                label ? (
                  <optgroup key={label} label={label}>
                    {rows.map(pageOption)}
                  </optgroup>
                ) : (
                  rows.map(pageOption)
                ),
              )}
            </select>
          </label>
          <label>
            原型状态{" "}
            <select
              value={state.stateKey}
              onChange={(event) => {
                if (
                  navigateExplanation({
                    pageId: page.id,
                    stateKey: event.target.value,
                    actionId: "",
                  })
                )
                  focusState.current = event.target.value;
              }}
            >
              {manifest.states.map((row) => (
                <option key={row.stateKey} value={row.stateKey}>
                  {row.name}
                </option>
              ))}
            </select>
          </label>
          {canEdit && (
            <Button
              aria-pressed={editor}
              onClick={() => {
                if (dirty) {
                  notify("请先保存或重新载入维护表单。", "error");
                  return;
                }
                setEditor(!editor);
                setShowExplanation(true);
                setDrawing(false);
                setDrawn(null);
                setPreview(undefined);
              }}
            >
              {editor ? "退出维护" : "维护页面与动作"}
            </Button>
          )}
          <button
            type="button"
            className="button"
            ref={contextToggle}
            aria-expanded={showExplanation}
            aria-controls={contextId}
            onClick={() => setShowExplanation(!showExplanation)}
          >
            {showExplanation ? "收起业务说明" : "展开业务说明"}
          </button>
          <small>
            {state.origin === "prototype_export"
              ? "原型导出 · "
              : "手工登记 · "}
            {state.viewport
              ? `${state.viewport.width} × ${state.viewport.height} · DPR ${state.viewport.devicePixelRatio}`
              : "已登记原型目录"}
          </small>
        </div>
        <h2 ref={heading} tabIndex={-1}>
          {page.name} · {state.name}
        </h2>
        <PrototypeSurface
          key={`${viewKey}:${statePreview?.image || ""}:${imageAttempt}`}
          url={statePreview?.image || (statePreview?.creating ? "" : url)}
          {...{ state, viewKey, actions, selectedAnchor }}
          preview={statePreview}
          selected={selected?.stable_key}
          onSelect={activate}
          onRetry={() => {
            setImageAttempt((attempt) => attempt + 1);
            void refresh().catch((error) => notify(error.message, "error"));
          }}
          onDraw={
            canEdit && editor && drawing
              ? (rect) => setDrawn({ surface: viewKey, rect })
              : undefined
          }
        />
        {canEdit && editor && (
          <PrototypeStateEditor
            key={stateEditorKey}
            {...{
              page,
              state,
              manifest,
              actions,
              drawing,
              setDrawing,
              selectedAnchor,
              setSelectedAnchor,
            }}
            drawn={drawn?.surface === viewKey ? drawn.rect : null}
            dirty={stateDirty}
            setDirty={setStateDirty}
            onPreview={setPreview}
            onSaved={(key) => setPendingState({ pageId: page.id, key })}
          />
        )}
      </div>
      {drawerOpen && (
        <div
          className="prototype-v3-drawer-backdrop"
          aria-hidden="true"
          onClick={() => setShowExplanation(false)}
        />
      )}
      <div
        ref={contextPanel}
        id={contextId}
        className={drawerOpen ? "prototype-v3-drawer" : undefined}
        role={drawerOpen ? "dialog" : undefined}
        aria-label={drawerOpen ? "业务说明" : undefined}
        aria-modal={drawerOpen || undefined}
        hidden={!showExplanation}
        onKeyDown={(event) => {
          if (!drawerOpen && event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            setShowExplanation(false);
            contextToggle.current?.focus({ preventScroll: true });
          }
        }}
      >
        {drawerOpen && (
          <button
            type="button"
            className="button prototype-v3-drawer-close"
            ref={drawerClose}
            onClick={() => setShowExplanation(false)}
          >
            关闭业务说明
          </button>
        )}
        <Panel className="prototype-v3-context" title="业务说明">
          <h3>{selected?.name || page.name}</h3>
          {selected && target && !target.surface && (
            <Notice>该动作的目标状态画面待补充，继续显示当前状态。</Notice>
          )}
          {hasOutdated && (
            <Button
              aria-pressed={showOutdated}
              onClick={() => setShowOutdated(!showOutdated)}
            >
              {showOutdated ? "隐藏待核对关联" : "查看待核对关联"}
            </Button>
          )}
          {!selected ? (
            <p>选择画布中的控件或下方的动作查看业务说明。</p>
          ) : context?.error ? (
            <Notice tone="red">
              {context.error}{" "}
              <Button
                onClick={() => setContextAttempt((attempt) => attempt + 1)}
              >
                重试业务说明
              </Button>
            </Notice>
          ) : !context?.data ? (
            <p role="status">正在加载业务说明…</p>
          ) : !points.length && !documents.length ? (
            <>
              <p>当前可见范围内暂无关联说明。</p>
              {canEdit && !editor && (
                <Button onClick={() => setEditor(true)}>添加业务说明</Button>
              )}
            </>
          ) : (
            <>
              {points.map((point) => (
                <section key={point.binding_id}>
                  <h4>{point.title}</h4>
                  {point.validity !== "valid" && (
                    <Notice>这条关联需要重新定位。</Notice>
                  )}
                  <p>{point.summary}</p>
                  {point.applicability && (
                    <p>适用条件　{point.applicability}</p>
                  )}
                  {point.outcome && <p>结果与影响　{point.outcome}</p>}
                  {point.exceptions && <p>例外　{point.exceptions}</p>}
                  {!point.sources.length && <small>维护说明 · 暂无原文</small>}
                  {point.sources
                    .filter(
                      (source) => showOutdated || source.validity === "valid",
                    )
                    .map((source) => (
                      <Button
                        key={source.section_revision_id}
                        onClick={() =>
                          showSource(
                            source.section_id,
                            source.section_revision_id,
                          )
                        }
                      >
                        {source.validity === "valid"
                          ? "查看原文"
                          : "出处待核对"}{" "}
                        · {source.title}
                      </Button>
                    ))}
                </section>
              ))}
              {documents.map((source) => (
                <section key={source.section_revision_id}>
                  <h4>{source.title}</h4>
                  <p>{source.excerpt}</p>
                  <Button
                    onClick={() =>
                      showSource(source.section_id, source.section_revision_id)
                    }
                  >
                    {source.validity === "valid" ? "查看原文" : "出处待核对"} ·{" "}
                    {source.title}
                  </Button>
                </section>
              ))}
            </>
          )}
          {canEdit && editor && selected && trigger && (
            <PrototypeBindingEditor
              key={bindingEditorKey}
              page={page}
              action={selected}
              state={trigger}
              manifest={manifest}
              dirty={bindingDirty}
              setDirty={setBindingDirty}
            />
          )}
        </Panel>
      </div>
    </div>
  );
}
