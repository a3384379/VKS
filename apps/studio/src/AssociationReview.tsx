import { useEffect, useRef, useState } from "react";
import { api, mutation } from "../../web/src/api";
import { RichMarkdown, type MarkdownReference } from "./RichMarkdown";
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
  items: ReviewRow[];
  next_offset?: number | null;
};
const settled = (state: string) =>
  ["succeeded", "failed", "cancelled"].includes(state);
const rowError = (row: ReviewRow) =>
  typeof row.error === "string" ? row.error : row.error?.message;
const statusText = (row: ReviewRow) =>
  ({
    pending: "待生成",
    generated: "已生成预览",
    insufficient: "依据不足",
    failed: "生成失败",
  })[row.status];
const pageSize = 25;

export function AssociationReview({
  project,
  analysis,
  items,
  mode = "missing",
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
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [instruction, setInstruction] = useState("");
  const [includeControls, setIncludeControls] = useState(false);
  const [job, setJob] = useState<{
    id: string;
    state: string;
    processed: number;
    total: number;
  } | null>(null);
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [overrides, setOverrides] = useState<Record<string, ReviewRow>>({});
  const [excluded, setExcluded] = useState(new Set<string>());
  const [directions, setDirections] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const [preview, setPreview] = useState<{
    id: string;
    value: AssociationPreview;
  } | null>(null);
  const localPreviews = useRef(new Map<string, AssociationPreview>());
  const [generating, setGenerating] = useState(false);
  const [regenerating, setRegenerating] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  const [started, setStarted] = useState(false);
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
  const total = job?.total ?? rows.length;
  const remaining = Math.max(0, total - excluded.size);

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
    if (!job?.id) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await request<ReviewPage>(
          `${base}/batch-preview/${job!.id}?offset=${offset}&limit=${pageSize}`,
        );
        if (cancelled || !active.current) return;
        currentJob.current = { id: result.job_id, state: result.state };
        setJob({
          id: result.job_id,
          state: result.state,
          processed: result.processed,
          total: result.total,
        });
        setRows(result.items);
        if (!settled(result.state)) timer = setTimeout(poll, 1000);
      } catch (cause) {
        if (!cancelled && active.current) setError((cause as Error).message);
      }
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [job?.id, offset, base]);
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
    if (generating || saving) return;
    setGenerating(true);
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
  async function regenerate(row: ReviewRow) {
    if (regenerating || saving || saved) return;
    setRegenerating(row.item_id);
    setError("");
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
          instruction: directions[row.item_id] ?? instruction,
          ...(row.preview_id ? { base_preview_id: row.preview_id } : {}),
          ...(job ? { batch_id: job.id, item_id: row.item_id } : {}),
        }),
      );
      if (!active.current) return;
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
    } catch (cause) {
      if (active.current) setError((cause as Error).message);
    } finally {
      if (active.current) setRegenerating("");
    }
  }
  async function confirm() {
    if (saving || generating || regenerating || !remaining) return;
    setSaving(true);
    setError("");
    try {
      if (single) {
        if (!current?.preview_id || !current.can_confirm)
          throw new Error("本项依据不足，暂不能保存。");
        const result = await request<{ saved: boolean }>(
          `${base}/confirm`,
          mutation("POST", { preview_id: current.preview_id }),
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
            `${base}/batch-preview/${job.id}?offset=${next}&limit=50`,
          );
          candidates.push(
            ...result.items
              .map((row) => overrides[row.item_id] || row)
              .filter((row) => !excluded.has(row.item_id)),
          );
          next =
            result.next_offset ??
            (next + result.items.length < result.total && result.items.length
              ? next + result.items.length
              : null);
        }
        if (!candidates.length) throw new Error("本次没有待保存的预览。");
        const invalid = candidates.filter(
          (row) => !row.can_confirm || !row.preview_id,
        );
        if (invalid.length)
          throw new Error(
            `还有 ${invalid.length} 项未形成可保存预览，请重试或从本次移除。`,
          );
        const result = await request<{ saved: boolean; count: number }>(
          `${base}/batch-confirm`,
          mutation("POST", {
            job_id: job.id,
            preview_ids: candidates.map((row) => row.preview_id),
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
          <button className="primary" disabled={generating}>
            {generating ? "正在生成预览…" : "生成关联预览"}
          </button>
        </form>
      ) : (
        <>
          {job && (
            <div className="association-review-progress" role="status">
              已处理 {job.processed} / {job.total} 项
              {!settled(job.state) ? " · 正在生成预览" : " · 预览生成结束"}
            </div>
          )}
          <div className="association-review-workspace">
            {!single && (
              <aside
                className="association-review-list"
                aria-label="本次关联预览列表"
              >
                <p>
                  {remaining} 项待保存
                  {excluded.size ? ` · 已从本次移除 ${excluded.size} 项` : ""}
                </p>
                <ul>
                  {visible.map((row) => (
                    <li key={row.item_id}>
                      <button
                        className={
                          current?.item_id === row.item_id ? "selected" : ""
                        }
                        aria-label={`查看关联预览：${row.label}`}
                        aria-pressed={current?.item_id === row.item_id}
                        onClick={() => setSelected(row.item_id)}
                      >
                        <strong>{row.label}</strong>
                        <small>{row.module_label}</small>
                        <span>{statusText(row)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
                {!visible.length && <p>本页没有待查看项目。</p>}
                {job && job.total > pageSize && (
                  <nav aria-label="预览列表分页">
                    <button
                      disabled={!offset}
                      onClick={() => {
                        setOffset(Math.max(0, offset - pageSize));
                        setSelected("");
                      }}
                    >
                      上一页
                    </button>
                    <span>
                      {Math.floor(offset / pageSize) + 1} /{" "}
                      {Math.ceil(job.total / pageSize)}
                    </span>
                    <button
                      disabled={offset + pageSize >= job.total}
                      onClick={() => {
                        setOffset(offset + pageSize);
                        setSelected("");
                      }}
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
                          disabled={saved || saving || !!regenerating}
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
                  {rowError(current) && <p role="alert">{rowError(current)}</p>}
                  {reading && !value && <p role="status">正在读取完整预览…</p>}
                  {value ? (
                    <>
                      {value.title && value.title !== current.label && (
                        <h3>{value.title}</h3>
                      )}
                      <AssociationComparison
                        key={currentPreview}
                        title={value.title}
                        explanation={value.explanation}
                        sections={value.sections}
                        baseline={value.baseline}
                        onReference={setSource}
                      />
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
                        !!regenerating ||
                        current.status === "pending"
                      }
                    >
                      {regenerating === current.item_id
                        ? "正在重新生成…"
                        : current.status === "failed"
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
              generating ||
              !!regenerating ||
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
