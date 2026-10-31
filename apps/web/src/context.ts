import {
  createContext,
  createElement,
  Fragment,
  useContext,
  type ReactNode,
} from "react";
import type { Entity, Session, View, Workspace } from "./types";
import { hasProjectPermission } from "./permissions";
import type { ExplanationNavigation, NavigationIntent } from "./navigation";
import type { JourneyNavigation } from "./journey-navigation";
export interface AppState {
  w: Workspace;
  session: Session;
  moduleId?: string;
  moduleEntities?: Entity[] | null;
  navigationEpoch?: number;
  selected: string;
  setSelected: (id: string) => void;
  explanation: ExplanationNavigation;
  navigateExplanation: (intent: NavigationIntent) => void;
  journey: JourneyNavigation;
  navigateJourney: (intent: JourneyNavigation, automatic?: boolean) => void;
  navigate: (view: View, entity?: Entity) => void;
  navigateReadEntity?: (
    workspace: Workspace,
    entity: Entity,
    baseline: string,
    stepKey?: string,
  ) => void;
  refresh: () => Promise<void>;
  notify: (message: string, tone?: string) => void;
  run: <T>(
    operation: () => Promise<T>,
    success?: string,
  ) => Promise<T | undefined>;
  busy: boolean;
  baseline: string;
  setBaseline: (value: string) => void;
  hasUnsavedChanges: boolean;
  setUnsavedChanges: (value: boolean) => void;
}
export const WorkspaceContext = createContext<AppState | null>(null);
export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("Workspace context required");
  return ctx;
}

export function usePermission(scope: string) {
  const { w, session } = useWorkspace();
  return hasProjectPermission(session, w.project.id, scope);
}

export function useReadScope() {
  const { w, session, baseline } = useWorkspace();
  return workspaceReadScope(w, session, baseline);
}

export function workspaceReadScope(
  w: Workspace,
  session: Session,
  baseline: string,
) {
  return JSON.stringify([
    w.project.id,
    baseline,
    w.selected_analysis_set?.id,
    w.selected_release?.id,
    w.selected_analysis_set?.selection_version,
    session.project_permissions?.[w.project.id] ?? session.scopes ?? [],
    w.entities.map((e) => e.entity_revision_id).sort(),
    (w.relations || [])
      .map(
        (row) =>
          `${row.id}:${row.row_version}:${row.excluded}:${row.validity_status}`,
      )
      .sort(),
    (w.evidence || [])
      .map((row) => `${row.id}:${row.content_sha256 || ""}`)
      .sort(),
    (w.sources || []).map((source) => source.source_snapshot_id).sort(),
  ]);
}

// Remount read projections when visible inputs/grants change. Editable drafts
// keep their own lifetime and conflict handling outside this boundary.
export function ReadScope({ children }: { children: ReactNode }) {
  return createElement(Fragment, { key: useReadScope() }, children);
}
