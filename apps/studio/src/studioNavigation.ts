import { useCallback, useEffect, useRef, useState } from "react";

const fields = [
  "project",
  "analysis",
  "release",
  "view",
  "doc",
  "section",
  "binding",
  "package",
  "book",
  "reader",
  "review",
  "page_ref",
] as const;
const views = ["prototype", "documents", "projects", "materials", "links", "ai"];
const readers = ["auto", "standard", "source", "original", "handbook"];
const reference = /^[a-zA-Z0-9_-]{1,128}$/;

export type Route = Record<(typeof fields)[number], string>;
export type NavigationStatus = {
  phase: "loading" | "runtime" | "locating" | "located" | "degraded" | "failed";
  message: string;
};

export function parseStudioRoute(hash: string): { route: Route; invalid: boolean } {
  const encoded = hash.replace(/^#/, "");
  const params = new URLSearchParams(encoded.length <= 4096 ? encoded : "");
  const route = Object.fromEntries(fields.map((key) => [key, params.get(key) || ""])) as Route;
  const hasTarget = !!(route.doc || route.binding || route.package || route.book || route.review);
  const invalid =
    encoded.length > 4096 ||
    [...params].some(([key, value]) =>
      !fields.includes(key as keyof Route) ||
      params.getAll(key).length !== 1 ||
      (value !== "" && !reference.test(value)),
    ) ||
    (!!route.view && !views.includes(route.view)) ||
    (!!route.reader && !readers.includes(route.reader)) ||
    !!(route.analysis && route.release) ||
    !!(route.binding && route.page_ref) ||
    !!(route.section && !route.doc) ||
    !!(route.page_ref && (!route.package || !/^[a-f0-9]{64}$/.test(route.page_ref))) ||
    (!route.project && !!(route.analysis || route.release || hasTarget)) ||
    (hasTarget && !(route.analysis || route.release));
  return { route, invalid };
}

export const getRoute = () => parseStudioRoute(location.hash).route;

export const sameWorkspace = (a: Route, b: Route) =>
  a.project === b.project && a.analysis === b.analysis && a.release === b.release;

function routeHash(route: Partial<Route>) {
  return "#" + new URLSearchParams(
    fields.flatMap((key) => route[key] ? [[key, route[key]]] : []),
  ).toString();
}

export function exactLocationUrl(route: Route, target: Partial<Route>) {
  const { project, release } = route;
  const url = new URL(location.pathname, location.origin);
  url.hash = routeHash({ project, release, ...target });
  return url.href;
}

function readLocation(generation: number) {
  const hash = location.hash;
  const parsed = parseStudioRoute(hash);
  const storedBinding = history.state?.vksPageBinding;
  const directory = history.state?.vksVersionDirectory === true;
  const external = history.state?.vksInternal !== true;
  const pageBinding =
    !parsed.route.binding && !parsed.route.page_ref &&
    typeof storedBinding === "string" && reference.test(storedBinding)
      ? storedBinding
      : "";
  return { ...parsed, hash, pageBinding, directory, external, generation };
}

export function useStudioNavigation() {
  const [attempt, setAttempt] = useState(() => readLocation(0));
  const current = useRef(attempt);
  const [result, setResult] = useState<(NavigationStatus & { generation: number }) | null>(null);
  const select = useCallback((force = false) => {
    const next = readLocation(current.current.generation + 1);
    if (!force && next.hash === current.current.hash &&
        next.pageBinding === current.current.pageBinding &&
        next.directory === current.current.directory &&
        next.external === current.current.external) return current.current.generation;
    current.current = next;
    setAttempt(next);
    return next.generation;
  }, []);
  useEffect(() => {
    const update = () => select();
    window.addEventListener("popstate", update);
    window.addEventListener("hashchange", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("hashchange", update);
    };
  }, [select]);
  const navigate = useCallback((patch: Partial<Route>, replace = false, state: object = {}) => {
    const next = { ...getRoute(), ...patch };
    history[replace ? "replaceState" : "pushState"](
      { ...state, vksInternal: true }, "", routeHash(next),
    );
    select();
  }, [select]);
  const isCurrent = useCallback((generation: number) =>
    current.current.generation === generation && current.current.hash === location.hash,
  []);
  const report = useCallback((generation: number, status: NavigationStatus) => {
    if (isCurrent(generation)) setResult((previous) =>
      previous?.generation === generation && previous.phase === status.phase &&
      previous.message === status.message ? previous : { ...status, generation },
    );
  }, [isCurrent]);
  const retry = useCallback(() => select(true), [select]);
  return {
    ...attempt,
    navigate,
    isCurrent,
    report,
    status: result?.generation === attempt.generation ? result : undefined,
    retry,
  };
}
