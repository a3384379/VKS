import type { JourneyDSL } from "./journeys";

export type JourneyNavigation = {
  journeyId: string;
  revisionId: string;
  stepKey: string;
  choices: Record<string, string>;
};
export const emptyJourneyNavigation: JourneyNavigation = {
  journeyId: "",
  revisionId: "",
  stepKey: "",
  choices: {},
};
const keyPattern = /^[a-zA-Z0-9_.-]{1,96}$/;

export function readJourneyNavigation(
  query: URLSearchParams,
): JourneyNavigation {
  let choices: Record<string, string> = {};
  const encoded = query.get("journey_choices") || "";
  if (encoded.length <= 25000) {
    try {
      const value: unknown = JSON.parse(encoded);
      if (value && typeof value === "object" && !Array.isArray(value))
        choices = Object.fromEntries(
          Object.entries(value)
            .slice(0, 120)
            .filter(
              ([key, edge]) =>
                keyPattern.test(key) &&
                typeof edge === "string" &&
                keyPattern.test(edge),
            ),
        );
    } catch {
      /* Invalid external route data is ignored. */
    }
  }
  return {
    journeyId:
      query.get("journey") ||
      (query.get("view") === "journeys" ? query.get("entity") || "" : ""),
    revisionId: query.get("journey_revision") || "",
    stepKey: query.get("journey_step") || "",
    choices,
  };
}

export function writeJourneyNavigation(
  query: URLSearchParams,
  value: JourneyNavigation,
) {
  if (!value.journeyId) return;
  query.set("journey", value.journeyId);
  if (value.revisionId) query.set("journey_revision", value.revisionId);
  if (value.stepKey) query.set("journey_step", value.stepKey);
  if (Object.keys(value.choices).length)
    query.set(
      "journey_choices",
      JSON.stringify(
        Object.fromEntries(
          Object.entries(value.choices).sort(([a], [b]) => a.localeCompare(b)),
        ),
      ),
    );
}

/** Retain only choices reachable in this exact DSL; never guess another revision's path. */
export function resolveJourneyReading(
  dsl: JourneyDSL,
  revisionId: string,
  value: JourneyNavigation,
) {
  const choices = new Map<string, string>();
  const seen = new Set<string>();
  let key: string | undefined = dsl.entry;
  while (key && !seen.has(key) && seen.size < 120) {
    seen.add(key);
    const outgoing = dsl.edges.filter((edge) => edge.source === key);
    if (outgoing.length > 1) {
      const choice =
        (!value.revisionId || value.revisionId === revisionId) &&
        outgoing.find((edge) => edge.key === value.choices[key!]);
      if (!choice) break;
      choices.set(key, choice.key);
      key = choice.target;
    } else key = outgoing[0]?.target;
  }
  const ordered = ["business_process", "operation_sequence"].includes(
    dsl.semantics?.kind || "",
  );
  const allowed = ordered ? seen : new Set(dsl.nodes.map((node) => node.key));
  const stepKey =
    (!value.revisionId || value.revisionId === revisionId) &&
    allowed.has(value.stepKey)
      ? value.stepKey
      : dsl.entry;
  return { revisionId, stepKey, choices: Object.fromEntries(choices) };
}
