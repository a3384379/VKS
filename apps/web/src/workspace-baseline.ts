import type { Workspace } from "./types";

/** URL aliases do not change the identity of the selected knowledge baseline. */
export function workspaceBaseline(
  workspace: Pick<Workspace, "selected_analysis_set" | "selected_release">,
) {
  if (workspace.selected_release)
    return `release:${workspace.selected_release.id}`;
  return workspace.selected_analysis_set
    ? `draft:${workspace.selected_analysis_set.id}`
    : "";
}
