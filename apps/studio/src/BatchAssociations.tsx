import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, Play, RefreshCw, Square } from "lucide-react";
import { api, mutation } from "../../web/src/api";
import {
  PrototypeInventoryScanner,
  type InventoryManifest,
} from "./PrototypeInventoryScanner";
import { mergeScanSnapshot, type ScanReceipt } from "./inventoryProtocol";

type Mode = "missing" | "changed" | "all";
type Outcome = { id?: string; label: string; status: string; message: string };
type Coverage = {
  discovered: number;
  modules: number;
  controls: number;
  linked: number;
  manual: number;
  excluded: number;
  pending: number;
  operation_groups?: number;
  duplicate_controls?: number;
  uncovered?: number;
  limitations?: (string | { message: string })[];
  coverage_complete?: boolean;
};
type Job = {
  id: string;
  state: string;
  progress: number;
  stage: string;
  mode?: Mode;
  current_scope?: boolean;
  scan_run_id?: string;
  scan_pipeline?: boolean;
  error?: { message: string };
  result?: {
    processed?: number;
    total?: number;
    saved?: number;
    pending?: number;
    details_truncated?: boolean;
    completed?: Record<string, Outcome>;
  };
};
type Status = {
  analysis_set_id: string;
  selection_version: number;
  editable: boolean;
  coverage: Coverage;
  jobs: Job[];
};
type ScanRun = {
  id: string;
  mode: Mode;
  state:
    | "running"
    | "interrupted"
    | "partial"
    | "completed"
    | "cancelled"
    | "invalidated";
  current_scope: boolean;
  client_id: string;
  selection_version: number;
  source_selection_version?: number;
  row_version?: number;
  concurrency?: number;
  pipeline?: boolean;
  ai?: {
    queued: number;
    running: number;
    completed: number;
    failed: number;
    saved: number;
    targets: number;
    waiting?: number;
    cancelled?: number;
    pending_admission?: number;
    error?: string;
    jobs: number;
  };
  pages_total: number;
  pages_completed: number;
  pages_attempted?: number;
  pages_failed: number;
  controls: number;
  current: {
    package_id: string;
    route: string;
    frame_path: string[];
    label?: string;
  } | null;
  batch_job_id: string | null;
  manifest: InventoryManifest;
};
const active = (j: Job) =>
  ["queued", "running", "retry_wait", "cancelling"].includes(j.state);
