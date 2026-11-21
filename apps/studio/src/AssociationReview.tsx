import { useEffect, useMemo, useRef, useState } from "react";
import { api, mutation } from "../../web/src/api";
import { RichMarkdown, type MarkdownReference } from "./RichMarkdown";
import { TaskTiming, type Timing } from "./TaskTiming";
import {
  AssociationComparison,
  type AssociationBaseline,
} from "./AssociationComparison";
import "./association-review.css";

export type ReviewTarget = {
  package_id: string;
  target: {
    route: string;
    frame_path: string[];
    selector: string;
    label: string;
    state?: string;
    open_steps?: string[];
  };
  context: {
    scope?: "control" | "module";
    page_title: string;
    section_title?: string;
    nearby_labels?: string[];
  };
  binding_id?: string;
  binding_version?: number;
  point_revision_id?: string;
  title?: string;
  additional_section_revision_ids?: string[];
};
export type AssociationPreview = {
  title: string;
  explanation: string;
  sections: MarkdownReference[];
  unknowns: string[];
  confidence: number;
  preview_id?: string | null;
  can_confirm?: boolean;
  saved: boolean;
  binding_id?: string | null;
  selection_version: number;
  baseline?: AssociationBaseline | null;
};
export type AssociationReviewProps = {
  project: string;
  analysis: string;
  items?: ReviewTarget[];
  mode?: "missing" | "all";
  resumeJobId?: string;
  blockedReason?: string;
  addedChapter?: { name: string; document: string };
  onClose: () => void;
  onSaved: () => unknown | Promise<unknown>;
};
type ReviewRow = {
  item_id: string;
  label: string;
  module_label?: string;
  status: "pending" | "generated" | "insufficient" | "failed";
  preview_id?: string | null;
  display_id?: string;
  can_confirm?: boolean;
  error?: string | { message?: string };
  request?: Record<string, unknown>;
};
type ReviewPage = {
  job_id: string;
  state: string;
  processed: number;
  total: number;
  timing?: Timing;
  failure_message?: string;
  matched_total: number;
  unresolved_total: number;
  items: ReviewRow[];
  next_offset?: number | null;
};
type ReviewFilter = "all" | "needs_attention" | ReviewRow["status"];
type PreviewEdit = { title: string; summary: string };
type ReviewTiming = {
  single_seconds: number | null;
  batch_seconds_per_item: number | null;
};
const reviewFilters: Record<ReviewFilter, string> = {
  all: "全部状态",
  needs_attention: "需处理",
  failed: "生成失败",
  insufficient: "依据不足",
  generated: "已生成预览",
  pending: "待生成",
};
const settled = (state: string) =>
  ["succeeded", "failed", "cancelled"].includes(state);
const jobStatus: Record<string, string> = {
  queued: "等待调度",
  running: "正在生成预览",
  retry_wait: "等待重试",
  cancelling: "正在停止生成",
  cancelled: "生成已停止",
  failed: "生成失败",
  succeeded: "预览生成结束",
};
const rowError = (row: ReviewRow) =>
  typeof row.error === "string" ? row.error : row.error?.message;
const statusText = (row: ReviewRow) => reviewFilters[row.status];
const pageSize = 25;

