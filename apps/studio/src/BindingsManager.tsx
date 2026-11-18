import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowUpRight, FileText, Link2, Search } from "lucide-react";
import { api } from "../../web/src/api";
import type { ReviewTarget } from "./AssociationReview";
import "./association-review.css";
import { BindingModulePicker } from "./BindingModulePicker";
import {
  bindingModuleTree,
  bindingPageKey,
  type ModulePage,
} from "./bindingModuleTree";

type ManagedBinding = {
  id: string;
  title: string;
  summary: string;
  package_id: string;
  binding_version?: number;
  point_revision_id?: string;
  target: {
    label: string;
    route: string;
    selector: string;
    frame_path?: string[];
    state?: string;
    open_steps?: string[];
  };
  scope?: string;
  origin?: string;
  excluded: boolean;
  validity_status: string;
  sections: { revision_id: string; document_name: string; name: string }[];
};
type Filter = "current" | "pending" | "linked" | "removed";
const status = (b: ManagedBinding) =>
  b.excluded ? "removed" : b.validity_status === "valid" ? "linked" : "pending";
const statusLabel = (b: ManagedBinding) =>
  b.origin === "pending"
    ? "待关联"
    : b.excluded
      ? "已移除"
      : b.validity_status === "valid"
        ? "已关联"
        : b.validity_status === "broken"
          ? "待定位"
          : "待重新关联";

