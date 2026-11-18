import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, mutation } from "../../web/src/api";
import {
  OriginalRuntime,
  isTarget,
  type RuntimeEvent,
  type RuntimeHandle,
} from "./runtime";
import {
  inventoryCapture,
  scanReceipt,
  type Capture,
  type InventoryManifest,
  type InventoryPackage,
  type InventoryTask,
  type ScanReceipt,
} from "./inventoryProtocol";

export function InventoryScanWorker({
  worker,
  identity,
  project,
  accountId,
  manifest,
  scanRunId,
  clientId,
  claim,
  release,
  isActive,
  onWorking,
  onSaved,
  onDone,
  onFatal,
}: {
  worker: number;
  identity: string;
  project: string;
  accountId: string;
  manifest: InventoryManifest;
  scanRunId?: string;
  clientId?: string;
  claim: (worker: number) => InventoryTask | undefined;
  release: (worker: number, task: InventoryTask) => void;
  isActive: () => boolean;
  onWorking: (worker: number, task: InventoryTask | null) => void;
  onSaved: (
    worker: number,
    task: InventoryTask,
    capture: Capture,
    receipt: ScanReceipt,
  ) => void;
  onDone: (worker: number) => void;
  onFatal: (error: Error) => void;
}) {
  const handle = useRef<RuntimeHandle | null>(null);
  const [runtime, setRuntime] = useState<{
    source: InventoryPackage;
    token: string;
  } | null>(null);
  const ready = useRef<{
    token: string;
    resolve: () => void;
    materialLoaded: boolean;
    pageReady: boolean;
    source: InventoryPackage;
  } | null>(null);
  const pending = useRef<{
    id: string;
    task: InventoryTask;
    resolve: (value: Capture) => void;
    reject: (error: Error) => void;
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true,
      held: InventoryTask | undefined,
      loaded = "";
    const check = () => {
      if (!alive || controller.signal.aborted || !isActive())
        throw new DOMException("预扫描已停止", "AbortError");
    };
    const wait = <T,>(
      make: (
        resolve: (value: T) => void,
        reject: (error: Error) => void,
      ) => void,
      delay: number,
    ) =>
      new Promise<T>((resolve, reject) => {
        const clear = () => {
          clearTimeout(timer);
          controller.signal.removeEventListener("abort", abort);
        };
        const abort = () => {
          clear();
          reject(new DOMException("预扫描已停止", "AbortError"));
        };
        const timer = setTimeout(() => {
          clear();
          reject(new Error("隔离页面加载或扫描超时"));
        }, delay);
        controller.signal.addEventListener("abort", abort, { once: true });
        make(
          (value) => {
            clear();
            resolve(value);
          },
          (error) => {
            clear();
            reject(error);
          },
        );
      });
    void (async () => {
      try {
        while (true) {
          check();
          held = claim(worker);
          if (!held) break;
          const task = held,
            { source, page } = task;
          onWorking(worker, task);
          const id = crypto.randomUUID();
          let capture: Capture;
          try {
            const key = source.package_id + ":" + source.sha256;
            if (loaded !== key) {
              const token = crypto.randomUUID();
              const loading = wait<void>((resolve) => {
                const waitsForPage =
                  ["charge-prototype", "static-site"].includes(
                    source.format || "",
                  ) &&
                  source.pages.some((item) =>
                    item.frame_path.includes("actualPage"),
                  );
                ready.current = {
                  token,
                  resolve,
                  materialLoaded: false,
                  pageReady: !waitsForPage,
                  source,
                };
              }, 30000);
              setRuntime({ source, token });
              await loading;
              check();
              handle.current?.send("inventory.configure", {
                format: source.format || "html",
                pages: source.pages.map(({ route, frame_path }) => ({
                  route,
                  frame_path,
                })),
              });
              loaded = key;
            }
            capture = await wait<Capture>((resolve, reject) => {
              pending.current = { id, task, resolve, reject };
              handle.current?.send("inventory.scan", {
                scan_id: id,
                route: page.route,
                frame_path: page.frame_path,
              });
            }, 14000);
          } catch (error) {
            check();
            handle.current?.send("inventory.cancel");
            loaded = "";
            capture = {
              scan_id: id,
              route: page.route,
              frame_path: page.frame_path,
              targets: [],
              observed: 0,
              truncated: 0,
              unverified_frames: 0,
              status: "failed",
              message: error instanceof Error ? error.message : String(error),
            };
          }
          pending.current = null;
          check();
          const response = await api<ScanReceipt>(
            `/api/v3/projects/${project}/associations/inventory`,
            {
              ...mutation(
                "PUT",
                {
                  analysis_set_id: manifest.analysis_set_id,
                  package_id: source.package_id,
                  artifact_sha256: source.sha256,
                  composition: manifest.composition,
                  route: page.route,
                  frame_path: page.frame_path,
                  targets: capture.targets,
                  observed: capture.observed,
                  truncated: capture.truncated,
                  unverified_frames: capture.unverified_frames,
                  status: capture.status,
                  ...(scanRunId
                    ? { scan_run_id: scanRunId, client_id: clientId }
                    : {}),
                },
                manifest.source_selection_version ?? manifest.selection_version,
              ),
              signal: controller.signal,
            },
          );
          check();
          onSaved(worker, task, capture, scanReceipt(response, scanRunId));
          held = undefined;
        }
        check();
        setRuntime(null);
        onWorking(worker, null);
        onDone(worker);
      } catch (error) {
        if (alive && !controller.signal.aborted && isActive())
          onFatal(error instanceof Error ? error : new Error(String(error)));
      } finally {
        if (held) release(worker, held);
      }
    })();
    return () => {
      alive = false;
      controller.abort();
      handle.current?.send("inventory.cancel");
      if (held) {
        release(worker, held);
        held = undefined;
      }
      pending.current = null;
      ready.current = null;
    };
  }, [
    worker,
    identity,
    project,
    accountId,
    manifest,
    scanRunId,
    clientId,
    claim,
    release,
    isActive,
    onWorking,
    onSaved,
    onDone,
    onFatal,
  ]);
  const event = (message: RuntimeEvent, token: string) => {
    const loading = ready.current;
    if (loading?.token === token) {
      if (message.type === "material.loaded") loading.materialLoaded = true;
      if (message.type === "context.changed") {
        const context = message.payload as {
          route?: unknown;
          frame_path?: unknown;
          module?: { target?: unknown };
        };
        // The wrapper initializes its default page asynchronously after srcdoc
        // assignment. Navigating earlier can be overwritten by that startup.
        // Accept an observed page in the declared child frame. A resumed scan
        // can omit the already captured default route from its remaining plan.
        // Keep the existing material-load timeout and cancellation scope.
        if (
          loading.source.pages.some(
            (item) =>
              item.frame_path.includes("actualPage") &&
              typeof context.route === "string" &&
              context.route.startsWith("/") &&
              context.route !== "/" &&
              isTarget(context.module?.target) &&
              JSON.stringify(context.frame_path) ===
                JSON.stringify(item.frame_path),
          )
        )
          loading.pageReady = true;
      }
      if (loading.materialLoaded && loading.pageReady) {
        loading.resolve();
        ready.current = null;
      }
    }
    const current = pending.current;
    if (
      message.type !== "inventory.result" ||
      !current ||
      (message.payload as { scan_id?: unknown })?.scan_id !== current.id
    )
      return;
    try {
      current.resolve(
        inventoryCapture(message.payload, current.id, current.task.page),
      );
    } catch (error) {
      current.reject(error as Error);
    }
  };
  return runtime
    ? createPortal(
        <div
          aria-hidden="true"
          inert
          data-scan-worker={worker + 1}
          style={{
            position: "fixed",
            left: -20000,
            top: 0,
            width: 1440,
            height: 900,
            pointerEvents: "none",
            opacity: 0,
            contain: "strict",
          }}
        >
          <OriginalRuntime
            key={runtime.token}
            url={runtime.source.url}
            digest={runtime.source.sha256}
            kind="prototype"
            inventoryOnly
            storageScope={{
              accountId: accountId || "studio",
              projectId: project,
              packageId:
                runtime.source.package_id + ":scan:" + identity + ":w" + worker,
            }}
            onEvent={(message) => event(message, runtime.token)}
            handle={handle}
          />
        </div>,
        document.body,
      )
    : null;
}
