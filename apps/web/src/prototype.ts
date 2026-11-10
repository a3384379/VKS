import type { Entity, Workspace } from "./types";

export type PrototypeAnchor = {
  anchorKey: string;
  actionKey: string;
  rect: [number, number, number, number];
  targetStateKey?: string;
};
export type PrototypeState = {
  stateKey: string;
  name: string;
  viewport?: { width: number; height: number; devicePixelRatio: number };
  surface?: { sha256: string };
  anchors: PrototypeAnchor[];
};
export type PrototypeManifest = {
  schemaVersion: "1.0";
  defaultStateKey: string;
  states: PrototypeState[];
};

export function prototypeManifest(page: Entity | undefined) {
  const value = page?.metadata?.prototype_manifest as
    | PrototypeManifest
    | undefined;
  return value?.schemaVersion === "1.0" ? value : undefined;
}

export type PrototypeSource = {
  section_id: string;
  section_revision_id: string;
  section_key: string;
  title: string;
  excerpt: string;
  validity: string;
};

export type BusinessPoint = {
  point_key: string;
  point_revision_id: string;
  title: string;
  summary: string;
  applicability: string;
  outcome: string;
  exceptions: string;
  sources: PrototypeSource[];
  binding_id: string;
  binding_version: number;
  state_keys: string[];
  priority: number;
  validity: string;
};

export type PrototypeContext = {
  points: BusinessPoint[];
  documents: PrototypeSource[];
};

export function prototypeScope(w: Workspace) {
  const query = new URLSearchParams();
  if (w.selected_release?.id) query.set("release_id", w.selected_release.id);
  else if (w.selected_analysis_set?.id)
    query.set("analysis_set_id", w.selected_analysis_set.id);
  return query;
}

export function prototypeApi(pid: string) {
  return `/api/v3/projects/${encodeURIComponent(pid)}/prototype`;
}
