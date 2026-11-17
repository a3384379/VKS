import { useEffect, useRef, useState } from "react";
import { Database, RefreshCw, Save } from "lucide-react";
import { api, mutation } from "../../web/src/api";
type Settings = {
  enabled: boolean;
  connection_id: string | null;
  model_id: string;
  dimensions: number;
  candidate_limit?: number;
  per_document_limit?: number;
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
    try {
      const s = await api<Status>(
        base + "/documents/status",
        mutation("POST", payload.current),
      );
      if (n === epoch.current) setStatus(s);
    } catch (error) {
      if (n === epoch.current) throw error;
    }
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
          <h2>知识检索</h2>
        </div>
        <span className="pill">
          {building
            ? "建库中"
            : form?.enabled === false
              ? "混合检索已关闭"
              : status?.state === "ready"
                ? "混合检索可用"
                : status?.state === "failed"
                  ? "需要重试"
                  : "待建立索引"}
        </span>
      </div>
      <p>
        原型助手和文档问答共用项目的向量与关键词索引。文档变更后可增量更新，已有片段会继续复用。
      </p>
      {form && (
        <>
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
          <div className="rag-actions rag-main-actions">
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
          {release && (
            <p className="rag-hint">
              当前为只读快照，可查看该版本索引状态；请回到项目当前草稿更新索引。
            </p>
          )}
          <details className="rag-parameters">
            <summary>
              索引与召回参数{" "}
              <span>
                {form.model_id} · {form.dimensions} 维
              </span>
            </summary>
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
                  onChange={(e) =>
                    setForm({ ...form, model_id: e.target.value })
                  }
                >
                  <option value="text-embedding-3-small">
                    text-embedding-3-small
                  </option>
                  <option value="text-embedding-3-large">
                    text-embedding-3-large
                  </option>
                  {![
                    "text-embedding-3-small",
                    "text-embedding-3-large",
                  ].includes(form.model_id) && <option>{form.model_id}</option>}
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
                混合检索候选章节数
                <input
                  aria-label="混合检索候选章节数"
                  type="number"
                  min={6}
                  max={64}
                  value={form.candidate_limit ?? 24}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      candidate_limit: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label>
                单篇文档候选上限
                <input
                  aria-label="单篇文档候选上限"
                  type="number"
                  min={1}
                  max={24}
                  value={form.per_document_limit ?? 8}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      per_document_limit: Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>
            <p className="rag-hint">
              候选数影响召回范围，不代表最终引用数量。调整这两项无需重新建立向量索引；重新关联按当前底稿与完整原文章节生成预览。
            </p>
            <label className="check-line">
              <input
                aria-label="启用 RAG 混合检索"
                type="checkbox"
                checked={form.enabled}
                onChange={(e) =>
                  setForm({ ...form, enabled: e.target.checked })
                }
              />
              启用 RAG 混合检索
            </label>
            <div className="rag-actions">
              <button
                disabled={busy || building}
                onClick={() => void save(false)}
              >
                <Save size={15} />
                保存检索设置
              </button>
              <button
                disabled={busy || building}
                onClick={async () => {
                  const n = epoch.current;
                  setBusy(true);
                  setError("");
                  try {
                    await api(base + "/settings", mutation("POST", form));
                    if (n !== epoch.current) return;
                    const result = await api<{
                      dimensions: number;
                      elapsed_ms: number;
                    }>(base + "/test", mutation("POST", {}));
                    if (n !== epoch.current) return;
                    setNotice(
                      `嵌入连接验证成功：${result.dimensions} 维，${Math.round(result.elapsed_ms)} ms`,
                    );
                  } catch (e) {
                    if (n === epoch.current)
                      setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    if (n === epoch.current) setBusy(false);
                  }
                }}
              >
                测试嵌入连接
              </button>
            </div>
            <small>
              建库会把获授权的文档片段发送到所选嵌入服务，并产生模型用量。向量保存在本机，修改文档后可增量更新；失败后重试会保留已完成的片段。嵌入与回答模型的调用用量分别记录。
            </small>
          </details>
        </>
      )}
    </section>
  );
}
