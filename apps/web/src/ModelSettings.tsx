import "./configuration.css";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Save, ShieldCheck, Sparkles } from "lucide-react";
import { api, ApiError, date, mutation } from "./api";
import { Button, Empty, Notice, Panel, Spinner, Tag } from "./components";
import { usePermission, useWorkspace } from "./context";

export interface ModelPolicy {
  enabled: boolean;
  protocol: "responses" | "chat_completions";
  endpoint: string;
  model_id: string;
  secret_ref: "model-default" | null;
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
}
interface PolicyResponse extends ModelPolicy {
  row_version: number;
  configured_secret?: boolean;
}
interface ModelUsage {
  id: string;
  model_id: string;
  state: string;
  input_tokens: number;
  output_tokens: number;
  cost_micro: number;
  error_code: string;
  created_at: string;
}

// GET metadata must never be echoed into the strict policy input contract.
export function policyPayload(policy: ModelPolicy): ModelPolicy {
  return {
    enabled: policy.enabled,
    protocol: policy.protocol,
    endpoint: policy.endpoint.trim(),
    model_id: policy.model_id.trim(),
    secret_ref: policy.secret_ref,
    allow_external: policy.allow_external,
    allow_source: policy.allow_source,
    max_input_tokens: policy.max_input_tokens,
    max_output_tokens: policy.max_output_tokens,
    daily_token_limit: policy.daily_token_limit,
    per_request_cost_limit_micro: policy.per_request_cost_limit_micro,
    daily_cost_limit_micro: policy.daily_cost_limit_micro,
    input_rate_micro_per_million: policy.input_rate_micro_per_million,
    output_rate_micro_per_million: policy.output_rate_micro_per_million,
    timezone: policy.timezone.trim(),
  };
}
const limits: {
  key: keyof ModelPolicy;
  label: string;
  min: number;
  max: number;
}[] = [
  {
    key: "max_input_tokens",
    label: "单次输入 token 上限",
    min: 1000,
    max: 128000,
  },
  {
    key: "max_output_tokens",
    label: "单次输出 token 上限",
    min: 100,
    max: 16000,
  },
  {
    key: "daily_token_limit",
    label: "每日 token 预算",
    min: 1000,
    max: 100000000,
  },
  {
    key: "per_request_cost_limit_micro",
    label: "单次费用上限（微单位）",
    min: 0,
    max: 1000000000,
  },
  {
    key: "daily_cost_limit_micro",
    label: "每日费用预算（微单位）",
    min: 0,
    max: 100000000000,
  },
  {
    key: "input_rate_micro_per_million",
    label: "每百万输入 token 费率（微单位）",
    min: 0,
    max: 100000000000,
  },
  {
    key: "output_rate_micro_per_million",
    label: "每百万输出 token 费率（微单位）",
    min: 0,
    max: 100000000000,
  },
];
export function ModelSettings() {
  const { w, notify, refresh } = useWorkspace();
  const canManage = usePermission("project:manage");
  const [policy, setPolicy] = useState<PolicyResponse | null>(null),
    [usage, setUsage] = useState<ModelUsage[]>([]),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const reload = useCallback(async () => {
    if (!canManage) return;
    setLoading(true);
    setError("");
    try {
      const [settings, rows] = await Promise.all([
        api<PolicyResponse>("/projects/" + w.project.id + "/model-policy"),
        api<ModelUsage[]>("/projects/" + w.project.id + "/model-usage"),
      ]);
      setPolicy(settings);
      setUsage(rows);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [canManage, w.project.id]);
  useEffect(() => {
    void reload();
  }, [reload]);
  async function save(e: FormEvent) {
    e.preventDefault();
    if (!policy) return;
    setBusy(true);
    setError("");
    try {
      const next = await api<PolicyResponse>(
        "/projects/" + w.project.id + "/model-policy",
        mutation("PUT", policyPayload(policy), policy.row_version),
      );
      setPolicy({ ...next, configured_secret: policy.configured_secret });
      notify("模型策略已保存；调用仍需提问时明确选择");
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "策略已被其他人更新。当前输入保留，请先复制变更，再重新载入合并。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  if (!canManage)
    return (
      <Panel>
        <Empty
          title="模型策略由项目管理员维护"
          description="日常查询默认使用确定性检索。选择模型辅助需要项目已启用对应策略。"
        />
      </Panel>
    );
  return (
    <div className="configuration-stack">
      <Panel
        title="模型连接与数据范围"
        action={
          <Tag value={policy?.enabled ? "valid" : "static"}>
            {policy?.enabled ? "项目已启用" : "默认关闭"}
          </Tag>
        }
      >
        <Notice>
          <ShieldCheck size={16} />
          模型只收到当前身份有权读取的知识和证据，结果直接保存并标注来源。实例出站允许清单与项目预算共同约束每次调用。
        </Notice>
        {error && (
          <Notice tone="red">
            <p>{error}</p>
            <Button
              kind="small"
              onClick={() => {
                void reload();
              }}
            >
              重新载入策略
            </Button>
          </Notice>
        )}
        {loading ? (
          <Spinner />
        ) : (
          policy && (
            <form
              onSubmit={(e) => {
                void save(e);
              }}
            >
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={policy.enabled}
                  onChange={(e) =>
                    setPolicy({ ...policy, enabled: e.target.checked })
                  }
                />
                启用项目模型辅助
              </label>
              <div className="configuration-fields">
                <label>
                  协议
                  <select
                    value={policy.protocol}
                    onChange={(e) =>
                      setPolicy({
                        ...policy,
                        protocol: e.target.value as ModelPolicy["protocol"],
                      })
                    }
                  >
                    <option value="responses">Responses</option>
                    <option value="chat_completions">Chat Completions</option>
                  </select>
                </label>
                <label>
                  模型标识
                  <input
                    required={policy.enabled}
                    maxLength={160}
                    value={policy.model_id}
                    onChange={(e) =>
                      setPolicy({ ...policy, model_id: e.target.value })
                    }
                    placeholder="由模型服务提供"
                  />
                </label>
                <label className="wide-field">
                  服务基础地址
                  <input
                    type="url"
                    required={policy.enabled}
                    maxLength={2048}
                    value={policy.endpoint}
                    onChange={(e) =>
                      setPolicy({ ...policy, endpoint: e.target.value })
                    }
                    placeholder="实例允许清单中的地址，例如 https://gateway.example/v1"
                  />
                  <small>
                    仅接受实例管理员预先登记的地址；远程地址需
                    HTTPS。此处不保存任何密钥。
                  </small>
                </label>
                <label>
                  凭证引用
                  <select
                    value={policy.secret_ref || ""}
                    onChange={(e) =>
                      setPolicy({
                        ...policy,
                        secret_ref: e.target.value ? "model-default" : null,
                      })
                    }
                  >
                    <option value="">无需凭证</option>
                    <option
                      value="model-default"
                      disabled={!policy.configured_secret}
                    >
                      model-default（
                      {policy.configured_secret ? "实例已配置" : "实例未配置"}）
                    </option>
                  </select>
                </label>
                <label>
                  每日预算时区
                  <input
                    required
                    value={policy.timezone}
                    maxLength={80}
                    onChange={(e) =>
                      setPolicy({ ...policy, timezone: e.target.value })
                    }
                  />
                </label>
              </div>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={policy.allow_external}
                  onChange={(e) =>
                    setPolicy({ ...policy, allow_external: e.target.checked })
                  }
                />
                允许将已授权上下文发送到登记的远程模型服务
              </label>
              <label className="check-line">
                <input
                  type="checkbox"
                  checked={policy.allow_source}
                  onChange={(e) =>
                    setPolicy({ ...policy, allow_source: e.target.checked })
                  }
                />
                允许包含源码上下文（调用者仍需 source:read 权限）
              </label>
              <h3 className="detail-section-title">Token 与费用预算</h3>
              <p className="section-description">
                费率由管理员按服务协议填写，默认 0 表示未计价。1 个计价单位 =
                1,000,000 微单位；此处不提供或推定供应商实时价格。
              </p>
              <div className="configuration-fields">
                {limits.map((field) => (
                  <label key={field.key}>
                    {field.label}
                    <input
                      type="number"
                      step="1"
                      min={field.min}
                      max={field.max}
                      required
                      value={Number(policy[field.key])}
                      onChange={(e) =>
                        setPolicy({
                          ...policy,
                          [field.key]: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
              <div className="configuration-actions">
                <Button kind="primary" type="submit" disabled={busy}>
                  <Save size={15} />
                  保存策略
                </Button>
              </div>
            </form>
          )
        )}
      </Panel>
      <Panel title="最近模型用量" action={<Sparkles size={18} />}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间 / 模型</th>
                <th>状态</th>
                <th>输入 / 输出 token</th>
                <th>费用（计价单位）</th>
                <th>结果代码</th>
              </tr>
            </thead>
            <tbody>
              {usage.map((row) => (
                <tr key={row.id}>
                  <td>
                    {date(row.created_at)}
                    <small>{row.model_id}</small>
                  </td>
                  <td>
                    <Tag value={row.state === "succeeded" ? "valid" : "static"}>
                      {(
                        {
                          not_sent: "未发送（未计费）",
                          succeeded: "已完成",
                          failed: "失败",
                          reserved: "额度已预留",
                        } as Record<string, string>
                      )[row.state] || row.state}
                    </Tag>
                  </td>
                  <td>
                    {row.input_tokens} / {row.output_tokens}
                  </td>
                  <td>{(row.cost_micro / 1000000).toFixed(6)}</td>
                  <td>
                    <code>{row.error_code || "—"}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!usage.length && (
          <Empty
            title="还没有模型调用记录"
            description="确定性检索不产生模型用量。明确使用模型后，这里记录预算结算与结果代码。"
          />
        )}
      </Panel>
    </div>
  );
}