export function AssociationReview({
  project,
  analysis,
  items,
  mode = "missing",
  resumeJobId,
  blockedReason,
  addedChapter,
  onClose,
  onSaved,
}: AssociationReviewProps) {
  const base = `/api/v3/projects/${project}/interactive/reassociate`;
  const single = items?.length === 1;
  const dialog = useRef<HTMLDialogElement>(null);
  const sourceDialog = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState<MarkdownReference | null>(null);
  const active = useRef(true);
  const controllers = useRef(new Set<AbortController>());
  const currentJob = useRef<{ id: string; state: string } | null>(null);
  const [historyTiming, setHistoryTiming] = useState<ReviewTiming | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [instruction, setInstruction] = useState("");
  const [includeControls, setIncludeControls] = useState(false);
  const [job, setJob] = useState<{
    id: string;
    state: string;
    processed: number;
    total: number;
    timing?: Timing;
  } | null>(() =>
    resumeJobId ? { id: resumeJobId, state: "queued", processed: 0, total: 0 } : null,
  );
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [overrides, setOverrides] = useState<Record<string, ReviewRow>>({});
  const [excluded, setExcluded] = useState(new Set<string>());
  const [directions, setDirections] = useState<Record<string, string>>({});
  const [keyword, setKeyword] = useState("");
  const [statusFilter, setStatusFilter] = useState<ReviewFilter>("all");
  const [matched, setMatched] = useState(0);
  const [unresolved, setUnresolved] = useState(0);
  const [listLoading, setListLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const [preview, setPreview] = useState<{
    id: string;
    value: AssociationPreview;
  } | null>(null);
  const localPreviews = useRef(new Map<string, AssociationPreview>());
  const [generating, setGenerating] = useState(false);
  const [generationStarted, setGenerationStarted] = useState("");
  const retrying = useRef(new Map<string, string>());
  const [regenerating, setRegenerating] = useState(new Set<string>());
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({});
  const [retryNotes, setRetryNotes] = useState<Record<string, string>>({});
  const [batchDirection, setBatchDirection] = useState("");
  const [batchRetry, setBatchRetry] = useState<{
    done: number; total: number; ready: number; running: boolean; startedAt: string;
  }>();
  const [edits, setEdits] = useState<Record<string, PreviewEdit>>({});
  const [editing, setEditing] = useState<{ id: string; point: PreviewEdit } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  const [started, setStarted] = useState(!!resumeJobId);
  const visible = rows
    .map((row) => overrides[row.item_id] || row)
    .filter((row) => !excluded.has(row.item_id));
  const current = visible.find((row) => row.item_id === selected) || visible[0];
  const currentContext = current?.request?.context as
    | ReviewTarget["context"]
    | undefined;
  const currentRow = useRef<string | undefined>(undefined);
  currentRow.current = current?.item_id;
  const currentPreview = current?.preview_id || current?.display_id;
  const value = preview && preview.id === currentPreview ? preview.value : null;
  const currentEdit = current?.preview_id ? edits[current.preview_id] : undefined;
  const activeEditor = editing?.id === currentPreview ? editing : null;
  const total = job?.total ?? rows.length;
  const remaining = Math.max(0, total - excluded.size);
  const localFilters = useMemo(
    () => ({
      excluded_item_ids: [...excluded],
      insufficient_item_ids: Object.values(overrides)
        .filter((row) => !row.can_confirm)
        .map((row) => row.item_id),
    }),
    [excluded, overrides],
  );
  const currentRetrying = !!current && regenerating.has(current.item_id);
  const currentRetryStarted = current ? retrying.current.get(current.item_id) : null;
  const showUnresolved = !!job && settled(job.state) && unresolved > 0;
  const singleEstimate = historyTiming?.single_seconds;
  const batchEstimate = historyTiming?.batch_seconds_per_item && job && job.total > 1
    ? Math.max(singleEstimate || 0, historyTiming.batch_seconds_per_item * (job.total - (job.timing?.baseline ?? 0)))
    : null;
  const batchHistory = batchEstimate ? {
    lower_seconds: batchEstimate * 0.7,
    upper_seconds: batchEstimate * 1.5,
  } : undefined;

  async function request<T>(path: string, init: RequestInit = {}) {
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      return await api<T>(path, { ...init, signal: controller.signal });
    } finally {
      controllers.current.delete(controller);
    }
  }
  function body(item: ReviewTarget) {
    const { title: _title, ...target } = item;
    return {
      ...target,
      analysis_set_id: analysis,
      mode: "explain",
      refresh: true,
    };
  }
  useEffect(() => {
    active.current = true;
    const node = dialog.current;
    node?.showModal();
    node?.querySelector("textarea")?.focus({ preventScroll: true });
    return () => {
      active.current = false;
      controllers.current.forEach((controller) => controller.abort());
      controllers.current.clear();
      if (currentJob.current && !settled(currentJob.current.state))
        void api(
          `/jobs/${currentJob.current.id}/cancel`,
          mutation("POST", {}),
        ).catch(() => {});
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void api<ReviewTiming>(`${base}/timing`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setHistoryTiming(value);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [base]);
  useEffect(() => {
    if (!job?.id) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await api<ReviewPage>(
          `${base}/batch-preview/${job!.id}/results`,
          {
            ...mutation("POST", {
              ...localFilters,
              offset,
              limit: pageSize,
              q: keyword,
              status: statusFilter,
            }),
            signal: controller.signal,
          },
        );
        if (controller.signal.aborted || !active.current) return;
        currentJob.current = { id: result.job_id, state: result.state };
        setJob({
          id: result.job_id,
          state: result.state,
          processed: result.processed,
          total: result.total,
          timing: result.timing,
        });
        setMatched(result.matched_total);
        setUnresolved(result.unresolved_total);
        if (result.failure_message) setError(result.failure_message);
        if (offset && offset >= result.matched_total) {
          setOffset(
            Math.max(0, Math.ceil(result.matched_total / pageSize) - 1) *
              pageSize,
          );
          setSelected("");
          return;
        }
        setRows(result.items);
        setListLoading(false);
        if (!settled(result.state)) timer = setTimeout(poll, 1000);
      } catch (cause) {
        if (!controller.signal.aborted && active.current) {
          setError((cause as Error).message);
          setListLoading(false);
        }
      }
    }
    timer = setTimeout(poll, keyword.trim() ? 180 : 0);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [job?.id, offset, base, keyword, statusFilter, localFilters]);
  useEffect(() => {
    if (!currentPreview) {
      setPreview(null);
      setReading(false);
      return;
    }
    if (preview?.id === currentPreview) return;
    const local = localPreviews.current.get(currentPreview);
    if (local) {
      setPreview({ id: currentPreview, value: local });
      setReading(false);
      return;
    }
    let cancelled = false;
    setReading(true);
    void request<AssociationPreview>(`${base}/previews/${currentPreview}`)
      .then((value) => {
        if (!cancelled && active.current)
          setPreview({ id: currentPreview, value });
      })
      .catch((cause) => {
        if (!cancelled && active.current) setError(cause.message);
      })
      .finally(() => {
        if (!cancelled && active.current) setReading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [currentPreview, base]);

  useEffect(() => {
    if (source && !sourceDialog.current?.open)
      sourceDialog.current?.showModal();
  }, [source]);

  async function generate() {
    if (generating || saving || blockedReason) return;
    setGenerating(true);
    setGenerationStarted(new Date().toISOString());
    setError("");
    try {
      if (single) {
        const result = await request<AssociationPreview>(
          base,
          mutation("POST", { ...body(items![0]), instruction }),
        );
        if (!active.current) return;
        if (!result.preview_id)
          localPreviews.current.set("local-single", result);
        setRows([
          {
            item_id: "single",
            label: items![0].title || items![0].target.label,
            module_label: items![0].context.page_title,
            status: result.can_confirm ? "generated" : "insufficient",
            preview_id: result.preview_id,
            display_id: result.preview_id || "local-single",
            can_confirm: result.can_confirm,
            request: body(items![0]),
          },
        ]);
        setSelected("single");
        setPreview({ id: result.preview_id || "local-single", value: result });
      } else {
        const result = await request<{ job_id: string; total: number }>(
          `${base}/batch-preview`,
          mutation("POST", {
            analysis_set_id: analysis,
            instruction,
            mode: items ? "all" : mode,
            module_only: !includeControls,
            ...(items ? { items: items.map(body) } : {}),
          }),
        );
        currentJob.current = { id: result.job_id, state: "queued" };
        if (!active.current) {
          void api(`/jobs/${result.job_id}/cancel`, mutation("POST", {})).catch(
            () => {},
          );
          return;
        }
        setJob({
          id: result.job_id,
          total: result.total,
          processed: 0,
          state: "queued",
        });
      }
      setStarted(true);
    } catch (cause) {
      if (active.current) setError((cause as Error).message);
    } finally {
      if (active.current) setGenerating(false);
    }
  }
  async function regenerate(row: ReviewRow, sharedDirection?: string) {
    if (blockedReason) return;
    if (
      retrying.current.has(row.item_id) ||
      saving ||
      saved ||
      (row.status === "pending" && (!job || !settled(job.state)))
    )
      return;
    retrying.current.set(row.item_id, new Date().toISOString());
    setRegenerating(new Set(retrying.current.keys()));
    setRetryErrors((previous) => ({ ...previous, [row.item_id]: "" }));
    setRetryNotes((previous) => ({ ...previous, [row.item_id]: "" }));
    try {
      const original =
        row.request ||
        (items?.[Number(row.item_id)]
          ? body(items[Number(row.item_id)])
          : null);
      if (!original)
        throw new Error("当前项目缺少准确目标信息，请关闭预览后重新选择。");
      const result = await request<AssociationPreview>(
        base,
        mutation("POST", {
          ...original,
          instruction: sharedDirection ?? directions[row.item_id] ?? instruction,
          ...(row.preview_id ? { base_preview_id: row.preview_id } : {}),
          ...(job ? { batch_id: job.id, item_id: row.item_id } : {}),
        }),
      );
      if (!active.current) return;
      if (
        currentRow.current === row.item_id && value &&
        value.title === result.title && value.explanation === result.explanation
      )
        setRetryNotes((previous) => ({
          ...previous,
          [row.item_id]: "本次重新生成已完成，模型返回了与上一版相同的标题和正文。",
        }));
      if (!result.preview_id)
        localPreviews.current.set(`local-${row.item_id}`, result);
      const updated: ReviewRow = {
        ...row,
        status: result.can_confirm ? "generated" : "insufficient",
        preview_id: result.preview_id,
        display_id: result.preview_id || `local-${row.item_id}`,
        can_confirm: result.can_confirm,
        error: undefined,
      };
      setOverrides((previous) => ({ ...previous, [row.item_id]: updated }));
      if (currentRow.current === row.item_id)
        setPreview({
          id: result.preview_id || `local-${row.item_id}`,
          value: result,
        });
      return Boolean(result.can_confirm);
    } catch (cause) {
      if (active.current)
        setRetryErrors((previous) => ({
          ...previous,
          [row.item_id]: (cause as Error).message,
        }));
    } finally {
      retrying.current.delete(row.item_id);
      if (active.current) setRegenerating(new Set(retrying.current.keys()));
    }
  }
  async function retryUnfinished() {
    if (!job || !settled(job.state) || batchRetry?.running || retrying.current.size || blockedReason) return;
    const startedAt = new Date().toISOString();
    setBatchRetry({ done: 0, total: unresolved, ready: 0, running: true, startedAt });
    setError("");
    try {
      const targets: ReviewRow[] = [];
      let next: number | null = 0;
      while (next !== null) {
        const result: ReviewPage = await request(
          `${base}/batch-preview/${job.id}/results`,
          mutation("POST", { ...localFilters, status: "needs_attention", offset: next, limit: 100 }),
        );
        targets.push(...result.items);
        next = result.next_offset ?? null;
      }
      let done = 0;
      let ready = 0;
      const queue = targets.values();
      setBatchRetry({ done, total: targets.length, ready, running: true, startedAt });
      await Promise.all(Array.from({ length: Math.min(4, targets.length) }, async () => {
        for (const row of queue) {
          if (!active.current) return;
          if (await regenerate(row, batchDirection.trim() || undefined)) ready++;
          done++;
          if (active.current) setBatchRetry({ done, total: targets.length, ready, running: true, startedAt });
        }
      }));
      if (active.current) setBatchRetry({ done, total: targets.length, ready, running: false, startedAt });
    } catch (cause) {
      if (active.current) {
        setError((cause as Error).message);
        setBatchRetry(undefined);
      }
    }
  }
  function moveToPage(next: number) {
    setOffset(next);
    setSelected("");
    setRows([]);
    setListLoading(true);
  }
  function filterByStatus(status: ReviewFilter) {
    if (status === statusFilter) return;
    setStatusFilter(status);
    moveToPage(0);
  }
  function showNeedsAttention() {
    if (!keyword && statusFilter === "needs_attention" && !offset) return;
    setKeyword("");
    setStatusFilter("needs_attention");
    moveToPage(0);
  }
  async function confirm() {
    if (saving || generating || retrying.current.size || editing || batchRetry?.running || !remaining || blockedReason) return;
    setSaving(true);
    setError("");
    try {
      if (single) {
        if (!current?.preview_id || !current.can_confirm)
          throw new Error("本项依据不足，暂不能保存。");
        const result = await request<{ saved: boolean }>(
          `${base}/confirm`,
          mutation("POST", { preview_id: current.preview_id, ...(currentEdit && { edit: currentEdit }) }),
        );
        if (!result.saved)
          throw new Error("服务器尚未确认保存，原关联保持不变。");
      } else {
        if (!job || !settled(job.state))
          throw new Error("请等待本次预览生成完成。");
        const candidates: ReviewRow[] = [];
        let next: number | null = 0;
        while (next !== null) {
          const result: ReviewPage = await request(
            `${base}/batch-preview/${job.id}/results`,
            mutation("POST", { ...localFilters, offset: next, limit: 100 }),
          );
          candidates.push(...result.items);
          next = result.next_offset ?? null;
        }
        if (!candidates.length) throw new Error("本次没有待保存的预览。");
        const invalid = candidates.filter(
          (row) => !row.can_confirm || !row.preview_id,
        );
        if (invalid.length) {
          setUnresolved(invalid.length);
          showNeedsAttention();
          throw new Error(
            `还有 ${invalid.length} 项未形成可保存预览，请重试或从本次移除。`,
          );
        }
        const result = await request<{ saved: boolean; count: number }>(
          `${base}/batch-confirm`,
          mutation("POST", {
            job_id: job.id,
            preview_ids: candidates.map((row) => row.preview_id),
            ...(Object.keys(edits).length && {
              edits: candidates.flatMap(({ preview_id }) =>
                preview_id && edits[preview_id] ? [{ preview_id, point: edits[preview_id] }] : [],
              ),
            }),
          }),
        );
        if (!result.saved || result.count !== candidates.length)
          throw new Error("服务器未确认整批保存，请保留列表并核对后重试。");
      }
      if (!active.current) return;
      setSaved(true);
      try {
        await onSaved();
      } catch {
        if (active.current)
          setError("关联已保存，列表刷新失败，可关闭后刷新页面查看。");
      }
    } catch (cause) {
      if (active.current) setError((cause as Error).message);
    } finally {
      if (active.current) setSaving(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className={`association-review ${single ? "single" : ""}`}
      aria-label="AI 关联预览"
      onCancel={(event) => {
        event.preventDefault();
        if (!saving) onCloseRef.current();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") event.stopPropagation();
      }}
    >
      <header className="association-review-head">
        <div>
          <h2>{single ? "AI 关联预览" : "批量 AI 关联预览"}</h2>
          <p>先查看生成内容，确认后再替换关联。取消或关闭保留原关联。</p>
          {blockedReason && <p role="status">{blockedReason}</p>}
        </div>
        <button
          type="button"
          aria-label="关闭关联预览"
          disabled={saving}
          onClick={onClose}
        >
          ×
        </button>
      </header>
      {error && (
        <p className="association-review-error" role="alert">
          {error}
        </p>
      )}
      {!started ? (
        <form
          className="association-review-prompt"
          onSubmit={(event) => {
            event.preventDefault();
            void generate();
          }}
        >
          <p>
            {items
              ? `已选择 ${items.length} 个模块或操作`
              : mode === "all"
                ? "范围：当前清单中的全部模块"
                : "范围：当前清单中的未关联模块"}
          </p>
          {single && (
            <p>
              <b>目标模块或操作：</b>
              {items![0].context.page_title} · {items![0].target.label}
            </p>
          )}
          {addedChapter && (
            <div className="association-added-chapter">
              <b>新增关联章节</b>
              <p>
                {addedChapter.document} / {addedChapter.name}
              </p>
              <small>
                结合原关联与新增章节补全说明，生成后对比，确认后一起保存。
              </small>
            </div>
          )}
          {!items && (
            <label className="association-review-option">
              <input
                type="checkbox"
                checked={includeControls}
                onChange={(event) => setIncludeControls(event.target.checked)}
              />
              包含页面控件
            </label>
          )}
          <label>
            {single ? "关联方向" : "统一关联方向"}
            <textarea
              autoFocus
              aria-label={single ? "关联方向" : "统一关联方向"}
              maxLength={4000}
              rows={4}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="可填写关注的业务规则、章节或关联方向，留空按当前目标生成。"
            />
          </label>
          <p className="muted">生成仅形成预览，不会替换已有结果。</p>
          <button className="primary" disabled={generating || !!blockedReason}>
            {generating ? "正在生成预览…" : "生成关联预览"}
          </button>
          {generating && (
            <TaskTiming
              timing={{ started_at: generationStarted }}
              state={single ? "running" : "queued"}
              total={single ? 1 : 0}
              estimateSeconds={single ? singleEstimate : undefined}
            />
          )}
        </form>
      ) : (
        <>
          {job && (
            <div className="association-review-progress">
              <span role="status">
                已处理 {job.processed} / {job.total} 项
                {" · "}{jobStatus[job.state] || "等待生成"}
              </span>
              <TaskTiming
                timing={job.timing}
                state={job.state}
                processed={job.processed}
                total={job.total}
                estimateSeconds={job.total === 1 ? singleEstimate : undefined}
                history={batchHistory}
              />
              {showUnresolved && (
                <button type="button" onClick={showNeedsAttention}>
                  查看 {unresolved} 项需处理
                </button>
              )}
            </div>
          )}
          {job && settled(job.state) && (showUnresolved || batchRetry) && (
            <div className="association-review-progress" role="group" aria-label="批量重试">
              <input
                aria-label="统一重试方向"
                placeholder="统一重试方向（可选）"
                value={batchDirection}
                maxLength={4000}
                disabled={batchRetry?.running || saving || saved}
                onChange={(event) => setBatchDirection(event.target.value)}
              />
              <button
                type="button"
                disabled={!showUnresolved || !!batchRetry?.running || !!regenerating.size || saving || saved || !!blockedReason}
                onClick={() => void retryUnfinished()}
              >
                批量重试未完成项
              </button>
              {batchRetry && (
                <span role="status">
                  已重试 {batchRetry.done} / {batchRetry.total} 项 · {batchRetry.ready} 项可保存
                </span>
              )}
              {batchRetry?.running && (
                <TaskTiming
                  timing={{ started_at: batchRetry.startedAt }}
                  state="running"
                  processed={batchRetry.done}
                  total={batchRetry.total}
                  estimateSeconds={batchRetry.total === 1 ? singleEstimate : undefined}
                />
              )}
            </div>
          )}
          <div className="association-review-workspace">
            {!single && (
              <aside
                className="association-review-list"
                aria-label="本次关联预览列表"
              >
                <p>
                  本次保留 {remaining} 项
                  {excluded.size ? ` · 已从本次移除 ${excluded.size} 项` : ""}
                  {(keyword || statusFilter !== "all") &&
                    ` · 筛选结果 ${matched} 项`}
                </p>
                <div
                  className="association-review-filters"
                  role="group"
                  aria-label="筛选本次关联预览"
                >
                  <input
                    type="search"
                    aria-label="搜索关联预览"
                    placeholder="搜索模块、页面或控件"
                    disabled={!!editing}
                    maxLength={200}
                    value={keyword}
                    onChange={(event) => {
                      setKeyword(event.target.value);
                      moveToPage(0);
                    }}
                  />
                  <select
                    aria-label="预览状态"
                    disabled={!!editing}
                    value={statusFilter}
                    onChange={(event) =>
                      filterByStatus(event.target.value as ReviewFilter)
                    }
                  >
                    {Object.entries(reviewFilters).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <ul>
                  {visible.map((row) => (
                    <li key={row.item_id}>
                      <button
                        className={
                          current?.item_id === row.item_id ? "selected" : ""
                        }
                        aria-label={`查看关联预览：${row.label}`}
                        aria-pressed={current?.item_id === row.item_id}
                        disabled={!!editing && current?.item_id !== row.item_id}
                        onClick={() => setSelected(row.item_id)}
                      >
                        <strong>{row.label}</strong>
                        <small>{row.module_label}</small>
                        <span>
                          {regenerating.has(row.item_id)
                            ? "正在重新生成…"
                            : statusText(row)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
                {!visible.length && (
                  <p>
                    {listLoading || (job && !settled(job.state))
                      ? "正在查找预览…"
                      : "没有符合条件的预览。"}
                  </p>
                )}
                {matched > pageSize && (
                  <nav aria-label="预览列表分页">
                    <button
                      disabled={listLoading || !!editing || !offset}
                      onClick={() => moveToPage(Math.max(0, offset - pageSize))}
                    >
                      上一页
                    </button>
                    <span>
                      {Math.floor(offset / pageSize) + 1} /{" "}
                      {Math.ceil(matched / pageSize)}
                    </span>
                    <button
                      disabled={listLoading || !!editing || offset + pageSize >= matched}
                      onClick={() => moveToPage(offset + pageSize)}
                    >
                      下一页
                    </button>
                  </nav>
                )}
              </aside>
            )}
            <article
              className="association-review-detail"
              aria-label="本项完整关联预览"
            >
              {current ? (
                <>
                  <header>
                    <h3>{current.label}</h3>
                    <p>
                      {currentContext?.scope === "module" ? "模块" : "控件"} ·
                      页面：{currentContext?.page_title || current.module_label}
                      {currentContext?.section_title
                        ? ` · 区域：${currentContext.section_title}`
                        : ""}
                    </p>
                    <div className="association-review-item-actions">
                      {!single && (
                        <button
                          disabled={saved || saving || currentRetrying || !!editing || !!batchRetry?.running}
                          onClick={() =>
                            setExcluded(
                              (previous) =>
                                new Set([...previous, current.item_id]),
                            )
                          }
                        >
                          从本次移除
                        </button>
                      )}
                    </div>
                  </header>
                  {currentRetryStarted && !batchRetry?.running && (
                    <TaskTiming
                      timing={{ started_at: currentRetryStarted }}
                      state="running"
                      total={1}
                      estimateSeconds={singleEstimate}
                    />
                  )}
                  {(retryErrors[current.item_id] || rowError(current)) && (
                    <p role="alert">
                      {retryErrors[current.item_id] || rowError(current)}
                    </p>
                  )}
                  {retryNotes[current.item_id] && <p role="status">{retryNotes[current.item_id]}</p>}
                  {reading && !value && <p role="status">正在读取完整预览…</p>}
                  {value ? (
                    <>
                      {(currentEdit?.title ?? value.title) !== current.label && (
                        <h3>{currentEdit?.title ?? value.title}</h3>
                      )}
                      {value.can_confirm && current.preview_id && !saved && (
                        <div className="association-review-item-actions">
                          <button
                            type="button"
                            disabled={Boolean(editing) || saving || currentRetrying || !!blockedReason}
                            onClick={() => setEditing({
                              id: current.preview_id!,
                              point: currentEdit || { title: value.title, summary: value.explanation },
                            })}
                          >
                            编辑预览
                          </button>
                          {currentEdit && (
                            <button type="button" disabled={Boolean(editing) || saving} onClick={() => {
                              setEdits(({ [current.preview_id!]: _removed, ...rest }) => rest);
                            }}>撤销编辑</button>
                          )}
                        </div>
                      )}
                      {activeEditor ? (
                        <form className="association-review-item-direction" onSubmit={(event) => {
                          event.preventDefault();
                          setEdits((previous) => ({ ...previous, [activeEditor.id]: activeEditor.point }));
                          setEditing(null);
                        }}>
                          <label>
                            关联标题
                            <input aria-label="关联标题" required maxLength={160} value={activeEditor.point.title} onChange={(event) =>
                              setEditing({ ...activeEditor, point: { ...activeEditor.point, title: event.target.value } })
                            } />
                          </label>
                          <label>
                            Markdown 正文
                            <textarea aria-label="Markdown 正文" required rows={15} maxLength={512000} value={activeEditor.point.summary} onChange={(event) =>
                              setEditing({ ...activeEditor, point: { ...activeEditor.point, summary: event.target.value } })
                            } />
                          </label>
                          <div className="association-review-item-actions">
                            <button disabled={!activeEditor.point.title.trim() || !activeEditor.point.summary.trim()}>应用到对比</button>
                            <button type="button" onClick={() => setEditing(null)}>取消编辑</button>
                          </div>
                        </form>
                      ) : (
                        <AssociationComparison
                          key={currentPreview}
                          title={currentEdit?.title ?? value.title}
                          explanation={currentEdit?.summary ?? value.explanation}
                          sections={value.sections}
                          baseline={value.baseline}
                          onReference={setSource}
                        />
                      )}
                      {!!value.sections?.length && (
                        <section
                          className="association-review-sources"
                          aria-label="预览引用原文"
                        >
                          <h4>准确引用原文 · {value.sections.length} 章</h4>
                          {value.sections.map((reference, index) => (
                            <button
                              key={reference.revision_id}
                              type="button"
                              aria-label={`查看预览引用原文 ${index + 1}`}
                              onClick={() => setSource(reference)}
                            >
                              <strong>{reference.document_name}</strong>
                              <span>{reference.name}</span>
                            </button>
                          ))}
                        </section>
                      )}
                      {!!value.unknowns?.length && (
                        <p>待确认：{value.unknowns.join("；")}</p>
                      )}
                      {!value.can_confirm && (
                        <p role="status">
                          依据不足或当前范围不可保存，原关联保持不变。
                        </p>
                      )}
                    </>
                  ) : (
                    !reading && <p>{statusText(current)}</p>
                  )}
                  <form
                    className="association-review-item-direction"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void regenerate(current);
                    }}
                  >
                    <label>
                      本项关联方向
                      <textarea
                        aria-label="本项关联方向"
                        rows={2}
                        maxLength={4000}
                        disabled={currentRetrying || saved || saving}
                        value={directions[current.item_id] ?? instruction}
                        onChange={(event) =>
                          setDirections((previous) => ({
                            ...previous,
                            [current.item_id]: event.target.value,
                          }))
                        }
                      />
                    </label>
                    <p className="muted">
                      基于当前预览继续修订，保留有依据的原内容并补充新要求；确认前不会替换已保存关联。
                    </p>
                    <button
                      disabled={
                        saved ||
                        saving ||
                        !!editing ||
                        !!currentEdit ||
                        !!batchRetry?.running ||
                        !!blockedReason ||
                        currentRetrying ||
                        (current.status === "pending" &&
                          (!job || !settled(job.state)))
                      }
                    >
                      {currentRetrying
                        ? "正在重新生成…"
                        : current.status === "failed" ||
                            current.status === "pending"
                          ? "重试本项"
                          : "按本项方向重新生成"}
                    </button>
                  </form>
                </>
              ) : (
                <p>选择列表中的项目查看完整预览。</p>
              )}
            </article>
          </div>
        </>
      )}
      <footer className="association-review-footer">
        <span role="status">{saved ? "关联已保存" : "预览尚未保存"}</span>
        <button disabled={saving} onClick={onClose}>
          {saved ? "关闭" : "取消"}
        </button>
        {started && (
          <button
            className="primary"
            disabled={
              saved ||
              saving ||
              !!blockedReason ||
              generating ||
              !!regenerating.size ||
              !!editing ||
              !!batchRetry?.running ||
              showUnresolved ||
              !remaining ||
              (single ? !current?.can_confirm : !job || !settled(job.state))
            }
            onClick={() => void confirm()}
          >
            {saving ? "正在保存…" : single ? "确认保存关联" : "整批保存"}
          </button>
        )}
      </footer>
      <dialog
        ref={sourceDialog}
        className="association-review-source"
        aria-label="关联预览引用原文"
        onCancel={(event) => {
          event.preventDefault();
          event.stopPropagation();
          sourceDialog.current?.close();
          setSource(null);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") event.stopPropagation();
        }}
      >
        {source && (
          <>
            <header>
              <div>
                <h3>{source.document_name}</h3>
                <p>{source.name}</p>
              </div>
              <button
                type="button"
                aria-label="关闭预览引用原文"
                onClick={() => {
                  sourceDialog.current?.close();
                  setSource(null);
                }}
              >
                关闭
              </button>
            </header>
            <div className="association-review-source-content">
              <RichMarkdown>{source.content}</RichMarkdown>
            </div>
          </>
        )}
      </dialog>
    </dialog>
  );
}
