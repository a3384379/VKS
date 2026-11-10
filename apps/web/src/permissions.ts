import type { Session } from "./types";

export function projectScopes(session: Session | null, projectId: string) {
  if (!session?.authenticated) return [];
  return session.project_permissions
    ? session.project_permissions[projectId] || []
    : session.scopes || [];
}

export function hasProjectPermission(
  session: Session,
  projectId: string,
  scope: string,
): boolean {
  return projectScopes(session, projectId).includes(scope);
}
