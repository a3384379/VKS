import { useLayoutEffect, useRef, useState } from "react";
import { Button, Notice, Panel } from "./components";
import { useWorkspace } from "./context";
import type {
  PrototypeAnchor,
  PrototypeManifest,
  PrototypeState,
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
}: {
  state: PrototypeState;
  url: string;
  viewKey: string;
  actions: Map<string, Entity>;
  selected?: string;
  onSelect: (anchor: PrototypeAnchor) => void;
}) {
  const [view, setView] = useState(() => savedView(viewKey));
  const [image, setImage] = useState<"loading" | "loaded" | "failed">(
    "loading",
  );
  const scroll = useRef<HTMLDivElement>(null);
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
      {!url ? (
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
              width: state.viewport.width,
              maxWidth: view.fit ? "100%" : undefined,
              aspectRatio: state.viewport.width + " / " + state.viewport.height,
            }}
          >
            <img
              src={url}
              alt={state.name + "原型画面"}
              onLoad={() => setImage("loaded")}
              onError={() => setImage("failed")}
            />
            {image === "loaded" &&
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
  const selectedRevision = selected?.entity_revision_id;
  const sections = new Map(
    w.entities
      .filter((row) => row.type === "section")
      .map((row) => [row.entity_revision_id, row]),
  );
  const explanations = selectedRevision
    ? w.relations.flatMap((relation) => {
        if (
          relation.from_revision_id !== selectedRevision ||
          (relation.relation_type || relation.type) !== "documents" ||
          relation.excluded ||
          relation.validity_status !== "valid" ||
          !relation.evidence_ids.length
        )
          return [];
        const section = sections.get(relation.to_revision_id);
        return section ? [{ section, relation }] : [];
      })
    : [];
  const releaseId = w.selected_release?.id;
  const analysisId = w.selected_analysis_set?.id;
  const query = new URLSearchParams({
    page_revision_id: page.entity_revision_id,
  });
  if (releaseId) query.set("release_id", releaseId);
  else if (analysisId) query.set("analysis_set_id", analysisId);
  const url =
    state.surface && (releaseId || analysisId)
      ? "/api/v3/projects/" +
        encodeURIComponent(w.project.id) +
        "/prototype/pages/" +
        encodeURIComponent(page.stable_key) +
        "/states/" +
        encodeURIComponent(state.stateKey) +
        "/image?" +
        query
      : "";
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
              {w.pages.map((row) => (
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
          <small>
            原型导出 · {state.viewport.width} × {state.viewport.height} · DPR{" "}
            {state.viewport.devicePixelRatio}
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
        />
      </div>
      <Panel className="prototype-v3-context" title="业务说明">
        <h3>{selected?.name || page.name}</h3>
        {selected && target && !target.surface && (
          <Notice>该动作的目标状态画面待补充，继续显示当前状态。</Notice>
        )}
        {!selected ? (
          <p>选择画布中的控件或下方的动作查看业务说明。</p>
        ) : !explanations.length ? (
          <p>当前可见范围内暂无关联说明。</p>
        ) : (
          explanations.map(({ section, relation }) => (
            <section key={relation.id}>
              <h4>{section.name}</h4>
              <p>{section.content?.slice(0, 400)}</p>
              <Button onClick={() => navigate("documents", section)}>
                查看原文 · {section.name}
              </Button>
            </section>
          ))
        )}
      </Panel>
    </div>
  );
}
