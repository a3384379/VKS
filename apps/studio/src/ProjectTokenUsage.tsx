import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, X } from "lucide-react";
import { api, ApiError } from "../../web/src/api";
import {
  bindingModuleTree,
  bindingPageKey,
  type ModulePage,
} from "./bindingModuleTree";

type Counts = { input_tokens: number; output_tokens: number };
type Usage = {
  model: Counts;
  embedding_tokens: number;
  unattributed: Counts;
  functions: (Counts & {
    package_id: string;
    route: string;
    page_title: string;
  })[];
};
const sum = (usage: Counts) => usage.input_tokens + usage.output_tokens;
const format = new Intl.NumberFormat("zh-CN");

function usageGroups(
  usage: Usage,
  packages: { id: string; name: string }[],
  pages: ModulePage[],
) {
  const rows = usage.functions.map((item, index) => ({
    ...item,
    id: String(index),
    target: { route: item.route, label: item.page_title, selector: "body" },
  }));
  const tree = bindingModuleTree(rows, packages, pages);
  const modules = tree.roots.map((node) => {
    let input_tokens = 0,
      output_tokens = 0;
    for (const id of node.bindingIds) {
      input_tokens += rows[Number(id)].input_tokens;
      output_tokens += rows[Number(id)].output_tokens;
    }
    return {
      id: node.id,
      label: node.label,
      path: "",
      input_tokens,
      output_tokens,
    };
  });
  const features = rows.map((row) => {
    const node = tree.byPage.get(bindingPageKey(row));
    return {
      ...row,
      label: node?.label || row.route,
      path: node?.path.slice(0, -1).join(" / ") || "",
    };
  });
  return {
    modules: modules.sort((a, b) => sum(b) - sum(a)),
    features: features.sort((a, b) => sum(b) - sum(a)),
  };
}

export function ProjectTokenUsage({
  project,
  analysis,
  release,
  refreshVersion,
  packages,
  pages,
}: {
  project: string;
  analysis: string;
  release: string;
  refreshVersion: number;
  packages: { id: string; name: string }[];
  pages: ModulePage[];
}) {
  const [usage, setUsage] = useState<Usage>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [request, setRequest] = useState(0);
  const [view, setView] = useState<"modules" | "features">("modules");
  const details = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams(
      release ? { release_id: release } : { analysis_set_id: analysis },
    );
    setBusy(true);
    setError("");
    void api<Usage>(
      `/api/v3/projects/${project}/model-usage/summary?${query}`,
      {
        signal: controller.signal,
        cache: "no-store",
      },
    )
      .then((data) => {
        if (!controller.signal.aborted) setUsage(data);
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        setUsage(undefined);
        setError(
          cause instanceof ApiError && cause.status === 403
            ? "当前账号无权查看项目用量。"
            : "无法读取 Token 用量，请刷新重试。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [project, analysis, release, refreshVersion, request]);
  const groups = useMemo(
    () => usage && usageGroups(usage, packages, pages),
    [usage, packages, pages],
  );
  const rows = groups?.[view] || [];
  const largest = rows[0] ? sum(rows[0]) : 1;
  return (
    <aside
      className="project-token-usage"
      aria-label="项目 Token 用量"
      aria-busy={busy}
    >
      <header>
        <h2>项目 Token 用量</h2>
        <div className="token-report-actions">
          <button
            type="button"
            aria-haspopup="dialog"
            disabled={!usage}
            onClick={() => details.current?.showModal()}
          >
            用量排行
          </button>
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
      {error ? (
        <p className="token-report-message" role="status">
          {error}
        </p>
      ) : usage ? (
        <>
          <dl className="token-report-metrics">
            <div className="token-report-total">
              <dt>项目累计</dt>
              <dd>
                <strong>
                  {format.format(sum(usage.model) + usage.embedding_tokens)}
                </strong>
              </dd>
            </div>
            <div>
              <dt>文本输入</dt>
              <dd>{format.format(usage.model.input_tokens)}</dd>
            </div>
            <div>
              <dt>文本输出</dt>
              <dd>{format.format(usage.model.output_tokens)}</dd>
            </div>
            <div>
              <dt>向量嵌入</dt>
              <dd>{format.format(usage.embedding_tokens)}</dd>
            </div>
          </dl>
          <dialog
            ref={details}
            className="token-report-dialog"
            aria-label="项目 Token 用量排行"
          >
            <header>
              <h2>项目 Token 用量排行</h2>
              <button
                type="button"
                aria-label="关闭用量排行"
                onClick={() => details.current?.close()}
              >
                <X size={16} />
              </button>
            </header>
            <div
              className="token-report-views"
              role="group"
              aria-label="Token 用量分组"
            >
              <button
                type="button"
                aria-pressed={view === "modules"}
                onClick={() => setView("modules")}
              >
                按模块
              </button>
              <button
                type="button"
                aria-pressed={view === "features"}
                onClick={() => setView("features")}
              >
                按功能
              </button>
            </div>
            {rows.length ? (
              <ol
                className="token-report-chart"
                aria-label={
                  view === "modules" ? "模块用量排行" : "功能用量排行"
                }
                tabIndex={0}
              >
                {rows.map((row) => (
                  <li key={row.id}>
                    <div className="token-report-row">
                      <span
                        title={
                          row.path ? `${row.path} / ${row.label}` : row.label
                        }
                      >
                        {row.label}
                      </span>
                      <b
                        title={`输入 ${format.format(row.input_tokens)} · 输出 ${format.format(row.output_tokens)}`}
                      >
                        {format.format(sum(row))}
                      </b>
                    </div>
                    {row.path && <small title={row.path}>{row.path}</small>}
                    <div className="token-report-bar" aria-hidden="true">
                      <i
                        style={{
                          width: `${(row.input_tokens / largest) * 100}%`,
                        }}
                      />
                      <i
                        style={{
                          width: `${(row.output_tokens / largest) * 100}%`,
                        }}
                      />
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="token-report-message">
                暂无可按{view === "modules" ? "模块" : "功能"}归属的用量。
              </p>
            )}
            <footer>
              <div>
                <span>未归属的文本用量</span>
                <b>{format.format(sum(usage.unattributed))}</b>
              </div>
              <small>文档级请求和缺少页面定位的历史记录计入未归属。</small>
            </footer>
          </dialog>
        </>
      ) : (
        <p className="token-report-message" role="status">
          正在读取项目用量…
        </p>
      )}
    </aside>
  );
}
