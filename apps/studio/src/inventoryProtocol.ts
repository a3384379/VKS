import { isTarget, type Target } from "./runtime";

export type InventoryPage = {
  route: string;
  frame_path: string[];
  label: string;
  status?: "captured" | "failed" | "pending";
};
export type InventoryPackage = {
  package_id: string;
  sha256: string;
  url: string;
  kind: "prototype";
  format?: string;
  pages: InventoryPage[];
};
export type InventoryManifest = {
  analysis_set_id: string;
  selection_version: number;
  source_selection_version?: number;
  composition: string;
  editable: boolean;
  packages: InventoryPackage[];
  pages_total: number;
  concurrency?: number;
};
export type Capture = {
  scan_id: string;
  route: string;
  frame_path: string[];
  targets: Target[];
  observed: number;
  truncated: number;
  unverified_frames: number;
  status: "captured" | "failed";
  message?: string;
};
export type InventoryTask = {
  key: string;
  source: InventoryPackage;
  page: InventoryPage;
};
export type ScanReceipt = {
  scan_run?: {
    id: string;
    row_version: number;
    pages_completed: number;
    pages_attempted?: number;
    pages_failed: number;
    controls: number;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export function inventoryCapture(
  value: unknown,
  id: string,
  page: InventoryPage,
): Capture {
  const p = value as Capture;
  if (
    !p ||
    p.scan_id !== id ||
    p.route !== page.route ||
    JSON.stringify(p.frame_path) !== JSON.stringify(page.frame_path) ||
    !["captured", "failed"].includes(p.status) ||
    !Array.isArray(p.targets) ||
    p.targets.length > 1500 ||
    p.targets.some(
      (target) =>
        !isTarget(target) ||
        target.route !== page.route ||
        target.open_steps.length !== 0 ||
        !page.frame_path.every(
          (part, index) => target.frame_path[index] === part,
        ),
    ) ||
    ![p.observed, p.truncated, p.unverified_frames].every(
      (n) => Number.isSafeInteger(n) && n >= 0 && n <= 100000,
    ) ||
    p.observed < p.targets.length ||
    p.truncated !== p.observed - p.targets.length ||
    (p.status === "failed" && p.targets.length !== 0) ||
    (p.status === "captured" &&
      !p.targets.some(
        (target) =>
          target.selector === "body" &&
          JSON.stringify(target.frame_path) === JSON.stringify(page.frame_path),
      ))
  )
    throw new Error("原型扫描响应与声明页面不一致");
  return p;
}

export function scanReceipt(value: unknown, runId?: string): ScanReceipt {
  const receipt = value as ScanReceipt,
    run = receipt?.scan_run;
  if (
    runId &&
    (!run ||
      run.id !== runId ||
      !Number.isSafeInteger(run.row_version) ||
      run.row_version < 0 ||
      ![run.pages_completed, run.pages_failed, run.controls].every(
        (n) => Number.isSafeInteger(n) && n >= 0,
      ))
  )
    throw new Error("服务端未返回有效的采集进度确认，请检查工作区版本后恢复");
  return receipt;
}

/** Capture ACKs version page progress; AI has a separate lifecycle and is polled. */
export function mergeScanSnapshot<
  T extends { id: string; row_version?: number; ai?: unknown },
>(previous: T | null, next: T | null, capture = false): T | null {
  if (!previous || !next || previous.id !== next.id) return next;
  if (
    typeof previous.row_version === "number" &&
    typeof next.row_version === "number" &&
    next.row_version < previous.row_version
  )
    return previous;
  // A queued page can finish inference without changing the scan row version.
  // Never overwrite its later polled AI outcome with a delayed page receipt.
  return capture ? { ...next, ai: previous.ai } : next;
}
