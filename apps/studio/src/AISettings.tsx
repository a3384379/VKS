import { useEffect, useRef, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  KeyRound,
  Plus,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Wifi,
  X,
} from "lucide-react";
import { api, mutation } from "../../web/src/api";
import { RAGSettings } from "./RAGSettings";

type Model = { id: string; name: string; description: string };
type Provider = {
  id: string;
  name: string;
  description: string;
  endpoint: string;
  protocol: string;
  models: Model[];
};
type Form = {
  connection_id: string | null;
  provider: string;
  name: string;
  endpoint: string;
  protocol: string;
  model_id: string;
  api_key: string;
  max_output_tokens: number;
  daily_token_limit: number;
  enabled: boolean;
  allow_external: boolean;
};
type Profile = {
  id: string;
  name: string;
  provider: string;
  settings: Omit<Form, "api_key" | "provider" | "connection_id" | "name">;
  configured: boolean;
  active: boolean;
};
type Catalog = {
  providers: Provider[];
  connections: Profile[];
  policy: {
    row_version: number;
    endpoint: string;
    protocol: string;
    model_id: string;
    max_output_tokens: number;
    daily_token_limit: number;
    enabled: boolean;
    allow_external: boolean;
  };
  legacy_configured: boolean;
};
const initials: Record<string, string> = {
  openai: "O",
  deepseek: "D",
  qwen: "Q",
  custom: "＋",
};
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
export function AISettings({
  project,
  analysis,
  release,
}: {
  project: string;
  analysis: string;
  release: string;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null),
    [form, setForm] = useState<Form | null>(null),
    [configured, setConfigured] = useState(false),
    [models, setModels] = useState<Model[] | null>(null),
    [query, setQuery] = useState(""),
    [custom, setCustom] = useState(false),
    [advancedAddress, setAdvancedAddress] = useState(false),
    [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [test, setTest] = useState<{ ok: boolean; text: string } | null>(null),
    [dirty, setDirty] = useState(false);
  const epoch = useRef(0),
    base = "/projects/" + project + "/model-connections";
  const provider = catalog?.providers.find((p) => p.id === form?.provider);
  function openProfile(c: Catalog, p: Profile) {
    const preset = c.providers.find((x) => x.id === p.provider)!;
    epoch.current++;
    setForm({
      connection_id: p.id,
      provider: p.provider,
      name: p.name,
      endpoint: p.settings.endpoint,
      protocol: p.settings.protocol,
      model_id: p.settings.model_id,
      api_key: "",
      max_output_tokens: p.settings.max_output_tokens,
      daily_token_limit: p.settings.daily_token_limit,
      enabled: p.settings.enabled,
      allow_external: p.settings.allow_external,
    });
    setConfigured(p.configured);
    setModels(null);
    setQuery("");
    setCustom(false);
    setAdvancedAddress(
      p.provider === "custom" || p.settings.endpoint !== preset.endpoint,
    );
    setTest(null);
    setDirty(false);
    setError("");
    setNotice("");
    setBusy("");
  }
  function chooseProvider(c: Catalog, p: Provider, forceNew = false) {
    const saved = !forceNew && c.connections.find((x) => x.provider === p.id);
    if (saved) {
      openProfile(c, saved);
      return;
    }
    epoch.current++;
    const legacy = p.id === "openai" && c.legacy_configured;
    const prior =
      legacy && (!c.policy.endpoint || c.policy.endpoint === p.endpoint);
    setForm({
      connection_id: null,
      provider: p.id,
      name: p.name,
      endpoint: p.endpoint,
      protocol: prior ? c.policy.protocol : p.protocol,
      model_id:
        prior && c.policy.model_id ? c.policy.model_id : p.models[0]?.id || "",
      api_key: "",
      max_output_tokens: prior ? c.policy.max_output_tokens : 2500,
      daily_token_limit: prior ? c.policy.daily_token_limit : 100000,
      enabled: true,
      allow_external: true,
    });
    setConfigured(legacy);
    setModels(null);
    setQuery("");
    setCustom(false);
    setAdvancedAddress(p.id === "custom");
    setTest(null);
    setDirty(false);
    setError("");
    setNotice("");
    setBusy("");
  }
  useEffect(() => {
    let alive = true;
    setCatalog(null);
    setForm(null);
    setError("");
    api<Catalog>(base)
      .then((c) => {
        if (!alive) return;
        setCatalog(c);
        const active = c.connections.find((p) => p.active) || c.connections[0];
        if (active) openProfile(c, active);
        else
          chooseProvider(
            c,
            c.providers.find(
              (p) => p.endpoint && p.endpoint === c.policy.endpoint,
            ) || c.providers[0],
          );
      })
      .catch((e) => {
        if (alive) setError(message(e));
      });
    return () => {
      alive = false;
      epoch.current++;
    };
  }, [base]);
  useEffect(() => {
    // Discover saved connections without inference or sending project content.
    // Never query a partially entered key or an unsaved edited endpoint.
    if (configured && form && !dirty && !form.api_key.trim()) void discover();
  }, [base, form?.connection_id, form?.endpoint, configured]);
  function change(patch: Partial<Form>) {
    if (!form) return;
    epoch.current++;
    setForm({ ...form, ...patch });
    setDirty(true);
    setTest(null);
    setNotice("");
    setBusy("");
    if (
      patch.api_key !== undefined ||
      patch.endpoint !== undefined ||
      patch.protocol !== undefined
    ) {
      setModels(null);
      setQuery("");
    }
    if (patch.endpoint !== undefined) {
      setConfigured(false);
    }
  }
  function payload() {
    if (!form) throw new Error("连接尚未加载");
    const { api_key, ...rest } = form;
    return {
      ...rest,
      endpoint: form.endpoint.trim(),
      model_id: form.model_id.trim(),
      ...(api_key.trim() ? { api_key: api_key.trim() } : {}),
    };
  }
  async function discover() {
    const current = ++epoch.current;
    setBusy("models");
    setError("");
    setNotice("");
    try {
      const response = await api<{ models: Model[] }>(
        base + "/models",
        mutation("POST", payload()),
      );
      if (current !== epoch.current) return;
      setModels(response.models);
      setQuery("");
      setNotice(`已获取当前 Key 可见的 ${response.models.length} 个模型`);
    } catch (e) {
      if (current === epoch.current) setError(message(e));
    } finally {
      if (current === epoch.current) setBusy("");
    }
  }
  async function probe() {
    const current = ++epoch.current;
    setBusy("test");
    setError("");
    setTest(null);
    try {
      const result = await api<{
        ok: boolean;
        latency_ms: number;
        model_id: string;
      }>(base + "/test", mutation("POST", payload()));
      if (current !== epoch.current) return;
      setTest({
        ok: true,
        text: `${result.model_id} 已返回测试回答 · ${result.latency_ms} ms`,
      });
    } catch (e) {
      if (current === epoch.current) setTest({ ok: false, text: message(e) });
    } finally {
      if (current === epoch.current) setBusy("");
    }
  }
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!catalog) return;
    const current = ++epoch.current;
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const next = await api<Catalog>(
        base,
        mutation("POST", payload(), catalog.policy.row_version),
      );
      if (current !== epoch.current) return;
      setCatalog(next);
      const active = next.connections.find((c) => c.active);
      if (active) openProfile(next, active);
      setNotice("连接已保存，当前项目将使用所选模型。");
    } catch (e) {
      if (current === epoch.current) setError(message(e));
    } finally {
      if (current === epoch.current) setBusy("");
    }
  }
  if (!catalog || !form)
    return (
      <section className="ai-settings ai-redesign">
        <h1>AI 设置</h1>
        {error ? (
          <p className="settings-message error" role="alert">
            {error}
          </p>
        ) : (
          <p className="subtle">正在读取模型连接…</p>
        )}
      </section>
    );
  const selected =
    (models || provider?.models || []).find((m) => m.id === form.model_id) ||
    provider?.models.find((m) => m.id === form.model_id);
  const filtered = (models || provider?.models || []).filter((m) =>
    (m.id + " " + m.name).toLowerCase().includes(query.toLowerCase()),
  );
  const missingSelected =
    form.model_id && !filtered.some((m) => m.id === form.model_id);
  const canConnect =
    !!form.endpoint.trim() && (configured || !!form.api_key.trim());
  return (
    <section className="ai-settings ai-redesign">
      <div className="ai-title-row">
        <div>
          <span className="eyebrow">项目设置 / 模型服务</span>
          <h1>连接你的 AI</h1>
          <p>选择服务商和回答模型，让知识关联与功能讲解使用合适的模型。</p>
        </div>
        <span className={"ai-save-state " + (dirty ? "unsaved" : "")}>
          <i />
          {dirty ? "有未保存的修改" : "连接设置"}
        </span>
      </div>
      <div className="ai-layout">
        <aside className="ai-connections">
          <div className="connection-list-heading">
            <b>我的连接</b>
            <button
              type="button"
              className="icon"
              aria-label="新增连接"
              onClick={() =>
                chooseProvider(catalog, catalog.providers[0], true)
              }
            >
              <Plus size={16} />
            </button>
          </div>
          {catalog.connections.map((c) => (
            <button
              type="button"
              key={c.id}
              className={
                "connection-item " +
                (form.connection_id === c.id ? "selected" : "")
              }
              onClick={() => openProfile(catalog, c)}
            >
              <span className={"provider-mark " + c.provider}>
                {initials[c.provider]}
              </span>
              <span>
                <b>{c.name}</b>
                <small>{c.settings.model_id}</small>
              </span>
              {c.active && <Check size={14} />}
            </button>
          ))}
          {!catalog.connections.length && (
            <p className="connections-empty">
              {catalog.legacy_configured
                ? "已保留你原有的 OpenAI Key，选择模型后即可统一保存。"
                : "添加一个模型连接，随后可以随时切换。"}
            </p>
          )}
          <div className="connection-list-heading new">
            <b>添加服务商</b>
          </div>
          {catalog.providers.map((p) => (
            <button
              type="button"
              key={p.id}
              className="provider-shortcut"
              onClick={() => chooseProvider(catalog, p)}
            >
              <span className={"provider-mark small " + p.id}>
                {initials[p.id]}
              </span>
              {p.name}
              <Plus size={13} />
            </button>
          ))}
          <div className="connection-side-note">
            <KeyRound size={15} />
            <span>不同连接分别保存密钥，切换时无需重新填写。</span>
          </div>
        </aside>
        <form className="ai-connection-form" onSubmit={save}>
          <label className="mobile-connection-picker">
            切换连接
            <select
              aria-label="切换已保存连接"
              value={form.connection_id || ""}
              onChange={(e) => {
                const profile = catalog.connections.find(
                  (c) => c.id === e.target.value,
                );
                if (profile) openProfile(catalog, profile);
                else
                  chooseProvider(
                    catalog,
                    provider || catalog.providers[0],
                    true,
                  );
              }}
            >
              <option value="">＋ 新建连接</option>
              {catalog.connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} · {c.settings.model_id}
                </option>
              ))}
            </select>
          </label>

          <div className="connection-form-top">
            <span className={"provider-mark large " + form.provider}>
              {initials[form.provider]}
            </span>
            <div>
              <h2>{provider?.name}</h2>
              <p>{provider?.description}</p>
            </div>
            {configured && (
              <span className="key-kept">
                <CheckCircle2 size={14} />
                已保留密钥
              </span>
            )}
          </div>
          <div className="ai-provider-pills" aria-label="选择模型服务商">
            {catalog.providers.map((p) => (
              <button
                type="button"
                key={p.id}
                className={p.id === form.provider ? "chosen" : ""}
                onClick={() => {
                  if (p.id !== form.provider) chooseProvider(catalog, p);
                }}
              >
                {p.name}
              </button>
            ))}
          </div>
          {error && (
            <div className="settings-message error" role="alert">
              <X size={15} />
              {error}
            </div>
          )}
          {notice && (
            <div className="settings-message" role="status">
              <CheckCircle2 size={15} />
              {notice}
            </div>
          )}
          <div className="ai-field-block">
            <div className="field-heading">
              <label htmlFor="ai-provider-key">API Key</label>
              <span>
                {configured
                  ? "已配置 · 留空则继续使用"
                  : provider?.name + " 的访问密钥"}
              </span>
            </div>
            <div className="key-input-wrap">
              <KeyRound size={16} />
              <input
                id="ai-provider-key"
                aria-label="API Key"
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={form.api_key}
                placeholder={
                  configured ? "更换 Key 时填写" : "粘贴此服务商的 API Key"
                }
                onChange={(e) => change({ api_key: e.target.value })}
              />
              {configured && !form.api_key && <CheckCircle2 size={16} />}
            </div>
          </div>
          <div className="endpoint-summary">
            <span>{form.endpoint || "填写自定义服务地址"}</span>
            <button
              type="button"
              onClick={() => setAdvancedAddress(!advancedAddress)}
            >
              {advancedAddress ? "收起连接参数" : "修改地址 / 协议"}
              <ChevronDown size={12} />
            </button>
          </div>
          {advancedAddress && (
            <div className="connection-advanced">
              <label>
                接口地址
                <input
                  aria-label="模型接口地址"
                  placeholder="https://your-provider.example/v1"
                  value={form.endpoint}
                  onChange={(e) => change({ endpoint: e.target.value })}
                />
              </label>
              <label>
                协议
                <select
                  aria-label="模型协议"
                  value={form.protocol}
                  onChange={(e) => change({ protocol: e.target.value })}
                >
                  <option value="responses">Responses API</option>
                  <option value="chat_completions">
                    OpenAI 兼容 Chat Completions
                  </option>
                </select>
              </label>
              <label>
                连接名称
                <input
                  aria-label="连接名称"
                  value={form.name}
                  onChange={(e) => change({ name: e.target.value })}
                />
              </label>
            </div>
          )}
          <div className="ai-field-block model-field">
            <div className="field-heading">
              <label htmlFor="ai-model-select">回答模型</label>
              <button
                type="button"
                className="model-refresh"
                disabled={!!busy || !canConnect}
                onClick={() => void discover()}
              >
                <RefreshCw
                  size={13}
                  className={busy === "models" ? "spinning" : ""}
                />
                {busy === "models" ? "正在查询…" : "获取可用模型"}
              </button>
            </div>
            <div className="model-picker-search">
              <Search size={14} />
              <input
                aria-label="搜索模型"
                placeholder="搜索模型名称…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <span>
                {models
                  ? "账户模型"
                  : busy === "models"
                    ? "正在查询"
                    : "参考模型"}
              </span>
            </div>
            <select
              id="ai-model-select"
              aria-label="回答模型"
              value={custom ? "__custom__" : form.model_id}
              onChange={(e) => {
                if (e.target.value === "__custom__") {
                  setCustom(true);
                  change({ model_id: "" });
                } else {
                  setCustom(false);
                  change({ model_id: e.target.value });
                }
              }}
            >
              {missingSelected && !custom && (
                <option value={form.model_id}>
                  {form.model_id} ·{" "}
                  {models ? "当前配置（列表未返回）" : "当前配置"}
                </option>
              )}
              {!form.model_id && !custom && (
                <option value="">请选择模型</option>
              )}
              <optgroup
                label={
                  models
                    ? "服务返回的模型 ID"
                    : "参考模型 ID（尚未验证账户权限）"
                }
              >
                {filtered.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.id}
                  </option>
                ))}
              </optgroup>
              <option value="__custom__">＋ 自定义模型 ID…</option>
            </select>
            {custom && (
              <input
                aria-label="自定义模型 ID"
                className="custom-model-input"
                placeholder="填写服务提供的精确模型 ID"
                value={form.model_id}
                onChange={(e) => change({ model_id: e.target.value })}
              />
            )}
            <div className="model-description">
              <Sparkles size={15} />
              <span>
                {selected?.description ||
                  "模型的实际可用性由服务商和此 Key 的权限决定。"}
              </span>
            </div>
            <p className="model-availability">
              {models
                ? `已从服务获取 ${models.length} 个模型。可见模型的协议与能力可能不同，可测试当前选择。`
                : "以下仅为参考 ID，尚未验证账户权限。获取可用模型后，以服务返回的列表为准。"}
            </p>
          </div>
          <div className="ai-output-row">
            <label>
              回答长度
              <select
                aria-label="回答长度"
                value={form.max_output_tokens}
                onChange={(e) =>
                  change({ max_output_tokens: Number(e.target.value) })
                }
              >
                {![1500, 2500, 4000, 8000].includes(form.max_output_tokens) && (
                  <option value={form.max_output_tokens}>
                    当前设置 · {form.max_output_tokens} tokens
                  </option>
                )}
                <option value={1500}>简洁 · 1,500 tokens</option>
                <option value={2500}>标准 · 2,500 tokens</option>
                <option value={4000}>详细 · 4,000 tokens</option>
                <option value={8000}>深入分析 · 8,000 tokens</option>
              </select>
            </label>
            <label className="project-ai-toggle">
              <span>项目 AI</span>
              <span>
                <input
                  aria-label="启用项目 AI"
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => change({ enabled: e.target.checked })}
                />
                {form.enabled ? "已启用" : "已关闭"}
              </span>
            </label>
          </div>
          <details className="ai-budget">
            <summary>更多设置</summary>
            <label>
              每日 Token 预算
              <input
                type="number"
                min={1000}
                max={100000000}
                value={form.daily_token_limit}
                onChange={(e) =>
                  change({ daily_token_limit: Number(e.target.value) })
                }
              />
            </label>
            <label className="check-line">
              <input
                type="checkbox"
                checked={form.allow_external}
                onChange={(e) => change({ allow_external: e.target.checked })}
              />
              允许 AI 使用本项目获授权的相关内容
            </label>
            <p>
              密钥保存在本机实例的数据目录。点击“保存并使用”将保存当前连接及填写的
              Key。
            </p>
          </details>
          {test && (
            <div
              className={"ai-test-result " + (test.ok ? "success" : "failure")}
              role="status"
            >
              {test.ok ? <CheckCircle2 size={17} /> : <X size={17} />}
              <span>{test.text}</span>
            </div>
          )}
          <footer className="ai-connection-footer">
            <div>
              <button
                type="button"
                disabled={!!busy || !canConnect || !form.model_id.trim()}
                onClick={() => void probe()}
              >
                <Wifi size={15} />
                {busy === "test" ? "正在测试…" : "测试连接"}
              </button>
              <small>发送一条固定测试消息，产生少量模型用量</small>
            </div>
            <button
              className="primary"
              type="submit"
              disabled={!!busy || !canConnect || !form.model_id.trim()}
            >
              <Save size={15} />
              {busy === "save" ? "正在保存…" : "保存并使用"}
            </button>
          </footer>
        </form>
      </div>
      <RAGSettings project={project} analysis={analysis} release={release} />
    </section>
  );
}