export function BindingsManager<T extends ManagedBinding>({
  bindings: savedBindings,
  packages = [],
  inventoryUrl,
  ready,
  editable,
  canCreate,
  onCreate,
  onReveal,
  onEdit,
  onToggle,
  onSource,
  renderSummary,
  onReview,
}: {
  bindings: T[];
  packages?: { id: string; name: string }[];
  inventoryUrl?: string;
  ready: boolean;
  editable: boolean;
  canCreate: boolean;
  onCreate: () => void;
  onReveal: (binding: T) => void;
  onEdit: (binding: T) => void;
  onToggle: (binding: T) => void;
  onSource: (binding: T, index: number) => void;
  renderSummary: (binding: T) => ReactNode;
  onReview?: (items: ReviewTarget[]) => void;
}) {
  const [query, setQuery] = useState(""),
    [filter, setFilter] = useState<Filter>("current"),
    [module, setModule] = useState(""),
    [selected, setSelected] = useState(""),
    [page, setPage] = useState(0),
    [jump, setJump] = useState(""),
    [pageSize, setPageSize] = useState(10);
  const [reviewSelection, setReviewSelection] = useState(
    new Map<string, ReviewTarget>(),
  );
  const [pendingTargets, setPendingTargets] = useState<
    {
      item_id: string;
      label: string;
      module_label: string;
      request: ReviewTarget;
    }[]
  >([]);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [pendingError, setPendingError] = useState("");
  const bindingIdentity = savedBindings
    .map(
      (binding) =>
        `${binding.id}:${binding.binding_version}:${binding.point_revision_id}:${binding.excluded}`,
    )
    .join("|");
  useEffect(() => {
    if (!inventoryUrl || !onReview || !editable || !ready) {
      setPendingTargets([]);
      return;
    }
    const controller = new AbortController();
    setPendingLoading(true);
    setPendingError("");
    const targetUrl = inventoryUrl.replace(
      "/associations/inventory?",
      "/interactive/reassociate/targets?",
    );
    async function load() {
      const rows: typeof pendingTargets = [];
      let offset: number | null = 0;
      while (offset !== null) {
        const result: {
          items: typeof pendingTargets;
          next_offset?: number | null;
        } = await api(
          `${targetUrl}&module_only=false&mode=missing&offset=${offset}&limit=200`,
          { signal: controller.signal },
        );
        rows.push(...result.items);
        offset = result.next_offset ?? null;
      }
      if (!controller.signal.aborted) setPendingTargets(rows);
    }
    void load()
      .catch((cause) => {
        if (!controller.signal.aborted) setPendingError(cause.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setPendingLoading(false);
      });
    return () => controller.abort();
  }, [inventoryUrl, editable, ready, bindingIdentity, !!onReview]);
  const pendingById = useMemo(
    () =>
      new Map(
        pendingTargets
          .filter((row) => !row.request.binding_id)
          .map((row) => [`inventory:${row.item_id}`, row]),
      ),
    [pendingTargets],
  );
  const bindings: ManagedBinding[] = useMemo(
    () => [
      ...savedBindings,
      ...[...pendingById].map(
        ([id, row]) =>
          ({
            id,
            title: row.label,
            summary: "尚未关联知识，可填写方向生成 AI 关联预览。",
            package_id: row.request.package_id,
            target: row.request.target,
            scope: row.request.context.scope,
            origin: "pending",
            excluded: false,
            validity_status: "pending",
            sections: [],
          }) satisfies ManagedBinding,
      ),
    ],
    [savedBindings, pendingById],
  );
  function reviewTarget(binding: ManagedBinding): ReviewTarget {
    const pending = pendingById.get(binding.id);
    if (pending) return { ...pending.request, title: pending.label };
    return {
      package_id: binding.package_id,
      target: {
        ...binding.target,
        frame_path: binding.target.frame_path || [],
        state: binding.target.state || "",
        open_steps: binding.target.open_steps || [],
      },
      context: {
        scope:
          binding.scope === "module" || binding.target.selector === "body"
            ? "module"
            : "control",
        page_title:
          tree.byPage.get(bindingPageKey(binding))?.label ||
          binding.target.label,
        section_title: "",
        nearby_labels: [],
      },
      binding_id: binding.id,
      binding_version: binding.binding_version,
      point_revision_id: binding.point_revision_id,
      title: binding.title,
    };
  }
  function toggleReview(binding: ManagedBinding, checked: boolean) {
    setReviewSelection((previous) => {
      const next = new Map(previous);
      if (checked) next.set(binding.id, reviewTarget(binding));
      else next.delete(binding.id);
      return next;
    });
  }
  const [pages, setPages] = useState<ModulePage[]>([]);
  const [loadPages, setLoadPages] = useState(false);
  const packageIdentity = packages
    .map((pack) => pack.id)
    .sort()
    .join(":");
  useEffect(() => {
    if (!loadPages || !inventoryUrl) return;
    const controller = new AbortController();
    void api<{
      packages: {
        package_id: string;
        pages: Omit<ModulePage, "package_id">[];
      }[];
    }>(inventoryUrl, { signal: controller.signal })
      .then((manifest) => {
        if (!controller.signal.aborted)
          setPages(
            manifest.packages.flatMap((pack) =>
              pack.pages.map((page) => ({
                ...page,
                package_id: pack.package_id,
              })),
            ),
          );
      })
      .catch(() => {}); // Saved page titles remain available if optional metadata cannot load.
    return () => controller.abort();
  }, [loadPages, inventoryUrl, packageIdentity]);
  const tree = useMemo(
    () => bindingModuleTree(bindings, packages, pages),
    [bindings, packages, pages],
  );
  const selectedModule = tree.nodes.get(module);
  useEffect(() => {
    if (ready && module && !tree.nodes.has(module)) {
      setModule("");
      setPage(0);
    }
  }, [ready, module, tree]);
  const counts = useMemo(
    () => ({
      current: bindings.filter((b) => !b.excluded).length,
      linked: bindings.filter((b) => status(b) === "linked").length,
      pending: bindings.filter((b) => status(b) === "pending").length,
      removed: bindings.filter((b) => b.excluded).length,
    }),
    [bindings],
  );
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return bindings.filter(
      (b) =>
        (filter === "current" ? !b.excluded : status(b) === filter) &&
        (!selectedModule || selectedModule.bindingIds.has(b.id)) &&
        (!needle ||
          [
            b.title,
            b.target.label,
            tree.byPage.get(bindingPageKey(b))?.path.join(" "),
            b.summary,
            ...b.sections.map((c) => c.document_name),
          ]
            .join(" ")
            .toLocaleLowerCase()
            .includes(needle)),
    );
  }, [bindings, filter, selectedModule, query, tree]);
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const pageBindings = visible.slice(
    currentPage * pageSize,
    (currentPage + 1) * pageSize,
  );
  const current =
    pageBindings.find((b) => b.id === selected) || pageBindings[0];
  useEffect(() => {
    if (ready) setPage((value) => Math.min(value, pageCount - 1));
  }, [pageCount, ready]);
  function navigate(next: number) {
    setPage(next);
    setSelected("");
    setJump("");
  }
  function resetPage() {
    setPage(0);
    setSelected("");
    setJump("");
  }
  return (
    <section className="binding-manager" aria-label="关联管理">
      <header className="binding-management-heading">
        <div>
          <span className="eyebrow">知识与业务操作</span>
          <h1>关联管理</h1>
          <p>按模块查找关联，选中后查看完整说明与原文。</p>
        </div>
      </header>
      <div className="binding-filters">
        <div
          className="binding-status-filters"
          role="group"
          aria-label="关联状态筛选"
        >
          {(
            [
              ["current", "全部当前"],
              ["pending", "待处理"],
              ["linked", "已关联"],
              ["removed", "已移除"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              aria-pressed={filter === key}
              onClick={() => {
                setFilter(key);
                resetPage();
              }}
            >
              {label}
              <span>{counts[key]}</span>
            </button>
          ))}
        </div>
        <div className="binding-search">
          <Search size={16} />
          <input
            aria-label="搜索关联"
            placeholder="搜索模块、控件或说明…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              resetPage();
            }}
          />
        </div>
        <BindingModulePicker
          roots={tree.roots}
          selected={selectedModule}
          onOpen={() => setLoadPages(true)}
          onSelect={(id) => {
            setModule(id);
            resetPage();
          }}
        />
      </div>
      {onReview && (
        <div className="binding-review-actions">
          <span className="binding-review-selection" role="status">
            已选 {reviewSelection.size} 项，跨页保留
          </span>
          <button
            className="primary"
            disabled={!editable || !ready || !reviewSelection.size}
            onClick={() => onReview([...reviewSelection.values()])}
          >
            批量 AI 关联
          </button>
          <button
            disabled={!reviewSelection.size}
            onClick={() => setReviewSelection(new Map())}
          >
            清空本次选择
          </button>
          <small>先预览生成内容，确认后统一保存。</small>
        </div>
      )}
      <p
        className="binding-inventory-status"
        role={pendingError ? "alert" : "status"}
        title={pendingError || undefined}
      >
        {!ready
          ? "正在加载关联…"
          : pendingLoading
            ? "正在读取未关联模块与控件…"
            : pendingError
              ? `未关联清单未能读取：${pendingError}`
              : ""}
      </p>
      <div className="binding-workspace">
        <div className="binding-list-panel">
          <div className="binding-list-caption">
            {visible.length} 条关联
            {!!visible.length &&
              ` · 当前 ${currentPage * pageSize + 1}–${Math.min((currentPage + 1) * pageSize, visible.length)} 条`}
          </div>
          <div className="binding-table-wrap">
            <table className="binding-table">
              <thead>
                <tr>
                  {onReview && (
                    <th>
                      <input
                        type="checkbox"
                        aria-label="选择本页可关联项"
                        checked={
                          !!pageBindings.filter((b) => !b.excluded).length &&
                          pageBindings
                            .filter((b) => !b.excluded)
                            .every((b) => reviewSelection.has(b.id))
                        }
                        disabled={!editable || !ready}
                        onChange={(event) => {
                          const checked = event.target.checked;
                          setReviewSelection((previous) => {
                            const next = new Map(previous);
                            for (const binding of pageBindings.filter(
                              (b) => !b.excluded,
                            )) {
                              if (checked)
                                next.set(binding.id, reviewTarget(binding));
                              else next.delete(binding.id);
                            }
                            return next;
                          });
                        }}
                      />
                    </th>
                  )}
                  <th>模块 / 操作</th>
                  <th>状态</th>
                  <th>引用</th>
                  {onReview && <th>AI 关联</th>}
                </tr>
              </thead>
              <tbody>
                {pageBindings.map((b) => (
                  <tr
                    key={b.id}
                    className={current?.id === b.id ? "selected" : ""}
                    onClick={() => setSelected(b.id)}
                  >
                    {onReview && (
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择关联：${b.title}`}
                          checked={reviewSelection.has(b.id)}
                          disabled={!editable || b.excluded}
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) =>
                            toggleReview(b, event.target.checked)
                          }
                        />
                      </td>
                    )}
                    <td>
                      <button
                        className="binding-row-title"
                        aria-label={`查看关联详情：${b.title}`}
                        aria-pressed={current?.id === b.id}
                        onClick={() => setSelected(b.id)}
                      >
                        <span>{b.title}</span>
                        <small>
                          {tree.byPage.get(bindingPageKey(b))?.path.join(" / ")}{" "}
                          ·{" "}
                          {b.scope === "module" || b.target.selector === "body"
                            ? "模块"
                            : "控件"}
                        </small>
                      </button>
                    </td>
                    <td>
                      <span className={`binding-status ${status(b)}`}>
                        {statusLabel(b)}
                      </span>
                    </td>
                    <td>{b.sections.length} 章</td>
                    {onReview && (
                      <td>
                        <button
                          className="binding-row-ai"
                          disabled={!editable || !ready || b.excluded}
                          aria-label={`${pendingById.has(b.id) ? "AI关联" : "AI重新关联"}：${b.title}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            onReview([reviewTarget(b)]);
                          }}
                        >
                          {pendingById.has(b.id) ? "AI 关联" : "AI 重新关联"}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {ready && !visible.length && (
            <div className="management-empty">
              {!bindings.length ? (
                <>
                  <Link2 />
                  <h3>从原型开始第一条关联</h3>
                  <button
                    className="primary"
                    disabled={!editable || !canCreate}
                    onClick={onCreate}
                  >
                    选择控件并关联知识
                  </button>
                </>
              ) : (
                <p>没有符合条件的关联，可调整筛选或搜索内容。</p>
              )}
            </div>
          )}
          {ready && !!visible.length && (
            <nav className="binding-pagination" aria-label="关联列表分页">
              <span aria-live="polite">
                第 {currentPage + 1} / {pageCount} 页
              </span>
              <select
                aria-label="每页关联条数"
                value={pageSize}
                onChange={(event) => {
                  setPageSize(Number(event.target.value));
                  resetPage();
                }}
              >
                {[10, 20, 50].map((size) => (
                  <option key={size} value={size}>
                    每页 {size} 条
                  </option>
                ))}
              </select>
              <div className="binding-pagination-controls">
                <button
                  disabled={currentPage === 0}
                  onClick={() => navigate(0)}
                >
                  首页
                </button>
                <button
                  disabled={currentPage === 0}
                  onClick={() => navigate(currentPage - 1)}
                >
                  上一页
                </button>
                <button
                  disabled={currentPage + 1 >= pageCount}
                  onClick={() => navigate(currentPage + 1)}
                >
                  下一页
                </button>
                <button
                  disabled={currentPage + 1 >= pageCount}
                  onClick={() => navigate(pageCount - 1)}
                >
                  末页
                </button>
              </div>
              <form
                className="batch-result-jump"
                onSubmit={(event) => {
                  event.preventDefault();
                  const value = Number(jump);
                  if (
                    Number.isInteger(value) &&
                    value >= 1 &&
                    value <= pageCount
                  )
                    navigate(value - 1);
                }}
              >
                <label>
                  跳至
                  <input
                    aria-label="跳转关联页"
                    type="number"
                    min={1}
                    max={pageCount}
                    step={1}
                    value={jump}
                    placeholder={`1–${pageCount}`}
                    onChange={(event) => setJump(event.target.value)}
                  />
                  页
                </label>
                <button disabled={!jump}>跳转</button>
              </form>
            </nav>
          )}
        </div>
        <article className="binding-detail-panel" aria-label="关联详情">
          {current ? (
            <>
              <header>
                <div>
                  <span className={`binding-status ${status(current)}`}>
                    {statusLabel(current)}
                  </span>
                  <small>
                    {pendingById.has(current.id)
                      ? "尚未关联"
                      : current.origin === "model"
                        ? "AI 自动关联"
                        : "人工维护"}
                  </small>
                </div>
                <h2>{current.title}</h2>
                <p>{current.target.label}</p>
                <div className="binding-detail-actions">
                  {onReview && (
                    <button
                      disabled={!editable || !ready || current.excluded}
                      onClick={() => onReview([reviewTarget(current)])}
                    >
                      {pendingById.has(current.id) ? "AI 关联" : "AI 重新关联"}
                    </button>
                  )}
                  {!pendingById.has(current.id) && (
                    <>
                      <button
                        onClick={() =>
                          onReveal(
                            savedBindings.find(
                              (binding) => binding.id === current.id,
                            )!,
                          )
                        }
                      >
                        <ArrowUpRight size={15} /> 打开原型
                      </button>
                      <button
                        disabled={!editable || !ready}
                        onClick={() =>
                          onEdit(
                            savedBindings.find(
                              (binding) => binding.id === current.id,
                            )!,
                          )
                        }
                      >
                        编辑
                      </button>
                      <button
                        disabled={!editable}
                        onClick={() =>
                          onToggle(
                            savedBindings.find(
                              (binding) => binding.id === current.id,
                            )!,
                          )
                        }
                      >
                        {current.excluded ? "恢复关联" : "移除关联"}
                      </button>
                    </>
                  )}
                </div>
              </header>
              <div className="binding-detail-scroll">
                <div className="binding-detail-body">
                  {pendingById.has(current.id) ? (
                    <p>
                      尚未关联知识。点击 AI
                      关联并填写方向后，先查看生成预览，确认才保存。
                    </p>
                  ) : (
                    renderSummary(
                      savedBindings.find(
                        (binding) => binding.id === current.id,
                      )!,
                    )
                  )}
                </div>
                <section
                  className="binding-detail-sources"
                  aria-label="引用原文"
                >
                  <h3>
                    引用原文 <span>{current.sections.length}</span>
                  </h3>
                  {current.sections.length ? (
                    current.sections.map((c, i) => (
                      <button
                        className="citation"
                        key={c.revision_id}
                        onClick={() =>
                          onSource(
                            savedBindings.find(
                              (binding) => binding.id === current.id,
                            )!,
                            i,
                          )
                        }
                      >
                        <FileText size={16} />
                        <span>
                          {c.document_name}
                          <small>{c.name}</small>
                        </span>
                        <ArrowUpRight size={15} />
                      </button>
                    ))
                  ) : (
                    <p>暂未关联准确章节，可回到原型重新分析或补充资料。</p>
                  )}
                </section>
              </div>
            </>
          ) : (
            <div className="management-empty">选择左侧关联查看详情。</div>
          )}
        </article>
      </div>
    </section>
  );
}
