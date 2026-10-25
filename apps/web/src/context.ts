import { createContext, useContext } from "react";
import type { Entity, Session, View, Workspace } from "./types";
import { hasProjectPermission } from "./permissions";
export interface AppState {
  w: Workspace;
  session: Session;
  selected: string;
  setSelected: (id: string) => void;
  navigate: (view: View, entity?: Entity) => void;
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
