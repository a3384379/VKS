import type { Entity } from "./types";
export type JourneyNode = {
  key: string;
  kind: "page" | "action" | "decision" | "branch" | "end" | "reference";
  title: string;
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
  entry: string;
  nodes: JourneyNode[];
  edges: { key: string; source: string; target: string; label: string }[];
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
