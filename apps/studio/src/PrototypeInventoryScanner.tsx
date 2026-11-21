import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LoaderCircle, Square } from "lucide-react";
import { InventoryScanWorker } from "./InventoryScanWorker";
import { TaskTiming, type Timing } from "./TaskTiming";
import type {
  Capture,
  InventoryManifest,
  InventoryTask,
  ScanReceipt,
} from "./inventoryProtocol";
export { inventoryCapture } from "./inventoryProtocol";
export type {
  InventoryPage,
  InventoryPackage,
  InventoryManifest,
} from "./inventoryProtocol";

type Current = { package_id: string; route: string; frame_path: string[] };
type Progress = {
  session: string;
  completed: number;
  controls: number;
  attempted: number;
  failed: number;
  version: number;
  working: Record<number, string>;
};
export function PrototypeInventoryScanner({
  project,
  accountId,
  manifest,
  onComplete,
  onCancel,
  onError,
  scanRunId,
  clientId,
  onCurrent,
  onSaved,
  progressBase,
  concurrency,
  timing,
  adaptive = false,
  incremental = false,
  onPause,
}: {
  project: string;
  accountId: string;
  manifest: InventoryManifest;
  onComplete: () => void;
  onCancel: () => void;
  onError: (error: Error) => void;
  scanRunId?: string;
  clientId?: string;
  onCurrent?: (current: Current) => void;
  onSaved?: (receipt: ScanReceipt) => void;
  progressBase?: {
    completed: number;
    controls: number;
    total: number;
    attempted?: number;
    failed?: number;
  };
  concurrency?: number;
  timing?: Timing;
  adaptive?: boolean;
  incremental?: boolean;
  onPause?: () => void;
}) {
  const configured = concurrency ?? manifest.concurrency ?? 3;
  const limit =
    Number.isInteger(configured) && configured >= 1 && configured <= 4
      ? configured
      : 3;
  const maxAdaptive = Math.max(1, Math.min(
    limit, Math.floor((navigator.hardwareConcurrency || 4) / 2),
    ((navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? 8) <= 4 ? 2 : limit,
  ));
  const pool = useMemo(() => {
    const seen = new Set<string>(),
      tasks: InventoryTask[] = [];
    for (const source of manifest.packages)
      for (const page of source.pages) {
        const key = JSON.stringify([
          source.package_id,
          page.route,
          page.frame_path,
        ]);
        if (!seen.has(key)) {
          seen.add(key);
          tasks.push({ key, source, page });
        }
      }
    const id = crypto.randomUUID();
    const initial: Progress = {
      session: id,
      completed: progressBase?.completed || 0,
      controls: progressBase?.controls || 0,
      attempted: progressBase?.attempted || 0,
      failed: progressBase?.failed || 0,
      version: -1,
      working: {},
    };
    return {
      id,
      tasks,
      cursor: 0,
      retry: [] as InventoryTask[],
      inflight: new Map<string, number>(),
      done: new Set<string>(),
      finished: new Set<number>(),
      failures: 0,
      stopped: false,
      initial,
    };
  }, [manifest, project, scanRunId, clientId, limit]);
  const poolRef = useRef(pool);
  poolRef.current = pool;
  const callbacks = useRef({
    onComplete,
    onCancel,
    onError,
    onCurrent,
    onSaved,
    onPause,
  });
  callbacks.current = {
    onComplete,
    onCancel,
    onError,
    onCurrent,
    onSaved,
    onPause,
  };
  const [view, setView] = useState<Progress>(pool.initial),
    [halted, setHalted] = useState<string | null>(null);
  const [lanes, setLanes] = useState(1);
  const lanesRef = useRef(1);
  useEffect(() => {
    if (!adaptive) return;
    let healthy = 0;
    let tick = performance.now();
    lanesRef.current = 1;
    setLanes(1);
    const timer = setInterval(() => {
      const now = performance.now();
      const lag = now - tick - 500;
      tick = now;
      const memory = (performance as Performance & {
        memory?: { usedJSHeapSize: number; jsHeapSizeLimit: number };
      }).memory;
      const heap = memory ? memory.usedJSHeapSize / memory.jsHeapSizeLimit : 0;
      if (document.hidden || lag >= 80 || heap >= 0.65) {
        healthy = 0;
        lanesRef.current = Math.max(1, lanesRef.current - 1);
      } else if (lag <= 25 && ++healthy >= 3) {
        healthy = 0;
        lanesRef.current = Math.min(maxAdaptive, lanesRef.current + 1);
      }
      setLanes(lanesRef.current);
    }, 500);
    return () => clearInterval(timer);
  }, [adaptive, maxAdaptive, pool]);
  const progress = view.session === pool.id ? view : pool.initial;
  const count = Math.min(limit, pool.tasks.length);
  const isActive = useCallback(
    () => poolRef.current === pool && !pool.stopped,
    [pool],
  );
  const claim = useCallback(
    (worker: number) => {
      if (!isActive()) return;
      if (adaptive && pool.inflight.size >= lanesRef.current) return null;
      while (true) {
        const task = pool.retry.pop() || pool.tasks[pool.cursor++];
        if (!task) return;
        if (pool.done.has(task.key) || pool.inflight.has(task.key)) continue;
        pool.inflight.set(task.key, worker);
        return task;
      }
    },
    [adaptive, pool, isActive],
  );
  const release = useCallback(
    (worker: number, task: InventoryTask) => {
      if (
        !isActive() ||
        pool.done.has(task.key) ||
        pool.inflight.get(task.key) !== worker
      )
        return;
      pool.inflight.delete(task.key);
      pool.retry.push(task);
    },
    [pool, isActive],
  );
  const working = useCallback(
    (worker: number, task: InventoryTask | null) => {
      if (!isActive()) return;
      if (task)
        callbacks.current.onCurrent?.({
          package_id: task.source.package_id,
          route: task.page.route,
          frame_path: task.page.frame_path,
        });
      setView((previous) => {
        const current = previous.session === pool.id ? previous : pool.initial,
          labels = { ...current.working };
        if (task) labels[worker] = task.page.label || task.page.route;
        else delete labels[worker];
        return { ...current, working: labels };
      });
    },
    [pool, isActive],
  );
  const saved = useCallback(
    (
      worker: number,
      task: InventoryTask,
      capture: Capture,
      receipt: ScanReceipt,
    ) => {
      if (
        !isActive() ||
        pool.done.has(task.key) ||
        pool.inflight.get(task.key) !== worker
      )
        return;
      pool.done.add(task.key);
      pool.inflight.delete(task.key);
      if (capture.status === "failed") pool.failures++;
      const ack = receipt.scan_run;
      setView((previous) => {
        const current = previous.session === pool.id ? previous : pool.initial;
        if (ack) {
          if (ack.row_version < current.version) return current;
          return {
            ...current,
            completed: ack.pages_completed,
            attempted:
              ack.pages_attempted ?? ack.pages_completed + ack.pages_failed,
            failed: ack.pages_failed,
            controls: ack.controls,
            version: ack.row_version,
          };
        }
        return {
          ...current,
          completed:
            current.completed + (capture.status === "captured" ? 1 : 0),
          attempted: current.attempted + 1,
          failed: current.failed + (capture.status === "failed" ? 1 : 0),
          controls:
            current.controls +
            capture.targets.filter((target) => target.selector !== "body")
              .length,
        };
      });
      callbacks.current.onSaved?.(receipt);
    },
    [pool, isActive],
  );
  const fatal = useCallback(
    (error: Error) => {
      if (!isActive()) return;
      pool.stopped = true;
      setHalted(pool.id);
      callbacks.current.onError(error);
    },
    [pool, isActive],
  );
  const done = useCallback(
    (worker: number) => {
      if (!isActive() || pool.finished.has(worker)) return;
      pool.finished.add(worker);
      if (pool.finished.size !== count) return;
      if (pool.done.size !== pool.tasks.length) {
        fatal(new Error("仍有未完成页面，采集进度已保存，请恢复扫描"));
        return;
      }
      if (pool.failures) {
        fatal(
          new Error(
            `${pool.failures} 个页面尚未完成，已保留进度。${incremental ? "已采集内容的后台 AI 会继续处理。" : "请重试，或选择仅关联已采集内容。"}`,
          ),
        );
        return;
      }
      pool.stopped = true;
      setHalted(pool.id);
      callbacks.current.onComplete();
    },
    [pool, count, incremental, isActive, fatal],
  );
  const stop = (pause: boolean) => {
    if (!isActive()) return;
    pool.stopped = true;
    setHalted(pool.id);
    if (pause) callbacks.current.onPause?.();
    else callbacks.current.onCancel();
  };
  return (
    <div
      className="batch-runtime-scan"
      role="status"
      aria-label="动态原型预扫描"
    >
      <p>
        <LoaderCircle size={14} className="spinning" />
        正在预扫描页面 {progress.completed} /{" "}
        {progressBase?.total ?? manifest.pages_total} · 已发现控件{" "}
        {progress.controls}
        {progress.failed ? ` · ${progress.failed} 页未完成` : ""}
      </p>
      <small>
        {adaptive ? `自动调节 · 当前上限 ${Math.min(count, lanes)} 路` : `${count} 路并行采集`} · 已尝试 {progress.attempted}{" "}
        页；仅采集默认页面，未打开的弹窗和条件状态仍未覆盖。
      </small>
      {Object.entries(progress.working).map(([worker, label]) => (
        <small key={worker} style={{ display: "block" }}>
          页面 {Number(worker) + 1}：{label}
        </small>
      ))}
      <progress
        max={Math.max(1, progressBase?.total ?? manifest.pages_total)}
        value={progress.completed}
      />
      <TaskTiming
        timing={timing}
        state="running"
        processed={progress.completed}
        total={progressBase?.total ?? manifest.pages_total}
      />
      {onPause && <button onClick={() => stop(true)}>暂停扫描</button>}
      <button onClick={() => stop(false)}>
        <Square size={13} />
        {onPause ? "停止任务" : "停止预扫描"}
      </button>
      {halted !== pool.id &&
        Array.from({ length: count }, (_, worker) => (
          <InventoryScanWorker
            key={pool.id + ":" + worker}
            worker={worker}
            identity={pool.id}
            project={project}
            accountId={accountId}
            manifest={manifest}
            scanRunId={scanRunId}
            clientId={clientId}
            claim={claim}
            release={release}
            isActive={isActive}
            onWorking={working}
            onSaved={saved}
            onDone={done}
            onFatal={fatal}
          />
        ))}
    </div>
  );
}
