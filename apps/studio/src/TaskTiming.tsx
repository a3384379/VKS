import { useEffect, useState } from "react";
import {
  estimatedSeconds,
  observeProgress,
  remainingRange,
  roundRemaining,
  type EstimateRange,
  type TaskEstimate,
} from "./taskEstimate";

export type Timing = {
  started_at?: string | null;
  updated_at?: string | null;
  baseline?: number;
};

function duration(seconds: number) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours} 小时 ${minutes} 分钟`;
  if (minutes) return `${minutes} 分 ${remainder} 秒`;
  return `${remainder} 秒`;
}

export function TaskTiming({
  timing,
  state,
  processed = 0,
  total = 0,
  estimateSeconds,
  history,
}: {
  timing?: Timing;
  state: string;
  processed?: number;
  total?: number;
  estimateSeconds?: number | null;
  history?: EstimateRange | null;
}) {
  const [now, setNow] = useState(Date.now);
  const [estimate, setEstimate] = useState<TaskEstimate | null>(null);
  const live = state === "running" || state === "cancelling";
  const start = timing?.started_at ? Date.parse(timing.started_at) : NaN;
  const updated = timing?.updated_at ? Date.parse(timing.updated_at) : NaN;
  const baseline = timing?.baseline ?? 0;
  useEffect(() => {
    if (!live || !Number.isFinite(start)) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live, start]);
  useEffect(() => {
    if (state !== "running") return;
    const receivedAt = Date.now();
    setEstimate((previous) => {
      const lastSample =
        previous?.startedAt === start ? previous.samples.at(-1)?.at : undefined;
      // Client-side scanning or indexing can report additional results while
      // the associated job's timestamp stays at the previous checkpoint.
      const at =
        Number.isFinite(updated) && updated > (lastSample ?? start)
          ? updated
          : receivedAt;
      return observeProgress(previous, {
        startedAt: start,
        baseline,
        processed,
        total,
        at,
      });
    });
  }, [state, start, updated, baseline, processed, total]);
  const end = live ? now : updated;
  const elapsed =
    Number.isFinite(start) && Number.isFinite(end)
      ? Math.max(0, Math.floor((end - start) / 1000))
      : null;
  const current =
    estimate?.startedAt === start &&
    estimate.baseline === baseline &&
    estimate.total === total
      ? estimate
      : null;
  const measured = estimatedSeconds(current, now);
  const forecast = history
    ? remainingRange(
        history,
        state === "queued" ? 0 : (elapsed ?? 0),
        measured,
        current?.calibrations ?? 0,
      )
    : null;
  const historical =
    forecast && forecast.upper > 0 && forecast.source !== "measured"
      ? formatRange(forecast.lower, forecast.upper)
      : null;
  let remaining = "—";
  if (state === "succeeded" || state === "completed") remaining = "0 秒";
  else if (state === "queued")
    remaining = historical ? `${historical}（开始后，参考历史）` : "等待调度";
  else if (state === "retry_wait") remaining = "等待重试";
  else if (state === "running") {
    if (total > 0 && processed >= total) {
      remaining = "正在收尾";
    } else if (
      historical &&
      (measured !== null || current?.finishAt === undefined)
    ) {
      remaining = `${historical}${measured === null ? "（参考历史）" : "（结合本轮）"}`;
    } else if (measured !== null) {
      remaining = `约 ${duration(roundRemaining(measured))}`;
    } else if (current?.finishAt !== undefined) {
      remaining = "等待新结果后更新";
    } else if (history) {
      remaining = "已超过历史参考范围，等待更多结果";
    } else if (estimateSeconds && elapsed !== null) {
      const seconds = estimateSeconds - elapsed;
      remaining =
        seconds > 0
          ? `约 ${duration(roundRemaining(seconds))}（参考历史）`
          : "已超过近期用时，仍在处理";
    } else if (total > 0 && processed <= baseline) {
      remaining = "等待首项结果";
    } else if (total > 1 && elapsed !== null) {
      remaining = "等待更多任务结果";
    } else {
      remaining = "暂无可用进度";
    }
  }
  return (
    <small className="task-timing" aria-live="off">
      已运行 {elapsed === null ? "—" : duration(elapsed)} · 预计剩余 {remaining}
    </small>
  );
}

function formatRange(lower: number, upper: number) {
  const from = roundRemaining(lower, true),
    to = roundRemaining(upper);
  if (!from) return `约 ${duration(to)}以内`;
  if (from === to) return `约 ${duration(to)}`;
  return `约 ${duration(from)}～${duration(to)}`;
}
