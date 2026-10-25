import "./configuration.css";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { KeyRound, Plus, ShieldCheck } from "lucide-react";
import { api, ApiError, mutation } from "./api";
import {
  Button,
  Empty,
  Modal,
  Notice,
  Panel,
  Spinner,
  Tag,
} from "./components";
import { usePermission, useWorkspace } from "./context";
import { hasProjectPermission } from "./permissions";

interface Issuer {
  id: string;
  issuer: string;
  audience: string;
  active: boolean;
  max_token_seconds: number;
  row_version: number;
  kids: string[];
}
interface Grant {
  id: string;
  issuer_id: string;
  subject: string;
  client_id: string;
  scopes: string[];
  active: boolean;
  row_version: number;
}
interface Metadata {
  resource: string;
  authorization_servers: string[];
  scopes_supported: string[];
}
export function publicJwks(text: string) {
  let value: { keys: Record<string, unknown>[] };
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("公开 JWKS 必须是有效 JSON");
  }
  if (
    !value ||
    Object.keys(value).length !== 1 ||
    !Array.isArray(value.keys) ||
    value.keys.length < 1 ||
    value.keys.length > 10
  )
    throw new Error(
      "请提供只含 keys 数组的公开 JWKS，包含 1–10 个 RSA 签名公钥",
    );
  const kids = new Set<string>();
  for (const key of value.keys) {
    if (
      !key ||
      typeof key !== "object" ||
      key.kty !== "RSA" ||
      (key.alg && key.alg !== "RS256") ||
      (key.use && key.use !== "sig") ||
      ["d", "p", "q", "dp", "dq", "qi", "oth", "k", "jku", "x5u"].some(
        (name) => name in key,
      )
    )
      throw new Error(
        "仅接收公开 RS256 签名公钥；请移除私钥、对称密钥和远程密钥地址",
      );
    if (
      typeof key.kid !== "string" ||
      !key.kid ||
      key.kid.length > 128 ||
      kids.has(key.kid)
    )
      throw new Error("每个公钥需有唯一、非空的 kid");
    if (typeof key.n !== "string" || typeof key.e !== "string")
      throw new Error("RSA 公钥需包含 n 和 e 参数");
    kids.add(key.kid);
  }
  return value;
}

