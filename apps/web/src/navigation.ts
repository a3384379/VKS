import type { Entity, Workspace } from "./types";

export type ExplanationNavigation = {
  pageId: string;
  scenarioId: string;
  stateKey: string;
  actionId: string;
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
  let scene = w.scenarios.find((s) => s.id === next.scenarioId);
  if (intent.scenarioId && !scene) throw new Error("当前基线中没有此场景");
  const initial = !scene;
  if (!scene) scene = w.scenarios[0];
  next.scenarioId = scene?.id || "";
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
