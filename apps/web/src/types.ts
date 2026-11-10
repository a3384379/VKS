export type Dict = Record<string, unknown>;
export type View =
  | "overview"
  | "imports"
  | "workbench"
  | "documents"
  | "relations"
  | "graph"
  | "versions"
  | "settings"
  | "journeys"
  | "agent";
export interface Project {
  id: string;
  key: string;
  name: string;
  description?: string;
  default_release_id?: string | null;
  row_version?: number;
  created_at?: string;
}
export interface Entity {
  id: string;
  entity_id: string;
  entity_revision_id: string;
  type: string;
  name: string;
  display_name?: string;
  stable_key: string;
  content?: string;
  content_sha256?: string;
  metadata: Dict;
  locator: Dict;
  row_version: number;
  artifact_id?: string;
}
export interface Relation {
  id: string;
  from_revision_id: string;
  to_revision_id: string;
  from_name: string;
  to_name: string;
  type?: string;
  relation_type?: string;
  excluded: boolean;
  validity_status: string;
  causal_basis: string;
  evidence_ids: string[];
  reason?: string;
  row_version: number;
  manual_lock?: boolean;
  confidence_band?: string;
}
export interface Evidence {
  id: string;
  kind: string;
  evidence_kind?: string;
  artifact_id?: string;
  artifact_path?: string;
  claim?: string;
  content?: string;
  excerpt?: string;
  locator?: Dict;
  source_locator?: Dict;
  content_sha256?: string;
  source?: string;
}
export interface AnalysisSet {
  id: string;
  name: string;
  key?: string;
  state: string;
  row_version?: number;
  selection_version?: number;
  created_at?: string;
  runtime_alignment?: string;
}
export interface Release {
  id: string;
  release_no?: number | string;
  name?: string;
  analysis_set_id: string;
  manifest_sha256?: string;
  manifest_hash?: string;
  published_at: string;
  status: string;
}
export interface Job {
  id: string;
  created_by?: string;
  kind: string;
  state: string;
  stage: string;
  progress?: number;
  completeness: string;
  issue_count?: number;
  created_at: string;
  error_code?: string;
  error_message?: string;
  error?: { code?: string; message?: string; retryable?: boolean };
  result?: Dict;
  result_refs?: Dict;
}
export interface Source {
  id: string;
  source_snapshot_id?: string;
  source_id?: string;
  role: string;
  provider: string;
  provider_revision?: string;
  content_sha256?: string;
  name?: string;
  root?: string;
}
export interface Audit {
  id: string;
  action: string;
  actor?: string;
  resource?: string;
  result?: string;
  created_at: string;
}
export interface Token {
  id: string;
  name: string;
  scopes: string[];
  expires_at?: string;
  revoked_at?: string | null;
  created_at?: string;
}
export interface Workspace {
  backup_status?: Partial<
    Record<
      "backup" | "restore",
      {
        timestamp: string;
        database_kind: "sqlite" | "postgres";
        sha256: string;
      }
    >
  >;
  prototype_settings?: {
    entry_page_id?: string | null;
    menu_order: string[];
    row_version: number;
  };
  project: Project;
  analysis_sets: AnalysisSet[];
  selected_analysis_set?: AnalysisSet | null;
  releases: Release[];
  selected_release?: Release | null;
  entities: Entity[];
  relations: Relation[];
  evidence: Evidence[];
  documents: Entity[];
  pages: Entity[];
  actions: Entity[];
  scenarios: Entity[];
  sources: Source[];
  jobs: Job[];
  audit_events: Audit[];
  capabilities: Dict;
  runtime_status?: {
    runner_enabled?: boolean;
    model_enabled?: boolean;
    oauth_enabled?: boolean;
    task_mode?: string;
  };
  unknowns: unknown[];
  stats: Dict;
}
export interface Session {
  authenticated: boolean;
  bootstrap_required?: boolean;
  csrf_token?: string;
  name?: string;
  actor_id?: string;
  human?: boolean;
  project_permissions?: Record<string, string[]>;
  user?: {
    id?: string;
    name?: string;
    username?: string;
    display_name?: string;
    status?: string;
    row_version?: number;
    instance_admin?: boolean;
    password_configured?: boolean;
  };
  actor?: string;
  scopes?: string[];
}
export interface Preflight {
  valid?: boolean;
  file_count?: number;
  total_size_bytes?: number;
  issues?: unknown[];
  manifest?: Dict;
  summary?: Dict;
  [key: string]: unknown;
}
export interface TeamUser {
  id: string;
  username: string;
  display_name: string;
  status: string;
  row_version: number;
  instance_admin: boolean;
  password_configured: boolean;
}
export interface TeamMember {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
  status: string;
  permissions: string[];
}
export interface SourceConnector {
  id: string;
  key: string;
  name: string;
  provider: string;
  remote_locator?: string;
  secret_ref?: string | null;
  collection_mode: string;
  row_version: number;
  read_only: boolean;
}
