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
import { preferredAnswerModels } from "../../web/src/modelChoices";
import {
  useDisplaySpeed,
  saveDisplaySpeed,
  type DisplaySpeed,
} from "./chatPreferences";

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
  max_input_tokens: number;
  association_max_input_tokens: number | null;
  association_max_output_tokens: number | null;
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
    association_max_input_tokens?: number | null;
    association_max_output_tokens?: number | null;
    max_input_tokens?: number;
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
  const display = useDisplaySpeed();
  const [tab, setTab] = useState<"connection" | "retrieval">("connection");
  const [catalog, setCatalog] = useState<Catalog | null>(null),
    [form, setForm] = useState<Form | null>(null),
    [configured, setConfigured] = useState(false),
    [models, setModels] = useState<Model[] | null>(null),
    [query, setQuery] = useState(""),
    [showAllModels, setShowAllModels] = useState(false),
    [modelPage, setModelPage] = useState(0),
    [custom, setCustom] = useState(false),
    [advancedAddress, setAdvancedAddress] = useState(false),
    [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [test, setTest] = useState<{ ok: boolean; text: string } | null>(null),
    [dirty, setDirty] = useState(false);
  const epoch = useRef(0),
    saving = useRef(false),
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
      max_input_tokens:
        p.settings.max_input_tokens ?? c.policy.max_input_tokens ?? 12000,
      association_max_input_tokens:
        p.settings.association_max_input_tokens ?? null,
      association_max_output_tokens:
        p.settings.association_max_output_tokens ?? null,
      enabled: p.settings.enabled,
      allow_external: p.settings.allow_external,
    });
    setConfigured(p.configured);
    setModels(null);
    setQuery("");
    setShowAllModels(false);
    setModelPage(0);
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
      max_input_tokens: prior ? (c.policy.max_input_tokens ?? 12000) : 12000,
      association_max_input_tokens: prior
        ? (c.policy.association_max_input_tokens ?? null)
        : null,
      association_max_output_tokens: prior
        ? (c.policy.association_max_output_tokens ?? null)
        : null,
      enabled: true,
      allow_external: true,
    });
    setConfigured(legacy);
    setModels(null);
    setQuery("");
    setShowAllModels(false);
    setModelPage(0);
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
    if (!form || saving.current) return;
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
      setShowAllModels(false);
      setModelPage(0);
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
      setShowAllModels(false);
      setModelPage(0);
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
    if (!catalog || saving.current) return;
    saving.current = true;
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
      saving.current = false;
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
  const allModels = models || provider?.models || [];
  const expandedModels = showAllModels || !!query.trim();
  const filtered = (
    expandedModels
      ? allModels
      : preferredAnswerModels(allModels, 6, form.provider)
  ).filter((m) =>
    (m.id + " " + m.name).toLowerCase().includes(query.toLowerCase()),
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 20)),
    page = Math.min(modelPage, pages - 1);
  const choices = filtered.slice(page * 20, page * 20 + 20);
  const missingSelected =
    form.model_id && !choices.some((m) => m.id === form.model_id);
  const canConnect =
    !!form.endpoint.trim() && (configured || !!form.api_key.trim());
  return (
    <section className="ai-settings ai-redesign">
      <div className="ai-title-row">
        <div>
          <span className="eyebrow">项目设置 / 模型服务</span>
          <h1>AI 设置</h1>
          <p>原型助手、文档问答与知识关联共用本项目的连接和检索设置。</p>
        </div>
        <span className={"ai-save-state " + (dirty ? "unsaved" : "")}>
          <i />
          {dirty ? "有未保存的修改" : "连接设置"}
        </span>
      </div>
      <div className="ai-settings-tabs" role="tablist" aria-label="AI 设置分类">
        <button
          type="button"
          id="ai-connection-tab"
          role="tab"
          aria-selected={tab === "connection"}
          aria-controls="ai-connection-panel"
          onClick={() => setTab("connection")}
        >
          模型连接
        </button>
        <button
          type="button"
          id="ai-retrieval-tab"
          role="tab"
          aria-selected={tab === "retrieval"}
          aria-controls="ai-retrieval-panel"
          onClick={() => setTab("retrieval")}
        >
          知识检索
        </button>
      </div>
      <div
        id="ai-connection-panel"
        role="tabpanel"
        aria-labelledby="ai-connection-tab"
        hidden={tab !== "connection"}
      >
        <div className="ai-layout">
          <aside className="ai-connections">
            <div className="connection-list-heading">
              <b>我的连接</b>
              <button
                type="button"
                className="icon"
                aria-label="新增连接"
                disabled={busy === "save"}
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
                disabled={busy === "save"}
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
            <div className="connection-side-note">
              <KeyRound size={15} />
              <span>不同连接分别保存密钥，切换时无需重新填写。</span>
            </div>
          </aside>
          <form className="ai-connection-form" onSubmit={save}>
            <fieldset className="connection-fields" disabled={busy === "save"}>
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
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setModelPage(0);
                    }}
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
                      {models && !allModels.some((m) => m.id === form.model_id)
                        ? "当前配置（列表未返回）"
                        : "当前选择"}
                    </option>
                  )}
                  {!form.model_id && !custom && (
                    <option value="">请选择模型</option>
                  )}
                  <optgroup
                    label={
                      expandedModels
                        ? "全部模型 · 当前页"
                        : "常用回答模型 · 版本优先"
                    }
                  >
                    {choices.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </optgroup>
                  <option value="__custom__">＋ 自定义模型 ID…</option>
                </select>
                <div className="model-list-controls">
                  <button
                    type="button"
                    onClick={() => {
                      setShowAllModels(!expandedModels);
                      setQuery("");
                      setModelPage(0);
                    }}
                  >
                    {expandedModels
                      ? "收起其他模型"
                      : `查看全部模型（${allModels.length}）`}
                  </button>
                  {expandedModels && pages > 1 && (
                    <span>
                      <button
                        type="button"
                        disabled={page === 0}
                        onClick={() => setModelPage(page - 1)}
                      >
                        上一页
                      </button>
                      <small>
                        {page + 1} / {pages}
                      </small>
                      <button
                        type="button"
                        disabled={page === pages - 1}
                        onClick={() => setModelPage(page + 1)}
                      >
                        下一页
                      </button>
                    </span>
                  )}
                </div>
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
                    ? `已获取 ${models.length} 个模型，默认精简展示较新回答模型；其他模型可展开或搜索。`
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
                    {![1500, 2500, 4000, 8000].includes(
                      form.max_output_tokens,
                    ) && (
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
                <label className="answer-display-field">
                  回答显示方式
                  <select
                    aria-label="回答显示方式"
                    value={display}
                    onChange={(event) =>
                      saveDisplaySpeed(event.target.value as DisplaySpeed)
                    }
                  >
                    <option value="normal">逐字显示 · 默认</option>
                    <option value="fast">逐字显示 · 更快</option>
                    <option value="instant">直接显示已收到内容</option>
                  </select>
                  <small>本机显示偏好，自动保存</small>
                </label>
              </div>
              <details className="ai-budget">
                <summary>高级设置</summary>
                <label>
                  问答输入上限（tokens）
                  <input
                    aria-label="问答输入上限"
                    type="number"
                    min={1000}
                    max={1050000}
                    value={form.max_input_tokens}
                    onChange={(e) =>
                      change({ max_input_tokens: Number(e.target.value) })
                    }
                  />
                </label>
                <div className="ai-output-row">
                  <label>
                    关联文档输入上限（tokens）
                    <input
                      aria-label="关联文档输入上限"
                      type="number"
                      min={1000}
                      max={1050000}
                      value={
                        form.association_max_input_tokens ??
                        (/^gpt-6-luna(?:-|$)/.test(form.model_id)
                          ? 128000
                          : form.max_input_tokens)
                      }
                      onChange={(e) =>
                        change({
                          association_max_input_tokens: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    关联文档输出上限（tokens）
                    <input
                      aria-label="关联文档输出上限"
                      type="number"
                      min={100}
                      max={128000}
                      value={
                        form.association_max_output_tokens ??
                        (/^gpt-6-luna(?:-|$)/.test(form.model_id)
                          ? 16000
                          : form.max_output_tokens)
                      }
                      onChange={(e) =>
                        change({
                          association_max_output_tokens: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                </div>
                <p className="model-availability">
                  保存即生效，无需重启。关联文档预算独立于问答和 AI 讲解；Luna
                  默认输入 128,000、输出 16,000
                  tokens，可按复杂度提高，始终预留总上下文余量。输入使用本地预估，实际用量以服务商返回为准。
                </p>
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

                <label className="check-line">
                  <input
                    type="checkbox"
                    checked={form.allow_external}
                    onChange={(e) =>
                      change({ allow_external: e.target.checked })
                    }
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
                  className={
                    "ai-test-result " + (test.ok ? "success" : "failure")
                  }
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
            </fieldset>
          </form>
        </div>
      </div>
      <div
        id="ai-retrieval-panel"
        role="tabpanel"
        aria-labelledby="ai-retrieval-tab"
        hidden={tab !== "retrieval"}
      >
        <RAGSettings project={project} analysis={analysis} release={release} />
      </div>
    </section>
  );
}
