import type { View } from "./types";
export const primaryNavigation: { id: View; title: string }[] = [
  { id: "overview", title: "项目空间" },
  { id: "workbench", title: "业务探索" },
  { id: "imports", title: "知识构建" },
  { id: "versions", title: "变更与版本" },
  { id: "settings", title: "项目设置" },
];
export function navigationGroup(view: View): View {
  if (["journeys", "graph"].includes(view)) return "workbench";
  if (["documents", "agent"].includes(view)) return "imports";
  if (view === "relations") return "versions";
  return view;
}
export function routeParameters(value: string) {
  const alias: Record<string, View> = {
    explorer: "workbench",
    agent: "agent",
    changes: "versions",
    reviews: "relations",
    overview: "overview",
    build: "imports",
  };
  const query = new URLSearchParams(value.includes("=") ? value : "");
  if (!value.includes("=")) query.set("view", alias[value] || "overview");
  else if (alias[query.get("view") || ""])
    query.set("view", alias[query.get("view")!]);
  return query;
}
