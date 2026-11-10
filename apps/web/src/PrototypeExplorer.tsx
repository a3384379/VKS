import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import { api } from "./api";
import {
  PrototypeBindingEditor,
  PrototypeStateEditor,
} from "./PrototypeMaintenance";
import { Button, Notice, Panel } from "./components";
import { usePermission, useWorkspace } from "./context";
import {
  prototypeApi,
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
  onSelect,
  onDraw,
}: {
  state: PrototypeState;
  url: string;
  viewKey: string;
  actions: Map<string, Entity>;
  selected?: string;
  onSelect: (anchor: PrototypeAnchor) => void;
  onDraw?: (rect: PrototypeAnchor["rect"]) => void;
}) {
  const [view, setView] = useState(() => savedView(viewKey));
  const [image, setImage] = useState<"loading" | "loaded" | "failed">(
    "loading",
  );
  const scroll = useRef<HTMLDivElement>(null);
  const start = useRef<[number, number] | null>(null);
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
  const viewport = state.viewport;
  const anchors = state.anchors.filter((anchor) =>
    actions.has(anchor.actionKey),
  );
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
          原型画面无法读取，请刷新并检查当前版本的资料与权限。
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
            {image === "loaded" && onDraw && (
              <div
                className="prototype-v3-draw"
                aria-label="拖拽绘制热区"
                onPointerDown={(event) => {
                  event.currentTarget.setPointerCapture(event.pointerId);
                  start.current = position(event);
                }}
                onPointerCancel={() => {
                  start.current = null;
                }}
                onPointerUp={(event) => {
                  if (!start.current) return;
                  const [x, y] = start.current;
                  start.current = null;
                  const [left, top] = position(event);
                  if (Math.abs(left - x) > 0.003 && Math.abs(top - y) > 0.003)
                    onDraw([
                      Math.min(left, x),
                      Math.min(top, y),
                      Math.abs(left - x),
                      Math.abs(top - y),
                    ]);
                }}
              />
            )}
            {image === "loaded" &&
              !onDraw &&
              view.hotspots &&
              anchors.map((anchor) => {
                const [x, y, width, height] = anchor.rect.map(
                  (value) => value * 100,
                );
                const action = actions.get(anchor.actionKey)!;
                return (
                  <button
                    type="button"
                    key={anchor.anchorKey}
                    aria-label={action.name}
                    aria-pressed={selected === anchor.actionKey}
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
              aria-pressed={selected === anchor.actionKey}
              onClick={() => onSelect(anchor)}
            >
              {actions.get(anchor.actionKey)?.name}
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
  const { w, session, explanation, navigateExplanation, navigate } =
    useWorkspace();
  const canEdit =
    usePermission("knowledge:edit") &&
    !w.selected_release &&
    w.selected_analysis_set?.state === "draft";
  const [editor, setEditor] = useState(false);
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
  const contextKey = `${contextUrl}:${w.selected_analysis_set?.selection_version}:${selected?.entity_revision_id}`;
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
        if (!request.signal.aborted)
          setResponse({ key: contextKey, error: error.message });
      },
    );
    return () => request.abort();
  }, [contextKey, contextUrl]);
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
  useLayoutEffect(() => {
    setDrawn(null);
    setDrawing(false);
  }, [viewKey]);
  useLayoutEffect(() => {
    if (focusState.current === state.stateKey) {
      focusState.current = null;
      heading.current?.focus({ preventScroll: true });
    }
  }, [state.stateKey]);
  function activate(row: PrototypeAnchor) {
    const action = actions.get(row.actionKey);
    if (!action) return;
    if (
      manifest.states.some(
        (candidate) =>
          candidate.stateKey === row.targetStateKey &&
          candidate.stateKey !== state.stateKey &&
          candidate.surface,
      )
    )
      focusState.current = row.targetStateKey!;
    navigateExplanation({
      pageId: page.id,
      stateKey: state.stateKey,
      triggerStateKey: state.stateKey,
      actionId: action.id,
      followTarget: true,
    });
  }
  return (
    <div className="prototype-v3">
      <div>
        <div className="prototype-v3-tools">
          {w.pages.length > 10 && (
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
              {w.pages
                .filter(
                  (row) =>
                    row.id === page.id ||
                    `${row.name} ${row.stable_key} ${JSON.stringify(row.metadata.prototype_manifest)}`
                      .toLowerCase()
                      .includes(pageSearch.toLowerCase()),
                )
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
            </select>
          </label>
          <label>
            原型状态{" "}
            <select
              value={state.stateKey}
              onChange={(event) => {
                focusState.current = event.target.value;
                navigateExplanation({
                  pageId: page.id,
                  stateKey: event.target.value,
                  actionId: "",
                });
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
                setEditor(!editor);
                setDrawing(false);
                setDrawn(null);
              }}
            >
              {editor ? "退出维护" : "维护页面与动作"}
            </Button>
          )}
          <small>
            {state.viewport
              ? `${state.viewport.width} × ${state.viewport.height} · DPR ${state.viewport.devicePixelRatio}`
              : "已登记原型目录"}
          </small>
        </div>
        <h2 ref={heading} tabIndex={-1}>
          {page.name} · {state.name}
        </h2>
        <PrototypeSurface
          key={viewKey}
          {...{ state, url, viewKey, actions }}
          selected={selected?.stable_key}
          onSelect={activate}
          onDraw={
            editor && drawing
              ? (rect) => setDrawn({ surface: viewKey, rect })
              : undefined
          }
        />
        {canEdit && editor && (
          <PrototypeStateEditor
            key={"editor:" + viewKey}
            {...{ page, state, manifest, actions, drawing, setDrawing }}
            drawn={drawn?.surface === viewKey ? drawn.rect : null}
          />
        )}
      </div>
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
          <Notice tone="red">{context.error}</Notice>
        ) : !context?.data ? (
          <p role="status">正在加载业务说明…</p>
        ) : !points.length && !documents.length ? (
          <p>当前可见范围内暂无关联说明。</p>
        ) : (
          <>
            {points.map((point) => (
              <section key={point.binding_id}>
                <h4>{point.title}</h4>
                {point.validity !== "valid" && (
                  <Notice>这条关联需要重新定位。</Notice>
                )}
                <p>{point.summary}</p>
                {point.applicability && <p>适用条件　{point.applicability}</p>}
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
                      {source.validity === "valid" ? "查看原文" : "出处待核对"}{" "}
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
            key={selected.entity_revision_id + trigger.stateKey}
            page={page}
            action={selected}
            state={trigger}
            points={context?.data?.points || []}
          />
        )}
      </Panel>
    </div>
  );
}
