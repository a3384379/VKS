import type { Entity, Workspace } from "./types";

/** Module members define the selectable actions; their pages are passive context. */
export function moduleWorkspace(
  w: Workspace,
  members: Entity[] | null,
): Workspace {
  const ids = new Set((members || []).map((entity) => entity.entity_id));
  const actions = w.actions.filter((action) => ids.has(action.entity_id));
  const pageKeys = new Set(
    actions.map((action) =>
      String(action.metadata.page_key || action.metadata.pageKey || ""),
    ),
  );
  return {
    ...w,
    actions,
    pages: w.pages.filter(
      (page) => ids.has(page.entity_id) || pageKeys.has(page.stable_key),
    ),
    scenarios: w.scenarios.filter((scene) => ids.has(scene.entity_id)),
  };
}
