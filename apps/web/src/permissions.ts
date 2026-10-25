import type { Session } from "./types";

export function hasProjectPermission(
  session: Session,
  projectId: string,
  scope: string,
): boolean {
  if (!session.authenticated) return false;
  if (session.project_permissions)
    return (session.project_permissions[projectId] || []).includes(scope);
  return (session.scopes || []).includes(scope);
}
