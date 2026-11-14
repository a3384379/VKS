import { useMemo, useState, type ReactNode } from "react";
import { ArrowUpRight, FileText, Link2, Search } from "lucide-react";

type ManagedBinding = {
  id: string;
  title: string;
  summary: string;
  package_id: string;
  target: {
    label: string;
    route: string;
    selector: string;
    frame_path?: string[];
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
  b.excluded
    ? "已移除"
    : b.validity_status === "valid"
      ? "已关联"
      : b.validity_status === "broken"
        ? "待定位"
        : "待重新关联";
const moduleKey = (b: ManagedBinding) =>
  JSON.stringify([b.package_id, b.target.route, b.target.frame_path || []]);

export function BindingsManager<T extends ManagedBinding>({
  bindings,
  ready,
  editable,
  canCreate,
  onCreate,
  onReveal,
  onEdit,
  onToggle,
  onSource,
  renderSummary,
}: {
  bindings: T[];
  ready: boolean;
  editable: boolean;
  canCreate: boolean;
  onCreate: () => void;
  onReveal: (binding: T) => void;
  onEdit: (binding: T) => void;
  onToggle: (binding: T) => void;
  onSource: (binding: T, index: number) => void;
  renderSummary: (binding: T) => ReactNode;
}) {
  const [query, setQuery] = useState(""),
    [filter, setFilter] = useState<Filter>("current"),
    [module, setModule] = useState(""),
    [selected, setSelected] = useState("");
  const modules = useMemo(() => {
    const names = new Map<string, string>();
    for (const b of bindings) {
      const key = moduleKey(b);
      if (
        !names.has(key) ||
        b.scope === "module" ||
        b.target.selector === "body"
      )
        names.set(key, b.target.label.split(" · ")[0] || b.target.route);
    }
    return names;
  }, [bindings]);
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
        (!module || moduleKey(b) === module) &&
        (!needle ||
          [
            b.title,
            b.target.label,
            modules.get(moduleKey(b)),
            b.summary,
            ...b.sections.map((c) => c.document_name),
          ]
            .join(" ")
            .toLocaleLowerCase()
            .includes(needle)),
    );
  }, [bindings, filter, module, query, modules]);
  const current = visible.find((b) => b.id === selected) || visible[0];
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
              onClick={() => setFilter(key)}
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
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <select
          aria-label="筛选关联模块"
          value={module}
          onChange={(e) => setModule(e.target.value)}
        >
          <option value="">全部模块</option>
          {[...modules].map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </div>
      {!ready && <p role="status">正在加载关联…</p>}
      {ready && !bindings.length ? (
        <div className="empty">
          <Link2 />
          <h3>从原型开始第一条关联</h3>
          <button
            className="primary"
            disabled={!editable || !canCreate}
            onClick={onCreate}
          >
            选择控件并关联知识
          </button>
        </div>
      ) : (
        <div className="binding-workspace">
          <div className="binding-list-panel">
            <div className="binding-list-caption">{visible.length} 条关联</div>
            <div className="binding-table-scroll">
              <table className="binding-table">
                <thead>
                  <tr>
                    <th>模块 / 操作</th>
                    <th>状态</th>
                    <th>引用</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((b) => (
                    <tr
                      key={b.id}
                      className={current?.id === b.id ? "selected" : ""}
                      onClick={() => setSelected(b.id)}
                    >
                      <td>
                        <button
                          className="binding-row-title"
                          aria-label={`查看关联详情：${b.title}`}
                          aria-pressed={current?.id === b.id}
                          onClick={() => setSelected(b.id)}
                        >
                          <span>{b.title}</span>
                          <small>
                            {modules.get(moduleKey(b))} ·{" "}
                            {b.scope === "module" ||
                            b.target.selector === "body"
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
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {ready && !visible.length && (
              <p className="management-empty">
                没有符合条件的关联，可调整筛选或搜索内容。
              </p>
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
                      {current.origin === "model" ? "AI 自动关联" : "人工维护"}
                    </small>
                  </div>
                  <h2>{current.title}</h2>
                  <p>{current.target.label}</p>
                  <div className="binding-detail-actions">
                    <button onClick={() => onReveal(current)}>
                      <ArrowUpRight size={15} /> 打开原型
                    </button>
                    <button
                      disabled={!editable || !ready}
                      onClick={() => onEdit(current)}
                    >
                      编辑
                    </button>
                    <button
                      disabled={!editable}
                      onClick={() => onToggle(current)}
                    >
                      {current.excluded ? "恢复关联" : "移除关联"}
                    </button>
                  </div>
                </header>
                <div className="binding-detail-body">
                  {renderSummary(current)}
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
                        onClick={() => onSource(current, i)}
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
              </>
            ) : (
              <div className="management-empty">选择左侧关联查看详情。</div>
            )}
          </article>
        </div>
      )}
    </section>
  );
}
