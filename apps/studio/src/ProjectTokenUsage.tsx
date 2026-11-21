import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, ApiError } from "../../web/src/api";

type Period = "day" | "month";
type Usage = {
  total_tokens: number;
  answer_tokens: number;
  association_tokens: number;
  embedding_tokens: number;
  other_tokens: number;
  unclassified_tokens: number;
};
type Report = { path: string; usage: Usage } | { path: string; error: string };

const format = new Intl.NumberFormat("zh-CN");
const formatTokens = (tokens?: number) =>
  tokens === undefined ? "—" : format.format(tokens);
const shanghaiDate = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

export function ProjectTokenUsage({
  project,
  refreshVersion,
}: {
  project: string;
  refreshVersion: number;
}) {
  const [period, setPeriod] = useState<Period>("month");
  const [day, setDay] = useState(() => shanghaiDate.format(new Date()));
  const [month, setMonth] = useState(() => day.slice(0, 7));
  const [report, setReport] = useState<Report>();
  const [busy, setBusy] = useState(true);
  const [request, setRequest] = useState(0);
  const date = period === "day" ? day : month + "-01";
  const query = new URLSearchParams({ period, date });
  const path = `/api/v3/projects/${project}/model-usage/period?${query}`;
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    void api<Usage>(path, { signal: controller.signal, cache: "no-store" })
      .then((usage) => {
        if (!controller.signal.aborted) setReport({ path, usage });
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        setReport({
          path,
          error:
            cause instanceof ApiError && cause.status === 403
              ? "当前账号无权查看项目用量。"
              : "无法读取 Token 用量，请刷新重试。",
        });
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [path, refreshVersion, request]);
  const current = report?.path === path ? report : undefined;
  const usage = current && "usage" in current ? current.usage : undefined;
  const message = !current
    ? "正在读取项目用量…"
    : "error" in current
      ? current.error
      : "";
  return (
    <aside
      className="project-token-usage"
      aria-label="项目 Token 用量"
      aria-busy={busy || !current}
    >
      <header>
        <h2>项目 Token 用量</h2>
        <div className="token-report-actions">
          <div
            className="token-report-views"
            role="group"
            aria-label="统计周期"
          >
            <button
              type="button"
              aria-pressed={period === "day"}
              onClick={() => setPeriod("day")}
            >
              按日
            </button>
            <button
              type="button"
              aria-pressed={period === "month"}
              onClick={() => setPeriod("month")}
            >
              按月
            </button>
          </div>
          <input
            className="token-report-date"
            type={period === "day" ? "date" : "month"}
            aria-label={period === "day" ? "统计日期" : "统计月份"}
            value={period === "day" ? day : month}
            onChange={(event) => {
              const { value } = event.target;
              if (value) (period === "day" ? setDay : setMonth)(value);
            }}
          />
          <button
            type="button"
            aria-label="刷新 Token 用量"
            title="刷新 Token 用量"
            disabled={busy}
            onClick={() => setRequest((current) => current + 1)}
          >
            <RefreshCw size={15} />
          </button>
        </div>
      </header>
      <dl className="token-report-metrics">
        {[
          { label: "本期合计", tokens: usage?.total_tokens },
          { label: "回答", tokens: usage?.answer_tokens },
          { label: "文档关联", tokens: usage?.association_tokens },
          { label: "向量", tokens: usage?.embedding_tokens },
        ].map(({ label, tokens }, index) => {
          const value = formatTokens(tokens);
          return (
            <div
              key={label}
              className={index === 0 ? "token-report-total" : undefined}
            >
              <dt>{label}</dt>
              <dd title={value}>
                {index === 0 ? <strong>{value}</strong> : value}
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="token-report-message" role="status">
        {message}
        {usage && usage.unclassified_tokens > 0 && (
          <span>历史未分类 {formatTokens(usage.unclassified_tokens)}</span>
        )}
        {usage && usage.other_tokens > 0 && (
          <span>其他 {formatTokens(usage.other_tokens)}</span>
        )}
      </p>
    </aside>
  );
}
