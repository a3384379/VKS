import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Check,
  Copy,
  FolderGit2,
  Plus,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { api, mutation } from "./api";
import {
  Button,
  Empty,
  Modal,
  Notice,
  Panel,
  Spinner,
  Tag,
} from "./components";
import { useWorkspace, usePermission, useReadScope } from "./context";
import type { Source, SourceConnector } from "./types";

export function SourceManagement() {
  const { w, notify } = useWorkspace();
  const canManage = usePermission("project:manage"),
    canRead = usePermission("knowledge:edit");
  const scope = useReadScope();
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const request = useRef<AbortController | null>(null);
  const [data, setData] = useState<{
      scope: string;
      connectors: SourceConnector[];
      snapshots: Source[];
    } | null>(null),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("local"),
    [key, setKey] = useState(""),
    [name, setName] = useState(""),
    [locator, setLocator] = useState(""),
    [secretRef, setSecretRef] = useState("");
  const connectors = canManage && data?.scope === scope ? data.connectors : [];
  const snapshots = canRead && data?.scope === scope ? data.snapshots : [];
  useLayoutEffect(() => {
    setOpen(false);
    setKey("");
    setName("");
    setLocator("");
    setSecretRef("");
  }, [w.project.id, canManage]);
  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const isCurrent = () =>
      !controller.signal.aborted && currentScope.current === scope;
    setLoading(true);
    setError("");
    try {
      const rows = await Promise.all([
        canManage
          ? api<SourceConnector[]>(
              `/projects/${w.project.id}/source-connectors`,
              { signal: controller.signal },
            )
          : Promise.resolve([]),
        canRead
          ? api<Source[]>(`/projects/${w.project.id}/source-snapshots`, {
              signal: controller.signal,
            })
          : Promise.resolve([]),
      ]);
      if (isCurrent())
        setData({ scope, connectors: rows[0], snapshots: rows[1] });
    } catch (e) {
      if (isCurrent()) setError((e as Error).message);
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [w.project.id, canManage, canRead, scope]);
  useEffect(() => {
    void reload();
    return () => request.current?.abort();
  }, [reload]);
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(
        `/projects/${w.project.id}/source-connectors`,
        mutation("POST", {
          key: key.trim(),
          name: name.trim(),
          provider,
          remote_locator: locator.trim(),
          ...(secretRef.trim() ? { secret_ref: secretRef.trim() } : {}),
        }),
      );
      if (currentScope.current !== scope) return;
      setOpen(false);
      setSecretRef("");
      notify("只读来源已登记；采集在你指定的客户端目录执行");
      await reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const command = `PYTHONPATH=. uv run python -m packages.collector --provider directory --root /你授权的项目目录 --project ${w.project.key} --baseline import-v2 --service backend --output project.vksbundle.zip`;
  return (
    <Panel
      title="来源连接与不可变快照"
      action={
        <div className="row">
          <Button
            kind="small"
            disabled={loading}
            onClick={() => {
              void reload();
            }}
          >
            <RefreshCw size={14} />
            刷新
          </Button>
          {canManage && (
            <Button
              kind="primary small"
              onClick={() => {
                setKey("");
                setName("");
                setProvider("local");
                setLocator("");
                setSecretRef("");
                setOpen(true);
              }}
            >
              <Plus size={14} />
              登记来源
            </Button>
          )}
        </div>
      }
    >
      <p className="section-description">
        来源登记描述项目资料，上传后的每次版本保持独立快照。服务端不会根据填写的本地路径自行读取文件。
      </p>
      {error && <Notice tone="red">{error}</Notice>}
      {loading ? (
        <Spinner />
      ) : (
        <>
          <div className="source-connectors">
            {connectors.map((c) => (
              <div key={c.id}>
                <span className="entity-icon">
                  <FolderGit2 size={20} />
                </span>
                <section>
                  <strong>{c.name}</strong>
                  <small>
                    {c.provider} · {c.key}
                  </small>
                  <code>{c.remote_locator || "离线上传"}</code>
                </section>
                <Tag value="static">客户端只读</Tag>
              </div>
            ))}
          </div>
          {canManage && !connectors.length && (
            <p className="muted">
              还没有登记连接。离线上传仍可使用，登记不会访问来源。
            </p>
          )}
          <div className="table-wrap source-snapshot-table">
            <table>
              <thead>
                <tr>
                  <th>来源</th>
                  <th>提供方</th>
                  <th>固定 revision</th>
                  <th>内容指纹</th>
                </tr>
              </thead>
              <tbody>
                {snapshots.map((s) => (
                  <tr key={s.source_snapshot_id || s.id}>
                    <td>{s.source_id || s.role}</td>
                    <td>{s.provider}</td>
                    <td>
                      <code title={s.provider_revision}>
                        {s.provider_revision && s.provider_revision.length > 30
                          ? `${s.provider_revision.slice(0, 20)}…`
                          : s.provider_revision}
                      </code>
                    </td>
                    <td>
                      <code title={s.content_sha256}>
                        {s.content_sha256?.slice(0, 16)}
                      </code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!snapshots.length && (
              <Empty
                title="还没有可访问的来源快照"
                description="导入指定来源版本后，快照与内容摘要将在此保留。"
              />
            )}
          </div>
        </>
      )}
      <details className="collector-guide">
        <summary>从本地目录、Git 或 SVN 采集资料</summary>
        <Notice>
          <ShieldCheck size={16} />
          采集器在你授权的客户端运行，仅读取资料，不运行 npm、Maven、hooks
          或仓库脚本。先采集，再上传生成的包。
        </Notice>
        <pre className="config-block">{command}</pre>
        <Button
          kind="small"
          onClick={() => {
            void navigator.clipboard
              .writeText(command)
              .then(() =>
                notify("命令模板已复制，请替换为自己的授权路径与版本"),
              );
          }}
        >
          <Copy size={14} />
          复制目录采集模板
        </Button>
        <ul className="collector-notes">
          <li>
            <Check size={14} />
            Git：使用{" "}
            <code>--provider git --root 本地仓库 --revision 固定ref</code>
            ，包内记录解析后的 commit。
          </li>
          <li>
            <Check size={14} />
            SVN：使用{" "}
            <code>
              --provider svn --revision 数字 --allow-svn-root 明确允许的仓库URL
            </code>
            ，并安装本地 SVN 客户端。
          </li>
          <li>
            <Check size={14} />
            凭据保留在客户端受控配置；来源地址不得包含密码或 token。
          </li>
        </ul>
      </details>
      {open && canManage && (
        <Modal title="登记只读来源" onClose={() => setOpen(false)}>
          <form
            onSubmit={(e) => {
              void save(e);
            }}
          >
            <label>
              来源名称
              <input
                required
                autoFocus
                value={name}
                maxLength={160}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：计费后端"
              />
            </label>
            <label>
              稳定标识
              <input
                required
                value={key}
                pattern="[a-z][a-z0-9._\-]*"
                maxLength={96}
                onChange={(e) => setKey(e.target.value)}
                placeholder="例如：billing-backend"
              />
            </label>
            <label>
              来源类型
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
              >
                <option value="local">本地目录</option>
                <option value="git">Git</option>
                <option value="svn">SVN</option>
                <option value="upload">离线上传</option>
              </select>
            </label>
            <label>
              来源定位说明
              <input
                value={locator}
                maxLength={2048}
                onChange={(e) => setLocator(e.target.value)}
                placeholder="授权仓库 URL 或本地来源说明"
              />
            </label>
            <label>
              受控凭据引用（可选）
              <input
                value={secretRef}
                maxLength={128}
                onChange={(e) => setSecretRef(e.target.value)}
                placeholder="仅引用名称，不填写秘密明文"
              />
            </label>
            <Notice>
              这里保存登记信息。实际读取由客户端采集器在明确授权范围内执行，不把本地路径映射为服务器路径。
            </Notice>
            <div className="modal-actions">
              <Button onClick={() => setOpen(false)}>取消</Button>
              <Button kind="primary" disabled={busy} type="submit">
                登记来源
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </Panel>
  );
}