export function OAuthSettings() {
  const { w, session, notify } = useWorkspace();
  const owner = !!session.user?.instance_admin,
    canManage = usePermission("project:manage");
  const [issuers, setIssuers] = useState<Issuer[]>([]),
    [grants, setGrants] = useState<Grant[]>([]),
    [metadata, setMetadata] = useState<Metadata | null>(null),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [issuerOpen, setIssuerOpen] = useState(false),
    [grantOpen, setGrantOpen] = useState(false),
    [rotating, setRotating] = useState<Issuer | null>(null);
  const [issuer, setIssuer] = useState(""),
    [jwks, setJwks] = useState(""),
    [duration, setDuration] = useState(3600),
    [issuerId, setIssuerId] = useState(""),
    [subject, setSubject] = useState(""),
    [clientId, setClientId] = useState(""),
    [scopes, setScopes] = useState(["knowledge:read"]);
  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [known, rows, resource] = await Promise.all([
        owner ? api<Issuer[]>("/oauth/issuers") : Promise.resolve([]),
        canManage
          ? api<Grant[]>("/projects/" + w.project.id + "/oauth-grants")
          : Promise.resolve([]),
        api<Metadata>("/oauth/resource-metadata"),
      ]);
      setIssuers(known);
      setGrants(rows);
      setMetadata(resource);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [owner, canManage, w.project.id]);
  useEffect(() => {
    void reload();
  }, [reload]);
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      notify(message);
      await reload();
      return true;
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "授权配置已变化。当前输入保留，请重新载入并核对后再操作。"
          : (e as Error).message,
      );
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function saveIssuer(e: FormEvent) {
    e.preventDefault();
    let keys;
    try {
      keys = publicJwks(jwks);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    if (
      await perform(
        () =>
          rotating
            ? api(
                "/oauth/issuers/" + rotating.id,
                mutation(
                  "PATCH",
                  { active: rotating.active, jwks: keys },
                  rotating.row_version,
                ),
              )
            : api(
                "/oauth/issuers",
                mutation("POST", {
                  issuer: issuer.trim(),
                  audience: metadata?.resource,
                  jwks: keys,
                  max_token_seconds: duration,
                }),
              ),
        rotating ? "公开签名密钥已更新" : "授权服务已登记",
      )
    ) {
      setIssuerOpen(false);
      setRotating(null);
      setJwks("");
    }
  }
  async function saveGrant(e: FormEvent) {
    e.preventDefault();
    if (
      await perform(
        () =>
          api(
            "/projects/" + w.project.id + "/oauth-grants",
            mutation("POST", {
              issuer_id: issuerId.trim(),
              subject: subject.trim(),
              client_id: clientId.trim(),
              scopes,
            }),
          ),
        "外部客户端项目授权已创建",
      )
    )
      setGrantOpen(false);
  }
  const allowedScopes = (
    metadata?.scopes_supported || ["knowledge:read"]
  ).filter((scope) => hasProjectPermission(session, w.project.id, scope));
  return (
    <div className="configuration-stack">
      <Notice>
        <ShieldCheck size={16} />
        OAuth 客户端先通过登记公钥验证，再按 subject、client_id
        和项目范围授权。客户端不能审核、批准或发布。
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
            重新载入
          </Button>
        </Notice>
      )}
      <Panel title="MCP 受保护资源" action={<Tag>HTTP OAuth</Tag>}>
        <code className="workflow-hash">
          {metadata?.resource || "读取实例资源配置中…"}
        </code>
        <p className="section-description">
          客户端按资源元数据连接已有授权服务。这里只登记公开签名密钥和授权范围，不保存访问令牌或私钥。
        </p>
      </Panel>
      {owner && (
        <Panel
          title="实例授权服务"
          action={
            <Button
              kind="primary small"
              disabled={busy || !metadata}
              onClick={() => {
                setIssuer("");
                setJwks("");
                setDuration(3600);
                setRotating(null);
                setIssuerOpen(true);
              }}
            >
              <Plus size={15} />
              登记授权服务
            </Button>
          }
        >
          {loading ? (
            <Spinner />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Issuer / audience</th>
                    <th>公钥 kid</th>
                    <th>最长令牌有效期</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {issuers.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <code>{row.issuer}</code>
                        <small>{row.audience}</small>
                      </td>
                      <td>{row.kids.join("、")}</td>
                      <td>{row.max_token_seconds} 秒</td>
                      <td>
                        <Tag value={row.active ? "valid" : "obsolete"}>
                          {row.active ? "有效" : "停用"}
                        </Tag>
                      </td>
                      <td>
                        <div className="row wrap">
                          <button
                            className="text-button"
                            disabled={busy}
                            onClick={() => {
                              setRotating(row);
                              setJwks("");
                              setIssuerOpen(true);
                            }}
                          >
                            更换公钥
                          </button>
                          <button
                            className="text-button"
                            disabled={busy}
                            onClick={() => {
                              void perform(
                                () =>
                                  api(
                                    "/oauth/issuers/" + row.id,
                                    mutation(
                                      "PATCH",
                                      { active: !row.active },
                                      row.row_version,
                                    ),
                                  ),
                                row.active
                                  ? "授权服务已停用"
                                  : "授权服务已启用",
                              );
                            }}
                          >
                            {row.active ? "停用" : "启用"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!issuers.length && (
                <Empty
                  title="尚未登记授权服务"
                  description="由实例管理员登记授权服务的公开 JWKS，再创建项目客户端授权。"
                />
              )}
            </div>
          )}
        </Panel>
      )}
      {canManage && (
        <Panel
          title="当前项目的外部客户端"
          action={
            <Button
              kind="primary small"
              disabled={busy}
              onClick={() => {
                setIssuerId("");
                setSubject("");
                setClientId("");
                setScopes(["knowledge:read"]);
                setGrantOpen(true);
              }}
            >
              <Plus size={15} />
              创建授权
            </Button>
          }
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>客户端 / 主体</th>
                  <th>授权服务</th>
                  <th>范围</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {grants.map((row) => (
                  <tr key={row.id}>
                    <td>
                      {row.client_id}
                      <small>{row.subject}</small>
                    </td>
                    <td>
                      <code>
                        {issuers.find((i) => i.id === row.issuer_id)?.issuer ||
                          row.issuer_id}
                      </code>
                    </td>
                    <td>
                      <div className="row wrap">
                        {row.scopes.map((scope) => (
                          <Tag key={scope}>{scope}</Tag>
                        ))}
                      </div>
                    </td>
                    <td>
                      <Tag value={row.active ? "valid" : "obsolete"}>
                        {row.active ? "有效" : "停用"}
                      </Tag>
                    </td>
                    <td>
                      <button
                        className="text-button"
                        disabled={busy}
                        onClick={() => {
                          void perform(
                            () =>
                              api(
                                "/projects/" +
                                  w.project.id +
                                  "/oauth-grants/" +
                                  row.id,
                                mutation(
                                  "PATCH",
                                  { active: !row.active },
                                  row.row_version,
                                ),
                              ),
                            row.active ? "项目授权已撤销" : "项目授权已启用",
                          );
                        }}
                      >
                        {row.active ? "撤销" : "启用"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!grants.length && (
              <Empty
                title="还没有外部客户端授权"
                description="外部身份只有获得本项目授权后，才能读取或提交允许的材料。"
              />
            )}
          </div>
        </Panel>
      )}
      {issuerOpen && (
        <Modal
          title={rotating ? "轮换公开签名密钥" : "登记 OAuth 授权服务"}
          onClose={() => {
            setIssuerOpen(false);
            setRotating(null);
            setJwks("");
          }}
        >
          <form
            onSubmit={(e) => {
              void saveIssuer(e);
            }}
          >
            {rotating ? (
              <Notice>
                {rotating.issuer} · 请提供轮换期间需要接受的全部公开公钥。
              </Notice>
            ) : (
              <>
                <label>
                  Issuer
                  <input
                    type="url"
                    required
                    value={issuer}
                    maxLength={2048}
                    onChange={(e) => setIssuer(e.target.value)}
                    placeholder="https://identity.example"
                  />
                </label>
                <label>
                  本实例 audience
                  <input readOnly value={metadata?.resource || ""} />
                </label>
                <label>
                  最长令牌有效期（秒）
                  <input
                    type="number"
                    required
                    min={60}
                    max={86400}
                    value={duration}
                    onChange={(e) => setDuration(Number(e.target.value))}
                  />
                </label>
              </>
            )}
            <label>
              公开 RSA JWKS
              <textarea
                className="configuration-json"
                required
                value={jwks}
                onChange={(e) => setJwks(e.target.value)}
                placeholder={
                  '{"keys":[{"kty":"RSA","kid":"...","n":"...","e":"AQAB","alg":"RS256"}]}'
                }
                spellCheck={false}
              />
            </label>
            <Notice>
              只接受至少 2048 位的公开 RS256
              签名密钥。请勿粘贴私钥、访问令牌或凭证。
            </Notice>
            {error && <Notice tone="red">{error}</Notice>}
            <div className="modal-actions">
              <Button
                onClick={() => {
                  setIssuerOpen(false);
                  setRotating(null);
                  setJwks("");
                }}
              >
                取消
              </Button>
              <Button kind="primary" type="submit" disabled={busy || !metadata}>
                <KeyRound size={15} />
                保存公开配置
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {grantOpen && (
        <Modal
          title="授权外部客户端访问项目"
          onClose={() => setGrantOpen(false)}
        >
          <form
            onSubmit={(e) => {
              void saveGrant(e);
            }}
          >
            <label>
              已登记的授权服务
              {owner ? (
                <select
                  required
                  value={issuerId}
                  onChange={(e) => setIssuerId(e.target.value)}
                >
                  <option value="">选择授权服务</option>
                  {issuers
                    .filter((i) => i.active)
                    .map((i) => (
                      <option key={i.id} value={i.id}>
                        {i.issuer}
                      </option>
                    ))}
                </select>
              ) : (
                <input
                  required
                  value={issuerId}
                  onChange={(e) => setIssuerId(e.target.value)}
                  placeholder="实例管理员提供的授权服务 ID"
                />
              )}
            </label>
            <label>
              Subject
              <input
                required
                maxLength={255}
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                placeholder="令牌的 sub 声明"
              />
            </label>
            <label>
              Client ID
              <input
                required
                maxLength={255}
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                placeholder="已登记的外部 MCP 客户端 ID"
              />
            </label>
            <div className="scope-options">
              {allowedScopes.map((scope) => (
                <label className="scope-option" key={scope}>
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope)}
                    disabled={scope === "knowledge:read"}
                    onChange={(e) =>
                      setScopes((previous) =>
                        e.target.checked
                          ? [...previous, scope]
                          : previous.filter((s) => s !== scope),
                      )
                    }
                  />
                  <span>{scope}</span>
                </label>
              ))}
            </div>
            <Notice>
              授权不超过当前管理员拥有的项目范围。停用后新请求立即拒绝；客户端需自行从授权服务获取令牌。
            </Notice>
            {error && <Notice tone="red">{error}</Notice>}
            <div className="modal-actions">
              <Button onClick={() => setGrantOpen(false)}>取消</Button>
              <Button kind="primary" type="submit" disabled={busy}>
                创建项目授权
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
