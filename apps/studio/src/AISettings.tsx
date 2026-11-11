import { useEffect, useState } from "react";
import {
  KeyRound,
  Save,
  RefreshCw,
  CheckCircle2,
  Settings2,
} from "lucide-react";
import { api, mutation } from "../../web/src/api";

type Credential = {
  configured: boolean;
  storage_path: string;
  env_name: string;
};
type Policy = {
  enabled: boolean;
  protocol: string;
  endpoint: string;
  model_id: string;
  secret_ref: string | null;
  allow_external: boolean;
  allow_source: boolean;
  max_input_tokens: number;
  max_output_tokens: number;
  daily_token_limit: number;
  per_request_cost_limit_micro: number;
  daily_cost_limit_micro: number;
  input_rate_micro_per_million: number;
  output_rate_micro_per_million: number;
  timezone: string;
  row_version: number;
  configured_secret: boolean;
};
const fields = [
  "enabled",
  "protocol",
  "endpoint",
  "model_id",
  "secret_ref",
  "allow_external",
  "allow_source",
  "max_input_tokens",
  "max_output_tokens",
  "daily_token_limit",
  "per_request_cost_limit_micro",
  "daily_cost_limit_micro",
  "input_rate_micro_per_million",
  "output_rate_micro_per_million",
  "timezone",
] as const;
export function AISettings({ project }: { project: string }) {
  const [policy, setPolicy] = useState<Policy | null>(null),
    [credential, setCredential] = useState<Credential | null>(null),
    [key, setKey] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const base = "/projects/" + project;
  useEffect(() => {
    let alive = true;
    setPolicy(null);
    setCredential(null);
    setKey("");
    setConfirmed(false);
    setError("");
    Promise.all([
      api<Policy>(base + "/model-policy"),
      api<Credential>(base + "/model-credential"),
    ])
      .then(([p, c]) => {
        if (!alive) return;
        setCredential(c);
        setPolicy({
          ...p,
          endpoint: p.endpoint || "https://api.openai.com/v1",
          secret_ref: "model-default",
        });
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [base]);
  async function saveKey() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const c = await api<Credential>(
        base + "/model-credential",
        mutation("POST", { api_key: key, confirm_local_storage: confirmed }),
      );
      setCredential(c);
      setKey("");
      setConfirmed(false);
      setNotice("API Key 已保存并在本机生效。页面不会回显密钥。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function savePolicy(e: React.FormEvent) {
    e.preventDefault();
    if (!policy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const body = Object.fromEntries(fields.map((f) => [f, policy[f]]));
      const p = await api<Policy>(
        base + "/model-policy",
        mutation("PUT", body, policy.row_version),
      );
      setPolicy({ ...p, configured_secret: !!credential?.configured });
      setNotice("AI 设置已保存。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="ai-settings">
      <div className="settings-heading">
        <span className="eyebrow">项目设置</span>
        <h1>
          <Settings2 size={26} />
          AI 设置
        </h1>
        <p>
          配置你已有的模型服务。API Key 由本机后端保存，项目页面只显示配置状态。
        </p>
      </div>
      {error && (
        <p className="settings-message error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="settings-message" role="status">
          <CheckCircle2 size={16} />
          {notice}
        </p>
      )}
      <div className="settings-columns">
        <article className="settings-card">
          <h2>
            <KeyRound size={18} />
            API Key
          </h2>
          {credential ? (
            <>
              <div
                className={
                  "credential-state " + (credential.configured ? "ready" : "")
                }
              >
                <i />
                {credential.configured ? "本机已配置密钥" : "尚未配置密钥"}
              </div>
              <label>
                OpenAI API Key
                <input
                  aria-label="OpenAI API Key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={key}
                  placeholder={
                    credential.configured
                      ? "输入新 Key 可替换当前密钥"
                      : "在此粘贴你已有的 API Key"
                  }
                  onChange={(e) => setKey(e.target.value)}
                />
              </label>
              <p className="storage-note">
                保存位置：<code>{credential.storage_path}</code>
              </p>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
                确认将密钥保存到上述本机文件，供本实例的 AI 功能使用
              </label>
              <button
                className="primary"
                disabled={busy || !key.trim() || !confirmed}
                onClick={() => void saveKey()}
              >
                <Save size={15} />
                {busy
                  ? "保存中…"
                  : credential.configured
                    ? "更新本机 Key"
                    : "保存本机 Key"}
              </button>
              <p className="settings-hint">
                保存后立即生效，重启服务仍会加载。此文件位于实例数据目录，不进入
                Git。此处“已配置”仅表示本机存在密钥，实际连接还需验证。
              </p>
            </>
          ) : (
            <p>正在读取本机配置…</p>
          )}
        </article>
        <form className="settings-card" onSubmit={savePolicy}>
          <h2>
            <RefreshCw size={18} />
            模型连接
          </h2>
          {policy ? (
            <>
              <label>
                接口地址
                <input
                  aria-label="模型接口地址"
                  value={policy.endpoint}
                  onChange={(e) =>
                    setPolicy({ ...policy, endpoint: e.target.value })
                  }
                />
              </label>
              <label>
                协议
                <select
                  aria-label="模型协议"
                  value={policy.protocol}
                  onChange={(e) =>
                    setPolicy({ ...policy, protocol: e.target.value })
                  }
                >
                  <option value="responses">OpenAI Responses</option>
                  <option value="chat_completions">
                    Chat Completions 兼容
                  </option>
                </select>
              </label>
              <label>
                模型名称
                <input
                  aria-label="模型名称"
                  placeholder="填写此 Key 可调用的模型 ID"
                  value={policy.model_id}
                  onChange={(e) =>
                    setPolicy({ ...policy, model_id: e.target.value })
                  }
                />
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={policy.enabled}
                  onChange={(e) =>
                    setPolicy({ ...policy, enabled: e.target.checked })
                  }
                />
                启用本项目的 AI 模型
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={policy.allow_external}
                  onChange={(e) =>
                    setPolicy({ ...policy, allow_external: e.target.checked })
                  }
                />
                允许将获授权的相关内容发送至配置的模型服务
              </label>
              <details>
                <summary>调用预算</summary>
                <label>
                  单次输出 Token 上限
                  <input
                    type="number"
                    min={100}
                    max={16000}
                    value={policy.max_output_tokens}
                    onChange={(e) =>
                      setPolicy({
                        ...policy,
                        max_output_tokens: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  每日 Token 预算
                  <input
                    type="number"
                    min={1000}
                    max={100000000}
                    value={policy.daily_token_limit}
                    onChange={(e) =>
                      setPolicy({
                        ...policy,
                        daily_token_limit: Number(e.target.value),
                      })
                    }
                  />
                </label>
              </details>
              <button className="primary" type="submit" disabled={busy}>
                <Save size={15} />
                保存 AI 设置
              </button>
              <p className="settings-hint">
                默认已登记 OpenAI 官方服务。其他接口地址需在实例允许清单中登记。
              </p>
            </>
          ) : (
            <p>正在读取项目设置…</p>
          )}
        </form>
      </div>
    </section>
  );
}
