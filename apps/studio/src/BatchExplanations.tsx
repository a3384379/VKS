import { useEffect, useRef, useState } from "react";
import { api, apiResponse, mutation } from "../../web/src/api";
import { TaskTiming, type Timing } from "./TaskTiming";
import type { EstimateRange } from "./taskEstimate";

type Batch = {
  job_id: string;
  state: string;
  processed: number;
  total: number;
  saved: number;
  reused: number;
  failed: number;
  timing: Timing;
  history?: EstimateRange | null;
};
const states: Record<string, string> = {
  queued: "等待运行",
  running: "正在生成",
  retry_wait: "等待重试",
  cancelling: "正在停止",
  succeeded: "批量讲解已完成",
  failed: "批量讲解未完成",
  cancelled: "批量讲解已停止",
};
const active = (job: Batch | null) =>
  job !== null &&
  ["queued", "running", "retry_wait", "cancelling"].includes(job.state);

export function BatchExplanations({
  project,
  analysis,
  version,
  bindingIds,
  blockedReason,
}: {
  project: string;
  analysis: string;
  version: number;
  bindingIds: string[];
  blockedReason: string;
}) {
  const [batch, setBatch] = useState<Batch | null>(null);
  const [reload, setReload] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const path = `/api/v3/projects/${project}/interactive/explanation-batches`;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const response = await apiResponse(
          `${path}/latest?analysis_set_id=${encodeURIComponent(analysis)}`,
          { signal: controller.signal, cache: "no-store" },
        );
        const { data }: { data: Batch | null } = await response.json();
        if (controller.signal.aborted) return;
        setBatch(data);
        if (active(data)) timer = setTimeout(poll, 1500);
      } catch {
        if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, analysis, reload]);

  async function run(action: "start" | "cancel" | "retry") {
    setPending(true);
    setError("");
    try {
      if (action === "start") {
        const created = await api<Batch>(
          path,
          mutation(
            "POST",
            {
              analysis_set_id: analysis,
              binding_ids: bindingIds,
            },
            version,
          ),
        );
        setBatch(created);
      } else if (batch) {
        await api(`/jobs/${batch.job_id}/${action}`, mutation("POST", {}));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(false);
      setReload((value) => value + 1);
    }
  }

  const description =
    blockedReason ||
    (bindingIds.length
      ? `补齐 ${bindingIds.length} 项有效关联的讲解`
      : "请选择有原文引用的已关联项");
  return (
    <>
      <button
        disabled={
          !!blockedReason ||
          bindingIds.length === 0 ||
          bindingIds.length > 10000
        }
        onClick={() => {
          setError("");
          dialog.current?.showModal();
        }}
        title={description}
      >
        批量 AI 讲解
      </button>
      {batch && (
        <button onClick={() => dialog.current?.showModal()}>
          讲解进度{active(batch) ? ` ${batch.processed}/${batch.total}` : ""}
        </button>
      )}
      <dialog
        ref={dialog}
        className="batch-explanation-dialog"
        aria-label="批量 AI 讲解"
      >
        <h2>批量 AI 讲解</h2>
        {batch && (
          <section aria-label="讲解任务进度" role="status">
            <p>
              {states[batch.state] || batch.state} · {batch.processed} /{" "}
              {batch.total} 项
            </p>
            <progress
              aria-label="讲解进度"
              value={batch.processed}
              max={batch.total || 1}
            />
            <TaskTiming
              timing={batch.timing}
              state={batch.state}
              processed={batch.processed}
              total={batch.total}
              history={batch.history}
            />
            <p>
              新生成 {batch.saved} · 已复用 {batch.reused} · 失败 {batch.failed}
            </p>
          </section>
        )}
        {!active(batch) && bindingIds.length > 0 && (
          <p>
            已选 {bindingIds.length}{" "}
            项有效关联。已有讲解直接复用，新讲解保存后随 HTML 成品导出。
          </p>
        )}
        {error && <p role="alert">{error}</p>}
        <footer>
          <button onClick={() => dialog.current?.close()}>关闭</button>
          {active(batch) ? (
            <button
              disabled={pending || batch?.state === "cancelling"}
              onClick={() => void run("cancel")}
            >
              停止讲解
            </button>
          ) : (
            <>
              {batch && ["cancelled", "failed"].includes(batch.state) && (
                <button
                  disabled={pending || !!blockedReason}
                  onClick={() => void run("retry")}
                >
                  继续未完成项
                </button>
              )}
              <button
                className="primary"
                disabled={
                  pending ||
                  !!blockedReason ||
                  !bindingIds.length ||
                  bindingIds.length > 10000
                }
                onClick={() => void run("start")}
              >
                补齐所选讲解
              </button>
            </>
          )}
        </footer>
      </dialog>
    </>
  );
}