export function BatchAssociations({
  project,
  analysis,
  editable,
  accountId = "",
  visible = true,
  onChanged,
}: {
  project: string;
  analysis: string;
  editable: boolean;
  accountId?: string;
  visible?: boolean;
  onChanged: () => Promise<unknown>;
}) {
  const base = `/api/v3/projects/${project}/associations/batch`;
  const scanBase = `/api/v3/projects/${project}/associations/scans`;
  const [clientId] = useState(() => crypto.randomUUID());
  const [scanConcurrency, setScanConcurrency] = useState(3);
  const [scanRun, setScanRun] = useState<ScanRun | null>(null);
  const [storedProgress, setStoredProgress] = useState<{
    pages_total: number;
    pages_captured: number;
    pages_failed: number;
    controls: number;
  } | null>(null);
  const ownedRun = useRef<string | null>(null),
    scanSerial = useRef(0),
    scanCurrent = useRef<ScanRun["current"]>(null);
  const [data, setData] = useState<Status | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [details, setDetails] = useState<{
      job: string;
      items: Outcome[];
      hasMore: boolean;
    } | null>(null),
    [detailsBusy, setDetailsBusy] = useState(false);
  const [scanSession, setScanSession] = useState<{
    run: ScanRun;
    manifest: InventoryManifest;
    resolve: () => void;
    reject: (error: Error) => void;
  } | null>(null);
  const scanPending = useRef<{ reject: (error: Error) => void } | null>(null);
  const pending = useRef(false),
    prior = useRef(new Map<string, string>()),
    epoch = useRef(0),
    readSerial = useRef(0),
    detailSerial = useRef(0),
    notification = useRef<{
      timer?: ReturnType<typeof setTimeout>;
      active: boolean;
      again: boolean;
    }>({ active: false, again: false }),
    savedSeen = useRef(new Map<string, number>()),
    changed = useRef(onChanged);
  changed.current = onChanged;
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const queueChanged = useCallback(() => {
    const n = epoch.current,
      state = notification.current;
    if (state.active) {
      state.again = true;
      return;
    }
    if (state.timer) return;
    state.timer = setTimeout(() => {
      if (n !== epoch.current) return;
      state.timer = undefined;
      state.active = true;
      void changed
        .current()
        .catch((error) => {
          if (n === epoch.current) setError(error.message);
        })
        .finally(() => {
          if (n !== epoch.current) return;
          state.active = false;
          if (state.again) {
            state.again = false;
            queueChanged();
          }
        });
    }, 150);
  }, []);
  const acceptScan = useCallback(
    (next: ScanRun | null, capture = false) =>
      setScanRun((previous) => mergeScanSnapshot(previous, next, capture)),
    [],
  );
  const load = useCallback(
    async (signal?: AbortSignal, n = epoch.current) => {
      if (n !== epoch.current) return;
      const sequence = ++readSerial.current;
      const next = await api<Status>(`${base}?analysis_set_id=${analysis}`, {
        signal,
      });
      if (
        signal?.aborted ||
        n !== epoch.current ||
        sequence !== readSerial.current
      )
        return;
      setData(next);
      let terminal = false;
      for (const job of next.jobs) {
        if (
          job.current_scope !== false &&
          prior.current.has(job.id) &&
          ["queued", "running", "retry_wait", "cancelling"].includes(
            prior.current.get(job.id)!,
          ) &&
          !active(job)
        )
          terminal = true;
        prior.current.set(job.id, job.state);
      }
      if (terminal) queueChanged();
      return next;
    },
    [base, analysis, queueChanged],
  );
  const loadScan = useCallback(
    async (signal?: AbortSignal, n = epoch.current) => {
      const serial = ++scanSerial.current;
      const result = await api<{
        run: ScanRun | null;
        stored_progress?: {
          pages_total: number;
          pages_captured: number;
          pages_failed: number;
          controls: number;
        };
      }>(`${scanBase}?analysis_set_id=${analysis}`, { signal });
      if (
        n !== epoch.current ||
        signal?.aborted ||
        serial !== scanSerial.current
      )
        return null;
      acceptScan(result.run);
      if (result.run?.ai) {
        const previous = savedSeen.current.get(result.run.id) || 0;
        if (result.run.ai.saved > previous) queueChanged();
        savedSeen.current.set(
          result.run.id,
          Math.max(previous, result.run.ai.saved),
        );
      }
      setStoredProgress(result.stored_progress || null);
      return result.run;
    },
    [scanBase, analysis, acceptScan, queueChanged],
  );
  useEffect(() => {
    epoch.current++;
    setData(null);
    setError("");
    setBusy(false);
    setDetails(null);
    detailSerial.current++;
    setDetailsBusy(false);
    setScanSession(null);
    setScanRun(null);
    setStoredProgress(null);
    pending.current = false;
    prior.current.clear();
    savedSeen.current.clear();
    if (notification.current.timer) clearTimeout(notification.current.timer);
    notification.current = { active: false, again: false };
    return () => {
      epoch.current++;
      scanPending.current?.reject(
        new DOMException("预扫描范围已切换", "AbortError"),
      );
      scanPending.current = null;
      const id = ownedRun.current;
      ownedRun.current = null;
      if (id)
        void api(`${scanBase}/${id}/pause`, {
          ...mutation("POST", { client_id: clientId }),
          keepalive: true,
        }).catch(() => {});
    };
  }, [load, loadScan, scanBase, clientId]);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    void load(controller.signal).catch((error) => {
      if (!controller.signal.aborted) setError(error.message);
    });
    void loadScan(controller.signal).catch((error) => {
      if (!controller.signal.aborted) setError(error.message);
    });
    return () => controller.abort();
  }, [visible, load, loadScan]);
  useEffect(() => {
    if (
      !scanSession &&
      !(
        scanRun?.pipeline &&
        (scanRun.ai?.queued || 0) +
          (scanRun.ai?.running || 0) +
          (scanRun.ai?.waiting || 0) +
          (scanRun.ai?.pending_admission || 0) >
          0
      )
    )
      return;
    const controller = new AbortController();
    const heartbeat = async () => {
      try {
        if (scanSession)
          await api(`${scanBase}/${scanSession.run.id}/heartbeat`, {
            ...mutation("POST", {
              client_id: clientId,
              ...(scanCurrent.current ? { current: scanCurrent.current } : {}),
            }),
            signal: controller.signal,
          });
        await loadScan(controller.signal);
        if (visibleRef.current) await load(controller.signal);
      } catch (error) {
        if (!controller.signal.aborted) {
          if (scanSession) {
            scanPending.current?.reject(error as Error);
            setScanSession(null);
          }
          setError((error as Error).message);
        }
      }
    };
    const timer = setInterval(() => void heartbeat(), 5000);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [
    scanSession?.run.id,
    scanRun?.id,
    !!(
      (scanRun?.ai?.queued || 0) +
      (scanRun?.ai?.running || 0) +
      (scanRun?.ai?.waiting || 0) +
      (scanRun?.ai?.pending_admission || 0)
    ),
    scanBase,
    clientId,
    loadScan,
    load,
  ]);
  const current =
    data?.jobs.find(
      (j) => !j.scan_pipeline && j.current_scope !== false && active(j),
    ) || data?.jobs.find((j) => !j.scan_pipeline && j.current_scope !== false);
  useEffect(() => {
    if (!current || !active(current)) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await load(controller.signal);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2200);
    };
    timer = setTimeout(poll, 2200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [load, current?.id, current?.state]);
  async function run(
    mode: Mode,
    resume?: string,
    restoreScan?: ScanRun,
    allowPartial = false,
  ) {
    if (pending.current || !data) return;
    const n = epoch.current;
    pending.current = true;
    setBusy(true);
    setError("");
    setDetails(null);
    setDetailsBusy(false);
    detailSerial.current++;
    try {
      let expected = data.selection_version;
      let scanId: string | undefined;
      if (!resume) {
        const selected =
          restoreScan &&
          (allowPartial ||
            (restoreScan.pipeline &&
              restoreScan.state === "completed" &&
              !restoreScan.manifest.pages_total))
            ? restoreScan
            : await api<ScanRun>(
                restoreScan ? `${scanBase}/${restoreScan.id}/resume` : scanBase,
                mutation(
                  "POST",
                  restoreScan
                    ? { client_id: clientId, pipeline: true }
                    : {
                        analysis_set_id: analysis,
                        mode,
                        client_id: clientId,
                        concurrency: scanConcurrency,
                        pipeline: true,
                      },
                  restoreScan
                    ? (restoreScan.source_selection_version ??
                        restoreScan.manifest.source_selection_version ??
                        restoreScan.manifest.selection_version)
                    : data.selection_version,
                ),
              );
        if (n !== epoch.current) {
          void api(
            `${scanBase}/${selected.id}/pause`,
            mutation("POST", { client_id: clientId }),
          ).catch(() => {});
          return;
        }
        ++scanSerial.current;
        acceptScan(selected);
        ownedRun.current = selected.id;
        scanId = selected.id;
        expected =
          selected.source_selection_version ??
          selected.manifest.source_selection_version ??
          selected.manifest.selection_version;
        const manifest = selected.manifest;
        if (manifest.pages_total && !allowPartial)
          await new Promise<void>((resolve, reject) => {
            scanPending.current = { reject };
            setScanSession({ run: selected, manifest, resolve, reject });
          });
        if (selected.pipeline) {
          ownedRun.current = null;
          await load(undefined, n);
          await loadScan(undefined, n);
          return;
        }
      }
      if (n !== epoch.current) return;
      await api(
        base,
        mutation(
          "POST",
          {
            analysis_set_id: analysis,
            mode,
            concurrency: 3,
            ...(resume ? { resume_job_id: resume } : {}),
            ...(scanId ? { scan_run_id: scanId } : {}),
            ...(allowPartial ? { allow_partial: true } : {}),
          },
          expected,
        ),
      );
      ownedRun.current = null;
      await load(undefined, n);
      await loadScan(undefined, n);
    } catch (e) {
      if (n !== epoch.current) return;
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
      void load(undefined, n).catch(() => {});
      void loadScan(undefined, n).catch(() => {});
    } finally {
      if (n === epoch.current) {
        scanPending.current = null;
        setScanSession(null);
        pending.current = false;
        setBusy(false);
      }
    }
  }
  async function stopScan() {
    const selected = scanRun || scanSession?.run;
    if (!selected) return;
    scanPending.current?.reject(
      new DOMException("任务已停止，已保存的关联保留", "AbortError"),
    );
    setScanSession(null);
    ownedRun.current = null;
    try {
      await api(
        `${scanBase}/${selected.id}/cancel`,
        mutation("POST", { client_id: selected.client_id || clientId }),
      );
      await loadScan();
    } catch (error) {
      setError((error as Error).message);
    }
  }
  async function pauseScan() {
    const selected = scanRun || scanSession?.run;
    if (!selected) return;
    scanPending.current?.reject(
      new DOMException("扫描已暂停，后台AI继续处理已采集内容", "AbortError"),
    );
    setScanSession(null);
    ownedRun.current = null;
    try {
      await api(
        `${scanBase}/${selected.id}/pause`,
        mutation("POST", { client_id: selected.client_id || clientId }),
      );
      await loadScan();
    } catch (error) {
      setError((error as Error).message);
    }
  }
  async function retryAI() {
    if (!scanRun || pending.current) return;
    const n = epoch.current;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const next = await api<ScanRun>(
        `${scanBase}/${scanRun.id}/retry-ai`,
        mutation(
          "POST",
          {},
          scanRun.source_selection_version ??
            scanRun.manifest.source_selection_version ??
            scanRun.manifest.selection_version,
        ),
      );
      if (n === epoch.current) {
        acceptScan(next);
        await loadScan();
        await load();
      }
    } catch (error) {
      if (n === epoch.current) setError((error as Error).message);
    } finally {
      if (n === epoch.current) {
        pending.current = false;
        setBusy(false);
      }
    }
  }
  async function stop(job: Job) {
    if (pending.current) return;
    const n = epoch.current;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      await api(`/jobs/${job.id}/cancel`, mutation("POST", {}));
      await load(undefined, n);
    } catch (e) {
      if (n === epoch.current) setError((e as Error).message);
    } finally {
      if (n === epoch.current) {
        pending.current = false;
        setBusy(false);
      }
    }
  }
  async function loadDetails(job: Job) {
    if (detailsBusy || active(job)) return;
    const n = epoch.current;
    const sequence = ++detailSerial.current;
    const previous = details?.job === job.id ? details.items : [];
    setDetailsBusy(true);
    try {
      const page = await api<{ items: Outcome[]; has_more: boolean }>(
        `${base}/items?job_id=${encodeURIComponent(job.id)}&offset=${previous.length}&limit=100`,
      );
      if (n === epoch.current && sequence === detailSerial.current)
        setDetails({
          job: job.id,
          items: [...previous, ...page.items],
          hasMore: page.has_more,
        });
    } catch (e) {
      if (n === epoch.current && sequence === detailSerial.current)
        setError((e as Error).message);
    } finally {
      if (n === epoch.current && sequence === detailSerial.current)
        setDetailsBusy(false);
    }
  }
  const pipelineJobs =
    data?.jobs.filter(
      (job) => job.scan_pipeline && job.scan_run_id === scanRun?.id,
    ) || [];
  const aiActive =
    !!scanRun?.pipeline &&
    (scanRun.ai?.queued || 0) +
      (scanRun.ai?.running || 0) +
      (scanRun.ai?.waiting || 0) +
      (scanRun.ai?.pending_admission || 0) >
      0;
  const coverage = data?.coverage;
  const disabled =
    !editable ||
    !data?.editable ||
    busy ||
    aiActive ||
    (!!current && active(current)) ||
    (!!scanRun?.current_scope &&
      !scanRun.batch_job_id &&
      ["running", "interrupted", "partial"].includes(scanRun.state));
  return (
    <section className="batch-associations" aria-label="AI 预关联">
      <div className="batch-heading">
        <div>
          <b>AI 预关联</b>
          <p>
            页面并行采集，已采集内容立即交给 AI
            整理文档与讲解。补齐和变更复用已保存页面；全量会重新采集。未打开的弹窗仍可在操作时补查。
          </p>
        </div>
        <div className="batch-actions">
          <label>
            页面采集
            <select
              aria-label="页面采集并发"
              value={scanConcurrency}
              disabled={disabled}
              onChange={(event) =>
                setScanConcurrency(Number(event.target.value))
              }
            >
              {[1, 2, 3, 4].map((value) => (
                <option key={value} value={value}>
                  {value} 路并行
                </option>
              ))}
            </select>
          </label>
          <button disabled={disabled} onClick={() => void run("missing")}>
            <Play size={14} />
            补齐未关联
          </button>
          <button disabled={disabled} onClick={() => void run("changed")}>
            <RefreshCw size={14} />
            分析变更项
          </button>
          <button
            disabled={disabled}
            onClick={() => void run("all")}
            className="primary"
          >
            全量 AI 关联
          </button>
        </div>
      </div>
      {error && (
        <p role="alert" className="batch-error">
          {error}
        </p>
      )}
      {scanSession && (
        <PrototypeInventoryScanner
          project={project}
          accountId={accountId}
          manifest={scanSession.manifest}
          scanRunId={scanSession.run.id}
          clientId={clientId}
          concurrency={
            scanSession.run.concurrency ?? scanSession.manifest.concurrency ?? 3
          }
          incremental={!!scanSession.run.pipeline}
          onPause={() => void pauseScan()}
          onSaved={(receipt: ScanReceipt) => {
            if (receipt.scan_run?.id === scanSession.run.id)
              acceptScan(receipt.scan_run as unknown as ScanRun, true);
          }}
          progressBase={{
            completed: scanSession.run.pages_completed,
            controls: scanSession.run.controls,
            total: scanSession.run.pages_total,
            attempted: scanSession.run.pages_attempted,
            failed: scanSession.run.pages_failed,
          }}
          onCurrent={(value) => {
            scanCurrent.current = value;
          }}
          onComplete={() => {
            scanSession.resolve();
            setScanSession(null);
          }}
          onCancel={() => void stopScan()}
          onError={(error) => {
            scanSession.reject(error);
            setScanSession(null);
          }}
        />
      )}
      {scanRun && !scanSession && (
        <div
          className="batch-current"
          role="status"
          aria-label="已保存的预扫描进度"
        >
          <div>
            <b>
              {scanRun.state === "partial" || scanRun.pages_failed
                ? "预扫描部分完成"
                : scanRun.state === "completed"
                  ? "页面预扫描完成"
                  : scanRun.state === "cancelled"
                    ? "任务已停止"
                    : scanRun.state === "invalidated" || !scanRun.current_scope
                      ? "预扫描版本已变化"
                      : "预扫描进度已保存"}
            </b>
            <span>
              已采集 {scanRun.pages_completed} / {scanRun.pages_total} 页 ·{" "}
              {scanRun.controls} 控件
            </span>
          </div>
          <progress
            max={Math.max(1, scanRun.pages_total)}
            value={scanRun.pages_completed}
          />
          <small>
            已尝试{" "}
            {scanRun.pages_attempted ??
              scanRun.pages_completed + scanRun.pages_failed}{" "}
            页
            {scanRun.pages_failed
              ? ` · 未完成：${scanRun.pages_failed} 页待重试`
              : ""}
          </small>
          {scanRun.current && (
            <small>{scanRun.current.label || scanRun.current.route}</small>
          )}
          {scanRun.current_scope &&
            !scanRun.batch_job_id &&
            (!scanRun.pipeline || scanRun.manifest.pages_total > 0) &&
            ["running", "interrupted", "partial", "completed"].includes(
              scanRun.state,
            ) && (
              <div className="batch-actions">
                <button
                  disabled={busy || !editable || !data}
                  onClick={() => void run(scanRun.mode, undefined, scanRun)}
                >
                  {scanRun.state === "partial" || scanRun.pages_failed
                    ? "重试未完成页面"
                    : scanRun.state === "completed"
                      ? "开始 AI 关联"
                      : "恢复预扫描"}
                </button>
                {!scanRun.pipeline &&
                  (scanRun.state === "partial" || scanRun.pages_failed > 0) &&
                  scanRun.pages_completed > 0 && (
                    <button
                      disabled={busy || !editable || !data}
                      onClick={() =>
                        void run(scanRun.mode, undefined, scanRun, true)
                      }
                    >
                      仅关联已采集内容
                    </button>
                  )}
                <button
                  disabled={busy || !editable}
                  onClick={() => void stopScan()}
                >
                  停止任务
                </button>
                <small>
                  恢复后仅继续未完成页面；已提交的 AI 在后台继续处理。
                </small>
              </div>
            )}
        </div>
      )}
      {scanRun?.pipeline && scanRun.ai && (
        <div
          className="batch-current"
          role="status"
          aria-label="后台 AI 关联进度"
        >
          <div>
            <b>
              {scanRun.ai.pending_admission
                ? "正在整理已采集页面"
                : aiActive
                  ? "后台 AI 正在关联"
                  : scanRun.ai.failed
                    ? "AI 部分内容待重试"
                    : scanRun.state === "running"
                      ? "等待页面内容"
                      : "本次 AI 处理已结束"}
            </b>
            <span>
              已处理 {scanRun.ai.completed} / {scanRun.ai.targets} 项
            </span>
          </div>
          <progress
            max={Math.max(
              1,
              scanRun.ai.targets + (scanRun.ai.pending_admission ? 1 : 0),
            )}
            value={scanRun.ai.completed}
          />
          <small>
            已保存 {scanRun.ai.saved} 项 · 排队 {scanRun.ai.queued} · 处理中{" "}
            {scanRun.ai.running}
            {scanRun.ai.waiting ? ` · 待处理 ${scanRun.ai.waiting}` : ""}
            {scanRun.ai.failed ? ` · 失败 ${scanRun.ai.failed}` : ""}
          </small>
          {scanRun.ai.error && <p>{scanRun.ai.error}</p>}
          <div className="batch-actions">
            {!!scanRun.ai.failed &&
              scanRun.current_scope &&
              !["cancelled", "invalidated"].includes(scanRun.state) && (
                <button
                  disabled={busy || !editable}
                  onClick={() => void retryAI()}
                >
                  重试 AI 未完成项
                </button>
              )}
            {aiActive && !scanSession && (
              <button disabled={!editable} onClick={() => void stopScan()}>
                停止任务
              </button>
            )}
          </div>
          {!!pipelineJobs.length && (
            <details>
              <summary>查看逐页处理结果（{pipelineJobs.length}）</summary>
              <ul>
                {pipelineJobs.map((job) => (
                  <li key={job.id}>
                    <b>
                      {job.state === "succeeded"
                        ? "已处理"
                        : active(job)
                          ? "处理中"
                          : job.state === "cancelled"
                            ? "已停止"
                            : "待重试"}
                    </b>{" "}
                    · 已保存 {job.result?.saved || 0} 项
                    {job.error?.message ? ` · ${job.error.message}` : ""}
                    {!!job.result?.completed && (
                      <details>
                        <summary>查看逐项结果</summary>
                        <ul>
                          {(details?.job === job.id
                            ? details.items
                            : Object.values(job.result.completed)
                          ).map((item, i) => (
                            <li key={item.id || i}>
                              {item.label} · {item.message}
                            </li>
                          ))}
                        </ul>
                        {job.result.details_truncated &&
                          (!details ||
                            details.job !== job.id ||
                            details.hasMore) && (
                            <button
                              disabled={detailsBusy || active(job)}
                              onClick={() => void loadDetails(job)}
                            >
                              加载更多结果
                            </button>
                          )}
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {!scanRun &&
        storedProgress &&
        (storedProgress.pages_captured > 0 ||
          storedProgress.pages_failed > 0) && (
          <div
            className="batch-current"
            role="status"
            aria-label="已保存的页面清单"
          >
            <b>已保存页面清单</b>
            <p>
              已采集 {storedProgress.pages_captured} /{" "}
              {storedProgress.pages_total} 页 · {storedProgress.controls} 控件
              {storedProgress.pages_failed
                ? ` · ${storedProgress.pages_failed} 页待重试`
                : ""}
            </p>
            <small>
              点击“补齐未关联”继续尚未完成的页面，已有清单会复用；不会自动调用模型。
            </small>
          </div>
        )}
      {coverage ? (
        <div className="batch-coverage" aria-label="关联覆盖情况">
          <span>
            已发现 <b>{coverage.discovered}</b>
          </span>
          <span>
            模块 <b>{coverage.modules}</b>
          </span>
          <span>
            控件 <b>{coverage.controls}</b>
          </span>
          <span>
            已关联 <b>{coverage.linked}</b>
          </span>
          <span>
            人工维护 <b>{coverage.manual}</b>
          </span>
          <span>
            已移除 <b>{coverage.excluded}</b>
          </span>
          <span>
            待处理 <b>{coverage.pending}</b>
          </span>
          {!!coverage.duplicate_controls && (
            <span>
              重复行操作 <b>{coverage.duplicate_controls}</b>
            </span>
          )}
        </div>
      ) : (
        <p role="status">
          <LoaderCircle size={14} className="spinning" />
          正在扫描当前资料中的模块与控件…
        </p>
      )}
      {!!coverage?.limitations?.length && (
        <details className="batch-limitations">
          <summary>扫描范围与尚未覆盖的状态</summary>
          <ul>
            {coverage.limitations.map((s, i) => (
              <li key={i}>{typeof s === "string" ? s : s.message}</li>
            ))}
          </ul>
        </details>
      )}
      {current && (
        <div className="batch-current" role="status">
          <div>
            <b>
              {active(current)
                ? current.state === "queued"
                  ? "等待预关联"
                  : current.state === "cancelling"
                    ? "正在停止预关联"
                    : current.stage === "DOCUMENT_INDEX"
                      ? "正在建立文档索引"
                      : "正在预关联"
                : current.state === "succeeded"
                  ? "本次处理完成"
                  : current.state === "cancelled"
                    ? "预关联已停止"
                    : "预关联暂未完成"}
            </b>
            <span>
              {current.result?.processed ?? 0} /{" "}
              {current.result?.total ?? coverage?.discovered ?? 0} 项
            </span>
          </div>
          <progress max={100} value={current.progress} />
          <small>
            已保存 {current.result?.saved ?? 0} 项 ·
            处理完成与有依据的关联覆盖率分别统计
          </small>
          {current.error && <p>{current.error.message}</p>}
          <div className="batch-actions">
            {active(current) ? (
              <button
                disabled={busy || current.state === "cancelling"}
                onClick={() => void stop(current)}
              >
                <Square size={13} />
                停止任务
              </button>
            ) : current.state !== "succeeded" || !!current.result?.pending ? (
              <button
                disabled={disabled}
                onClick={() => void run(current.mode || "missing", current.id)}
              >
                继续 / 重试未完成项
              </button>
            ) : null}
          </div>
          {!!current.result?.completed && (
            <details>
              <summary>查看逐项结果</summary>
              <ul>
                {(details?.job === current.id
                  ? details.items
                  : Object.values(current.result.completed)
                ).map((item, i) => (
                  <li key={item.id || i}>
                    <b>{item.label}</b> · {item.message}
                  </li>
                ))}
              </ul>
              {current.result.details_truncated &&
                (!details || details.job !== current.id || details.hasMore) && (
                  <button
                    disabled={detailsBusy || active(current)}
                    onClick={() => void loadDetails(current)}
                  >
                    {detailsBusy
                      ? "正在读取…"
                      : details?.job === current.id
                        ? "加载更多结果"
                        : "查看全部结果"}
                  </button>
                )}
            </details>
          )}
        </div>
      )}
    </section>
  );
}
