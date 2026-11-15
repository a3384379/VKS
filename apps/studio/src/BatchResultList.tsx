import { useEffect, useState } from "react";
import { api } from "../../web/src/api";

type Outcome = { id?: string; label: string; message: string; status?: string };
type Result = {
  processed?: number;
  details_truncated?: boolean;
  completed?: Record<string, Outcome>;
};
const PAGE_SIZE = 10;
const FILTERS = [
  ["all", "全部结果"],
  ["linked", "已保存 / 复用"],
  ["insufficient", "依据不足"],
  ["failed", "调用失败"],
  ["manual", "人工保留"],
  ["excluded", "已移除"],
  ["skipped", "已跳过"],
] as const;

export function BatchResultList({
  base,
  job,
}: {
  base: string;
  job: { id: string; state: string; result?: Result };
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [jump, setJump] = useState("");
  const [remote, setRemote] = useState<{
    page: number;
    items: Outcome[];
    hasMore: boolean;
    total: number;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const serverPaged = !!job.result?.details_truncated;
  const preview = Object.values(job.result?.completed || {}).filter(
    (item) =>
      (status === "all" ||
        (status === "linked"
          ? ["linked", "reused"].includes(item.status || "")
          : item.status === status)) &&
      (!query.trim() ||
        `${item.label} ${item.message}`
          .toLocaleLowerCase()
          .includes(query.trim().toLocaleLowerCase())),
  );
  const total = serverPaged
    ? (remote?.total ??
      (query || status !== "all" ? 0 : job.result?.processed || 0))
    : preview.length;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    if (!serverPaged || remote)
      setPage((value) => Math.min(value, pageCount - 1));
  }, [pageCount, serverPaged, remote?.total]);

  useEffect(() => {
    if (!open || !serverPaged) {
      setLoading(false);
      setError("");
      return;
    }
    if (query.trim() !== search) {
      setLoading(true);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void api<{
      items: Outcome[];
      has_more: boolean;
      total: number;
      diagnostics_restricted?: boolean;
    }>(
      `${base}/items?job_id=${encodeURIComponent(job.id)}&offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}&q=${encodeURIComponent(search)}&status=${encodeURIComponent(status)}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (controller.signal.aborted) return;
        setRemote({
          page,
          items: result.diagnostics_restricted ? [] : result.items,
          hasMore: !result.diagnostics_restricted && result.has_more,
          total: result.diagnostics_restricted ? 0 : result.total,
        });
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setRemote(null);
          setError((reason as Error).message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    base,
    job.id,
    job.state,
    job.result?.processed,
    open,
    page,
    retry,
    serverPaged,
    query,
    search,
    status,
  ]);

  const items = serverPaged
    ? remote?.page === currentPage
      ? remote.items
      : []
    : preview.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const hasMore = serverPaged
    ? remote?.page === currentPage && remote.hasMore
    : currentPage + 1 < pageCount;

  function navigate(next: number) {
    setError("");
    setPage(Math.max(0, Math.min(next, pageCount - 1)));
    setJump("");
  }
  function resetSearch() {
    setPage(0);
    setRemote(null);
    setError("");
    setJump("");
  }

  return (
    <details
      className="batch-result-details"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>查看逐项结果</summary>
      {open && (
        <>
          <div className="batch-result-filters">
            <input
              aria-label="搜索逐项结果"
              placeholder="搜索控件名称或结果说明…"
              maxLength={200}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                resetSearch();
              }}
            />
            <select
              aria-label="筛选处理结果"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                resetSearch();
              }}
            >
              {FILTERS.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div
            className="batch-result-viewport"
            role="region"
            aria-label="逐项处理结果"
            aria-busy={loading}
            key={currentPage}
          >
            {loading && !items.length ? (
              <p role="status">正在读取结果…</p>
            ) : error ? (
              <div role="alert">
                <p>{error}</p>
                <button onClick={() => setRetry((value) => value + 1)}>
                  重新读取
                </button>
              </div>
            ) : items.length ? (
              <ul className="batch-result-list">
                {items.map((item, index) => (
                  <li key={item.id || index}>
                    <b>{item.label}</b>
                    <span>
                      {item.message.length > 80 ? (
                        <>
                          <span className="batch-result-message">
                            {item.message.slice(0, 80)}…
                          </span>
                          <details className="batch-result-explanation">
                            <summary>展开完整说明</summary>
                            <p>{item.message}</p>
                          </details>
                        </>
                      ) : (
                        item.message
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>暂无可查看的结果</p>
            )}
          </div>
          <nav className="batch-result-pagination" aria-label="逐项结果分页">
            <span aria-live="polite">
              共 {total} 条 · 第 {currentPage + 1} / {pageCount} 页 · 每页{" "}
              {PAGE_SIZE} 条
            </span>
            <div>
              <button
                disabled={loading || currentPage === 0}
                onClick={() => navigate(0)}
              >
                首页
              </button>
              <button
                disabled={loading || currentPage === 0}
                onClick={() => navigate(currentPage - 1)}
              >
                上一页
              </button>
              <button
                disabled={loading || !!error || !hasMore}
                onClick={() => navigate(currentPage + 1)}
              >
                下一页
              </button>
              <button
                disabled={loading || !!error || currentPage + 1 >= pageCount}
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
                if (Number.isInteger(value) && value >= 1 && value <= pageCount)
                  navigate(value - 1);
              }}
            >
              <label>
                跳至
                <input
                  aria-label="跳转结果页"
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
              <button disabled={loading || !jump || !!error}>跳转</button>
            </form>
          </nav>
        </>
      )}
    </details>
  );
}
