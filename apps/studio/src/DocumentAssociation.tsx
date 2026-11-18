import { useEffect, useRef, useState } from "react";
import { Search, FileText, Layers } from "lucide-react";
import { api } from "../../web/src/api";
import type { ReviewTarget } from "./AssociationReview";
import "./association-review.css";

export type AssociationChapter = {
  revision: string;
  name: string;
  document: string;
};
type ModuleRow = {
  item_id: string;
  state: string;
  label: string;
  module_label: string;
  request: ReviewTarget;
};
type ModulePage = {
  total: number;
  items: ModuleRow[];
  next_offset: number | null;
};

export function DocumentAssociation({
  project,
  analysis,
  chapter,
  onClose,
  onSelect,
}: {
  project: string;
  analysis: string;
  chapter: AssociationChapter;
  onClose: () => void;
  onSelect: (target: ReviewTarget) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ModuleRow | null>(null);
  const [page, setPage] = useState<ModulePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    dialog.current?.showModal();
    dialog.current?.querySelector("input")?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const timer = setTimeout(
      () => {
        const params = new URLSearchParams({
          analysis_set_id: analysis,
          module_only: "true",
          mode: "all",
          q: query,
          offset: String(offset),
          limit: "30",
        });
        void api<ModulePage>(
          `/api/v3/projects/${project}/interactive/reassociate/targets?${params}`,
          { signal: controller.signal },
        )
          .then((result) => {
            if (!controller.signal.aborted) setPage(result);
          })
          .catch((cause) => {
            if (!controller.signal.aborted) {
              setPage(null);
              setError((cause as Error).message);
            }
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          });
      },
      query ? 200 : 0,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [project, analysis, query, offset, retry]);
  return (
    <dialog
      ref={dialog}
      className="document-association"
      aria-label="选择关联模块"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") event.stopPropagation();
      }}
    >
      <header className="association-review-head">
        <div>
          <h2>选择关联模块</h2>
          <p>将当前章节加入目标模块，再预览 AI 完善后的关联说明。</p>
        </div>
        <button aria-label="关闭模块选择" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="association-added-chapter">
        <FileText size={18} />
        <div>
          <b>{chapter.name}</b>
          <p>{chapter.document}</p>
        </div>
      </div>
      <div className="document-association-search">
        <Search size={17} />
        <input
          aria-label="搜索目标模块"
          maxLength={200}
          placeholder="搜索模块名称或页面路径…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOffset(0);
            setSelected(null);
          }}
        />
      </div>
      <div
        className="document-association-modules"
        role="radiogroup"
        aria-label="目标模块"
        aria-busy={loading}
      >
        {error ? (
          <div role="alert">
            <p>{error}</p>
            <button onClick={() => setRetry((value) => value + 1)}>
              重新加载模块
            </button>
          </div>
        ) : loading ? (
          <p role="status">正在读取模块…</p>
        ) : !page?.items.length ? (
          <p>没有可关联的模块。可先导入原型并完成页面扫描，或调整搜索条件。</p>
        ) : (
          page.items.map((row) => (
            <label
              key={row.item_id}
              className={selected?.item_id === row.item_id ? "selected" : ""}
            >
              <input
                type="radio"
                name="association-module"
                checked={selected?.item_id === row.item_id}
                onChange={() => setSelected(row)}
              />
              <Layers size={17} />
              <span>
                <b>{row.module_label || row.label}</b>
                <small>
                  {row.request.target.route}
                  {row.request.target.frame_path.length
                    ? ` · ${row.request.target.frame_path.join(" / ")}`
                    : ""}
                </small>
              </span>
              <small>{row.request.binding_id ? "已有关联" : "待关联"}</small>
            </label>
          ))
        )}
      </div>
      <div className="document-association-paging">
        <span>{page && !loading ? `${page.total} 个模块` : ""}</span>
        <button
          disabled={loading || !offset}
          onClick={() => {
            setOffset(Math.max(0, offset - 30));
            setSelected(null);
          }}
        >
          上一页
        </button>
        <button
          disabled={loading || page?.next_offset == null}
          onClick={() => {
            setOffset(page!.next_offset!);
            setSelected(null);
          }}
        >
          下一页
        </button>
      </div>
      <footer>
        <span>
          {selected
            ? `目标：${selected.module_label || selected.label}`
            : "请选择需要关联的模块"}
        </span>
        <button onClick={onClose}>取消</button>
        <button
          className="primary"
          disabled={!selected || loading || !!error}
          onClick={() => {
            if (selected)
              onSelect({
                ...selected.request,
                title: selected.module_label || selected.label,
                additional_section_revision_ids: [chapter.revision],
              });
          }}
        >
          下一步：关联预览
        </button>
      </footer>
    </dialog>
  );
}
