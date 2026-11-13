import { useEffect, useRef, useState } from "react";
import { Database, RefreshCw, Save } from "lucide-react";
import { api, mutation } from "../../web/src/api";
type Settings = {
  enabled: boolean;
  connection_id: string | null;
  model_id: string;
  dimensions: number;
  daily_token_limit: number;
};
type Status = {
  state: string;
  phase?: string;
  completed: number;
  total: number;
  error: string;
  indexed_chunks: number;
  indexed_sections: number;
  total_sections: number;
  used_tokens: number;
  configuration_error?: string;
};
export function RAGSettings({
  project,
  analysis,
  release,
}: {
  project: string;
  analysis: string;
  release: string;
}) {
  const [form, setForm] = useState<Settings | null>(null),
    [status, setStatus] = useState<Status | null>(null),
    [connections, setConnections] = useState<
      { id: string; name: string; provider: string }[]
    >([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const payload = useRef<Record<string, unknown> | null>(null),
    epoch = useRef(0);
  const root = `/api/v3/projects/${project}/interactive`,
    base = root + "/rag";
  const refresh = async () => {
    if (!payload.current) return;
    const n = epoch.current;
    const s = await api<Status>(
      base + "/documents/status",
      mutation("POST", payload.current),
    );
    if (n === epoch.current) setStatus(s);
  };
  useEffect(() => {
    const n = ++epoch.current;
    payload.current = release
      ? { release_id: release }
      : analysis
        ? { analysis_set_id: analysis }
        : null;
    setForm(null);
    setStatus(null);
    setError("");
    setNotice("");
    setBusy(false);
    void Promise.all([
      api<Settings>(base + "/settings"),
      api<{ connections: { id: string; name: string; provider: string }[] }>(
        `/projects/${project}/model-connections`,
      ),
    ])
      .then(async ([settings, catalog]) => {
        if (n !== epoch.current) return;
        setForm(settings);
        setConnections(catalog.connections);
        await refresh();
      })
      .catch((e) => {
        if (n === epoch.current) setError(e.message);
      });
    return () => {
      epoch.current++;
    };
  }, [project, analysis, release]);
  useEffect(() => {
    if (status?.state !== "building") return;
    const timer = setInterval(
      () => void refresh().catch((e) => setError(e.message)),
      2000,
    );
    return () => clearInterval(timer);
  }, [status?.state, project, analysis, release]);
  async function save(build = false) {
    if (!form) return;
    const n = epoch.current;
    setBusy(true);
    setError("");
    try {
      await api(base + "/settings", mutation("POST", form));
      if (n !== epoch.current) return;
      if (build && payload.current) {
        await api(base + "/documents/build", mutation("POST", payload.current));
        if (n !== epoch.current) return;
        setNotice(
          "文档索引任务已提交，可在此查看进度；普通 Markdown 无需导入原型即可建库。",
        );
      }
      await refresh();
    } catch (e) {
      if (n === epoch.current)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (n === epoch.current) setBusy(false);
    }
  }
  const building = status?.state === "building",
    percent = status?.total
      ? Math.min(100, Math.round((status.completed / status.total) * 100))
      : 0;
  return (
    <section className="rag-settings" aria-label="RAG 向量知识库">
      <div className="rag-heading">
        <div>
          <Database size={21} />
          <h2>RAG 向量知识库</h2>
        </div>
        <span className="pill">
          {building
            ? "建库中"
            : status?.state === "ready"
              ? "混合检索可用"
              : status?.state === "failed"
                ? "需要重试"
                : "待建立索引"}
        </span>
      </div>
      <p>
        把项目文档建立为本地向量索引，结合关键词检索理解模块和按钮的含义。相关章节先显示，AI
        在后台核对和讲解。
      </p>
      {form && (
        <>
          <div className="rag-grid">
            <label>
              嵌入模型连接
              <select
                aria-label="嵌入模型连接"
                value={form.connection_id || ""}
                onChange={(e) =>
                  setForm({ ...form, connection_id: e.target.value || null })
                }
              >
                <option value="">使用当前项目连接</option>
                {connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.provider}
                  </option>
                ))}
              </select>
            </label>
            <label>
              嵌入模型
              <select
                aria-label="嵌入模型"
                value={form.model_id}
                onChange={(e) => setForm({ ...form, model_id: e.target.value })}
              >
                <option value="text-embedding-3-small">
                  text-embedding-3-small
                </option>
                <option value="text-embedding-3-large">
                  text-embedding-3-large
                </option>
                {!["text-embedding-3-small", "text-embedding-3-large"].includes(
                  form.model_id,
                ) && <option>{form.model_id}</option>}
              </select>
            </label>
            <label>
              向量维度
              <select
                aria-label="向量维度"
                value={form.dimensions}
                onChange={(e) =>
                  setForm({ ...form, dimensions: Number(e.target.value) })
                }
              >
                <option value={512}>512 · 轻量快速</option>
                <option value={1536}>1536 · 完整小模型维度</option>
                <option
                  value={3072}
                  disabled={form.model_id === "text-embedding-3-small"}
                >
                  3072 · 大模型完整维度
                </option>
              </select>
            </label>
            <label>
              每日嵌入 Token 预算
              <input
                aria-label="每日嵌入 Token 预算"
                type="number"
                min={10000}
                max={100000000}
                value={form.daily_token_limit}
                onChange={(e) =>
                  setForm({
                    ...form,
                    daily_token_limit: Number(e.target.value),
                  })
                }
              />
            </label>
          </div>
          <label className="check-line">
            <input
              aria-label="启用 RAG 混合检索"
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            启用 RAG 混合检索
          </label>
          <div className="rag-progress">
            <div>
              <b>
                {status?.indexed_sections || 0} / {status?.total_sections || 0}{" "}
                章节已索引
              </b>
              <span>
                {status?.indexed_chunks || 0} 向量片段 · 今日{" "}
                {status?.used_tokens?.toLocaleString() || 0} Token
              </span>
            </div>
            {building && (
              <>
                <progress max={100} value={percent} />
                <p>
                  {status.phase} · {status.completed} / {status.total} ·{" "}
                  {percent}%
                </p>
              </>
            )}
          </div>
          {notice && (
            <p className="rag-success" role="status">
              {notice}
            </p>
          )}
          {(error || status?.error || status?.configuration_error) && (
            <p className="error" role="alert">
              {error || status?.error || status?.configuration_error}
            </p>
          )}
          <div className="rag-actions">
            <button
              disabled={busy || building}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await api(base + "/settings", mutation("POST", form));
                  const result = await api<{
                    dimensions: number;
                    elapsed_ms: number;
                  }>(base + "/test", mutation("POST", {}));
                  setNotice(
                    `嵌入连接验证成功：${result.dimensions} 维，${Math.round(result.elapsed_ms)} ms`,
                  );
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              测试嵌入连接
            </button>
            <button
              disabled={busy || building}
              onClick={() => void save(false)}
            >
              <Save size={15} />
              保存检索设置
            </button>
            <button
              className="primary"
              disabled={
                busy ||
                building ||
                !form.enabled ||
                !payload.current ||
                !!release
              }
              onClick={() => void save(true)}
            >
              <Database size={15} />
              {building
                ? "正在建立索引…"
                : status?.indexed_chunks
                  ? "更新 / 继续索引"
                  : "建立向量索引"}
            </button>
            <button
              disabled={busy}
              onClick={() => void refresh().catch((e) => setError(e.message))}
            >
              <RefreshCw size={15} />
              刷新状态
            </button>
          </div>
          <small>
            {release &&
              "当前为只读快照，可查看该版本索引状态；请回到项目当前草稿更新索引。 "}
            建库会把获授权的文档片段发送到所选嵌入服务，并产生模型用量。向量保存在本机，修改文档后可增量更新；失败后重试会保留已完成的片段。嵌入预算与回答模型分开统计。
          </small>
        </>
      )}
    </section>
  );
}
