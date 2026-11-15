import { useEffect, useState } from "react";
import { api } from "../../web/src/api";

type Outcome = { id?: string; label: string; message: string };
type Result = {
  processed?: number;
  details_truncated?: boolean;
  completed?: Record<string, Outcome>;
};
const PAGE_SIZE = 10;

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
  const [remote, setRemote] = useState<{
    page: number;
    items: Outcome[];
    hasMore: boolean;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const serverPaged = !!job.result?.details_truncated;
  const preview = Object.values(job.result?.completed || {});
  const pageCount = Math.max(1, Math.ceil(preview.length / PAGE_SIZE));
  const currentPage = serverPaged ? page : Math.min(page, pageCount - 1);

  useEffect(() => {
    if (!open || !serverPaged) {
      setLoading(false);
      setError("");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void api<{
      items: Outcome[];
      has_more: boolean;
      diagnostics_restricted?: boolean;
    }>(
      `${base}/items?job_id=${encodeURIComponent(job.id)}&offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (controller.signal.aborted) return;
        setRemote({
          page,
          items: result.diagnostics_restricted ? [] : result.items,
          hasMore: !result.diagnostics_restricted && result.has_more,
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
    setPage(next);
  }

  return (
    <details
      className="batch-result-details"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>查看逐项结果</summary>
      {open && (
        <>
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
              第 {currentPage + 1}
              {serverPaged ? "" : ` / ${pageCount}`} 页 · 每页 {PAGE_SIZE} 条
            </span>
            <div>
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
            </div>
          </nav>
        </>
      )}
    </details>
  );
}
