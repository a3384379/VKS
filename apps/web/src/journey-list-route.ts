import {
  readJourneyNavigation,
  type JourneyNavigation,
} from "./journey-navigation";
import { routeParameters } from "./workspace-navigation";

export type JourneyListRoute = {
  projectId: string;
  baseline: string;
  selectedEntity: string;
  journey: JourneyNavigation;
};

export function journeyListRouteKey(route: JourneyListRoute) {
  return JSON.stringify([
    route.projectId,
    route.baseline,
    route.selectedEntity,
    route.journey.journeyId,
    route.journey.revisionId,
    route.journey.stepKey,
    Object.entries(route.journey.choices).sort(([a], [b]) =>
      a.localeCompare(b),
    ),
  ]);
}

/** A native URL may advance before React commits it. Never canonicalize across that gap. */
export function journeyListRouteMatches(hash: string, route: JourneyListRoute) {
  const query = routeParameters(hash.replace(/^#/, ""));
  return (
    (query.get("view") || query.get("page")) === "journeys" &&
    journeyListRouteKey({
      projectId: query.get("project") || "",
      baseline: query.get("baseline") || "",
      selectedEntity: query.get("entity") || "",
      journey: readJourneyNavigation(query),
    }) === journeyListRouteKey(route)
  );
}
