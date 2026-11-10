import type { Entity, Workspace } from "./types";
import { prototypeManifest } from "./prototype";

export type ExplanationNavigation = {
  pageId: string;
  scenarioId: string;
  stateKey: string;
  actionId: string;
  triggerStateKey?: string;
};
export type NavigationIntent = Partial<ExplanationNavigation> & {
  followTarget?: boolean;
};
export const emptyNavigation: ExplanationNavigation = {
  pageId: "",
  scenarioId: "",
  stateKey: "",
  actionId: "",
};
type State = { stateKey: string; pageKey?: string; actionKey?: string };
type Graph = { states: State[] };
function pageKey(action: Entity | undefined) {
  return String(action?.metadata.page_key || action?.metadata.pageKey || "");
}
function graph(entity: Entity | undefined): Graph | undefined {
  return (entity?.metadata.dsl as { explanation_graph?: Graph } | undefined)
    ?.explanation_graph;
}

/** Select already declared knowledge only. No DSL step or business request is executed. */
export function resolveExplanation(
  w: Workspace,
  current: ExplanationNavigation,
  intent: NavigationIntent,
): ExplanationNavigation {
  const next = { ...current, ...intent };
  const actions = w.actions.filter((a) => a.metadata.status !== "removed");
  let action = actions.find((a) => a.id === next.actionId);
  if (intent.actionId && !action) throw new Error("当前基线中没有此动作");
  if (intent.pageId && !w.pages.some((p) => p.id === intent.pageId))
    throw new Error("当前基线中没有此页面");
  const requestedPage = w.pages.find((p) => p.id === intent.pageId);
  const actionPage =
    action && w.pages.find((p) => p.stable_key === pageKey(action));
  const rasterPage =
    requestedPage ||
    (intent.actionId
      ? actionPage
      : w.pages.find((p) => p.id === current.pageId)) ||
    actionPage ||
    (!current.pageId &&
      w.pages.find((p) =>
        prototypeManifest(p)?.states.some((state) => state.surface),
      ));
  const raster = rasterPage ? prototypeManifest(rasterPage) : undefined;
  if (rasterPage && raster && !intent.scenarioId) {
    if (intent.actionId && actionPage?.id !== rasterPage.id)
      throw new Error("动作不属于当前原型页面");
    const previousState =
      current.pageId === rasterPage.id ? current.stateKey : "";
    const state = raster.states.find(
      (row) =>
        row.stateKey ===
        (intent.stateKey || previousState || raster.defaultStateKey),
    );
    if (!state) throw new Error("当前原型页面没有此状态");
    if (intent.actionId === "" || actionPage?.id !== rasterPage.id)
      action = undefined;
    const location = {
      pageId: rasterPage.id,
      scenarioId: "",
      stateKey: state.stateKey,
      actionId: "",
    };
    if (!action) return location;

    const matching = raster.states.filter((row) =>
      row.anchors.some((anchor) => anchor.actionKey === action!.stable_key),
    );
    const triggerKey =
      intent.triggerStateKey ||
      (current.actionId === action.id &&
        !intent.followTarget &&
        current.triggerStateKey);
    const trigger = triggerKey
      ? matching.find((row) => row.stateKey === triggerKey)
      : matching.find((row) => row.stateKey === state.stateKey) ||
        (!intent.stateKey && matching.length === 1 ? matching[0] : undefined);
    if (!trigger) throw new Error("当前原型没有此动作的准确触发状态");
    const anchor = trigger.anchors.find(
      (row) => row.actionKey === action!.stable_key,
    )!;
    const target = raster.states.find(
      (row) => row.stateKey === anchor.targetStateKey && row.surface,
    );
    if (
      !intent.followTarget &&
      intent.stateKey &&
      state.stateKey !== trigger.stateKey &&
      state.stateKey !== target?.stateKey
    )
      throw new Error("动作与当前原型状态不匹配");
    return {
      ...location,
      stateKey: intent.followTarget
        ? (target || trigger).stateKey
        : intent.stateKey
          ? state.stateKey
          : trigger.stateKey,
      actionId: action.id,
      triggerStateKey: trigger.stateKey,
    };
  }
  let scene = w.scenarios.find((s) => s.id === next.scenarioId);
  if (intent.scenarioId && !scene) throw new Error("当前基线中没有此场景");
  const initial = !scene;
  if (!scene) scene = w.scenarios[0];
  next.scenarioId = scene?.id || "";
  if (prototypeManifest(w.pages.find((p) => p.id === current.pageId))) {
    next.stateKey = intent.stateKey || "";
    next.scenarioId = intent.scenarioId || "";
  }
  const explicitState = intent.stateKey !== undefined && !!intent.stateKey;
  let targetFollowed = false;
  if (
    intent.scenarioId &&
    intent.scenarioId !== current.scenarioId &&
    intent.stateKey === undefined
  ) {
    next.stateKey = graph(scene)?.states[0]?.stateKey || "";
  }
  if (intent.actionId && action) {
    next.pageId =
      w.pages.find((p) => p.stable_key === pageKey(action))?.id || "";
    const target =
      intent.followTarget && String(action.metadata.target_state_key || "");
    if (target) {
      const matches = w.scenarios.filter((s) =>
        graph(s)?.states.some((state) => state.stateKey === target),
      );
      if (initial && matches.length > 1)
        throw new Error("目标状态存在多义，请先选择场景");
      if (!graph(scene)?.states.some((state) => state.stateKey === target)) {
        if (matches.length !== 1)
          throw new Error("目标状态不可用或存在多义，请先选择场景");
        scene = matches[0];
      }
      next.scenarioId = scene!.id;
      next.stateKey = target;
      targetFollowed = true;
    } else if (!explicitState) {
      const page = w.pages.find((p) => p.id === next.pageId);
      const matches =
        graph(scene)?.states.filter(
          (state) =>
            state.pageKey === page?.stable_key ||
            state.stateKey ===
              (page?.metadata.stateKey || page?.metadata.state_key),
        ) || [];
      next.stateKey = matches.length === 1 ? matches[0].stateKey : "";
    }
  }
  if (!next.pageId && !action) {
    action = actions[0];
    next.pageId =
      w.pages.find((p) => p.stable_key === pageKey(action))?.id ||
      w.pages[0]?.id ||
      "";
  }
  if (intent.pageId && !explicitState) {
    next.pageId = intent.pageId;
    const page = w.pages.find((p) => p.id === next.pageId);
    const matches =
      graph(scene)?.states.filter(
        (state) =>
          state.pageKey === page?.stable_key ||
          state.stateKey ===
            (page?.metadata.stateKey || page?.metadata.state_key),
      ) || [];
    next.stateKey = matches.length === 1 ? matches[0].stateKey : "";
  } else if (initial && !next.stateKey) {
    const page = w.pages.find((p) => p.id === next.pageId);
    next.stateKey =
      graph(scene)?.states.find(
        (state) =>
          state.pageKey === page?.stable_key ||
          state.stateKey ===
            (page?.metadata.stateKey || page?.metadata.state_key),
      )?.stateKey || "";
  }
  const state = graph(scene)?.states.find((s) => s.stateKey === next.stateKey);
  if (next.stateKey && !state) throw new Error("当前场景中没有此状态");
  if (state) {
    const stateAction = state.actionKey
      ? actions.find((a) => a.stable_key === state.actionKey)
      : undefined;
    if (state.actionKey && !stateAction)
      throw new Error("状态动作不属于当前基线");
    const matches = w.pages.filter(
      (p) =>
        p.stable_key === (state.pageKey || pageKey(stateAction)) ||
        (!state.pageKey &&
          !stateAction &&
          (p.metadata.stateKey || p.metadata.state_key) === state.stateKey),
    );
    if (matches.length > 1) throw new Error("状态对应多个页面，请明确页面绑定");
    if ((state.pageKey || state.actionKey) && !matches.length)
      throw new Error("状态页面不属于当前基线");
    if (matches.length) next.pageId = matches[0].id;
    if (
      stateAction &&
      (explicitState || targetFollowed || !intent.actionId || !!intent.pageId)
    )
      action = stateAction;
  }
  const page = w.pages.find((p) => p.id === next.pageId);
  if (!action || pageKey(action) !== page?.stable_key)
    action = actions.find((a) => pageKey(a) === page?.stable_key);
  next.actionId = action?.id || "";
  return {
    pageId: next.pageId,
    scenarioId: next.scenarioId,
    stateKey: next.stateKey,
    actionId: next.actionId,
  };
}
