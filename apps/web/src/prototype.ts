import type { Entity } from "./types";

export type PrototypeAnchor = {
  anchorKey: string;
  actionKey: string;
  rect: [number, number, number, number];
  targetStateKey?: string;
};
export type PrototypeState = {
  stateKey: string;
  name: string;
  viewport: { width: number; height: number; devicePixelRatio: number };
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
