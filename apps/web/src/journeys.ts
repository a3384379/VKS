import type { Entity } from "./types";
export type JourneyNode = {
  key: string;
  kind:
    | "page"
    | "action"
    | "step"
    | "decision"
    | "branch"
    | "end"
    | "reference";
  title: string;
  description?: string;
  x: number;
  y: number;
  binding?: { entity_id: string; revision_id: string };
  state_ref?: {
    scenario_id: string;
    scenario_revision_id: string;
    state_key: string;
  };
  capture_ref?: {
    scenario_id: string;
    scenario_revision_id: string;
    capture_id: string;
    snapshot_index: number;
  };
};
export type JourneyDSL = {
  schema_version: "2.0";
  key: string;
  title: string;
  semantics?: JourneySemantics;
  entry: string;
  nodes: JourneyNode[];
  edges: { key: string; source: string; target: string; label: string }[];
};
export type JourneySemantics = {
  kind:
    | "business_process"
    | "operation_sequence"
    | "evidence_navigation"
    | "observation_comparison"
    | "unclassified";
  basis: "declared" | "observed" | "inferred" | "unknown";
  purpose?: string;
  outcome?: string;
  preconditions?: string[];
  unknowns?: string[];
  source_refs?: { entity_id: string; revision_id: string }[];
};
export type JourneyRevision = {
  journey_id: string;
  revision_id: string;
  content_sha256: string;
  created_at: string;
  dsl: JourneyDSL;
};
export type JourneyHistory = {
  past: JourneyDSL[];
  present: JourneyDSL;
  future: JourneyDSL[];
};
export function prepareJourneyDraft(dsl: JourneyDSL): JourneyDSL {
  function text(value: string | undefined) {
    const result = value?.trim();
    if (result && result.length > 2000)
      throw new Error("说明文字不能超过 2000 字");
    return result || undefined;
  }
  function lines(values: string[] | undefined, limit: number) {
    if (!values) return undefined;
    const result = values.map(text).filter((value): value is string => !!value);
    if (result.length > limit) throw new Error(`最多填写 ${limit} 项`);
    return result;
  }
  const next: JourneyDSL = {
    ...dsl,
    nodes: dsl.nodes.map((node) => {
      if (node.kind === "step" && !text(node.description))
        throw new Error("业务声明步骤请填写步骤说明");
      if (node.description === undefined) return node;
      const { description, ...rest } = node;
      const value = text(description);
      return value ? { ...rest, description: value } : rest;
    }),
  };
  if (dsl.semantics) {
    const { purpose, outcome, preconditions, unknowns, ...rest } =
      dsl.semantics;
    const goal = text(purpose),
      result = text(outcome);
    if (rest.kind === "business_process" && (!goal || !result))
      throw new Error("业务场景请填写业务目标和预期结果");
    if (
      rest.kind === "business_process" &&
      next.nodes.some((node) => node.kind === "step") &&
      !rest.source_refs?.length
    )
      throw new Error("业务声明步骤请至少选择一项场景依据资料");
    next.semantics = {
      ...rest,
      ...(goal ? { purpose: goal } : {}),
      ...(result ? { outcome: result } : {}),
      ...(preconditions ? { preconditions: lines(preconditions, 30) } : {}),
      ...(unknowns ? { unknowns: lines(unknowns, 50) } : {}),
    };
  }
  return next;
}
export function updateJourney(
  history: JourneyHistory,
  next: JourneyDSL,
): JourneyHistory {
  if (JSON.stringify(next) === JSON.stringify(history.present)) return history;
  return {
    past: [...history.past.slice(-49), history.present],
    present: next,
    future: [],
  };
}
export function travelJourney(
  history: JourneyHistory,
  direction: "undo" | "redo",
): JourneyHistory {
  if (direction === "undo" && history.past.length)
    return {
      past: history.past.slice(0, -1),
      present: history.past.at(-1)!,
      future: [history.present, ...history.future],
    };
  if (direction === "redo" && history.future.length)
    return {
      past: [...history.past, history.present],
      present: history.future[0],
      future: history.future.slice(1),
    };
  return history;
}
export function newJourney(action?: Entity): JourneyDSL {
  return {
    schema_version: "2.0",
    key: `journey-${crypto.randomUUID()}`,
    title: "新业务流程",
    entry: action ? "step" : "done",
    nodes: [
      ...(action
        ? [
            {
              key: "step",
              kind: "action" as const,
              title: action.name,
              x: 40,
              y: 60,
              binding: {
                entity_id: action.entity_id,
                revision_id: action.entity_revision_id,
              },
            },
          ]
        : []),
      { key: "done", kind: "end", title: "结束", x: 340, y: 60 },
    ],
    edges: action
      ? [{ key: "next", source: "step", target: "done", label: "" }]
      : [],
  };
}
