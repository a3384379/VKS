import "./team-settings.css";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { KeyRound, Plus, Save, ShieldCheck, UserRound } from "lucide-react";
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
import { useWorkspace, usePermission } from "./context";
import type { TeamUser, TeamMember } from "./types";

const roles = [
  { value: "viewer", label: "阅读者" },
  { value: "editor", label: "编辑者" },
  { value: "reviewer", label: "审核者" },
  { value: "maintainer", label: "维护者" },
  { value: "admin", label: "项目管理员" },
];
const sensitive = [
  { scope: "source:read", name: "读取源码", help: "源文件与机密材料" },
  { scope: "trace:read", name: "读取 Trace", help: "脱敏网络与运行证据" },
  { scope: "capture:run", name: "发起录制", help: "还需环境与动作审批" },
];
function roleName(role: string) {
  return roles.find((r) => r.value === role)?.label || role;
}
export function TeamSettings() {
  const { w, session, notify, refresh } = useWorkspace();
  const canManage = usePermission("project:manage"),
    owner = !!session.user?.instance_admin;
  const [users, setUsers] = useState<TeamUser[]>([]),
    [members, setMembers] = useState<TeamMember[]>([]),
    [version, setVersion] = useState(w.project.row_version || 1),
    [loading, setLoading] = useState(false),
    [working, setWorking] = useState(false),
    [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false),
    [memberOpen, setMemberOpen] = useState(false),
    [editing, setEditing] = useState<TeamMember | null>(null),
    [resetUser, setResetUser] = useState<TeamUser | null>(null),
    [disableUser, setDisableUser] = useState<TeamUser | null>(null);
  const [username, setUsername] = useState(""),
    [displayName, setDisplayName] = useState(""),
    [password, setPassword] = useState(""),
    [selectedUser, setSelectedUser] = useState(""),
    [role, setRole] = useState("viewer"),
    [status, setStatus] = useState("active"),
    [permissions, setPermissions] = useState<string[]>([]);
  const [profileName, setProfileName] = useState(
      session.name || session.user?.display_name || "",
    ),
    [profileVersion, setProfileVersion] = useState(
      session.user?.row_version || 1,
    ),
    [passwordOpen, setPasswordOpen] = useState(false);
  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const results = await Promise.all([
        owner ? api<TeamUser[]>("/users") : Promise.resolve([]),
        canManage
          ? api<{ row_version: number; members: TeamMember[] }>(
              `/projects/${w.project.id}/member-permissions`,
            )
          : Promise.resolve({ row_version: 1, members: [] }),
      ]);
      setUsers(results[0]);
      setMembers(results[1].members);
      setVersion(results[1].row_version);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [w.project.id, owner, canManage]);
  useEffect(() => {
    void reload();
  }, [reload]);
  useEffect(() => {
    setProfileName(session.name || session.user?.display_name || "");
    setProfileVersion(session.user?.row_version || 1);
  }, [session.name, session.user?.row_version, session.user?.display_name]);
  const administrator =
    members.find((member) => member.user_id === session.actor_id)?.role ===
    "admin";
  async function perform(action: () => Promise<unknown>, message: string) {
    setWorking(true);
    setError("");
    try {
      await action();
      notify(message);
      await refresh();
      await reload();
      return true;
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "资料已被其他人更新。当前输入已保留，请关闭后重新载入再合并。"
          : (e as Error).message,
      );
      return false;
    } finally {
      setWorking(false);
    }
  }
  async function create(e: FormEvent) {
    e.preventDefault();
    if (
      await perform(
        () =>
          api(
            "/users",
            mutation("POST", {
              username: username.trim(),
              display_name: displayName.trim(),
              password,
            }),
          ),
        "团队用户已创建，分配项目后即可访问",
      )
    ) {
      setCreateOpen(false);
      setPassword("");
    }
  }
  async function saveMember(e: FormEvent) {
    e.preventDefault();
    const userId = editing?.user_id || selectedUser;
    if (
      await perform(
        () =>
          api(
            `/projects/${w.project.id}/members`,
            mutation("PUT", { user_id: userId, role, status }, version),
          ),
        "项目角色已更新，受影响令牌已撤销",
      )
    ) {
      setMemberOpen(false);
      setEditing(null);
    }
  }
  async function saveSensitive() {
    if (!editing) return;
    if (
      await perform(
        () =>
          api(
            `/projects/${w.project.id}/member-permissions`,
            mutation("PUT", { user_id: editing.user_id, permissions }, version),
          ),
        "独立敏感权限已更新，相关令牌需重新创建",
      )
    )
      setEditing(null);
  }
  function editMember(member: TeamMember) {
    setError("");
    setEditing(member);
    setSelectedUser(member.user_id);
    setRole(member.role);
    setStatus(member.status);
    setMemberOpen(true);
  }
  return (
    <div className="team-settings">
      {error && (
        <Notice tone="red">
          <strong>操作尚未完成</strong>
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
      <Panel
        title="我的账户"
        action={<Tag>{owner ? "实例管理员" : "项目成员"}</Tag>}
      >
        <div className="profile-row">
          <div className="profile-avatar">
            <UserRound size={25} />
          </div>
          <div>
            <strong>{session.name || session.user?.display_name}</strong>
            <small>
              {session.user?.username || session.actor_id} · 本地账户
            </small>
          </div>
        </div>
        <form
          className="profile-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform(async () => {
              const result = await api<TeamUser>(
                "/auth/profile",
                mutation(
                  "PATCH",
                  { display_name: profileName.trim() },
                  profileVersion,
                ),
              );
              setProfileVersion(result.row_version);
            }, "账户名称已更新");
          }}
        >
          <label>
            显示名称
            <input
              value={profileName}
              maxLength={160}
              onChange={(e) => setProfileName(e.target.value)}
            />
          </label>
          <Button disabled={working || !profileName.trim()} type="submit">
            <Save size={15} />
            保存名称
          </Button>
          <Button
            onClick={() => {
              setPassword("");
              setPasswordOpen(true);
            }}
            disabled={!session.user?.password_configured}
          >
            <KeyRound size={15} />
            修改密码
          </Button>
        </form>
        {!session.user?.password_configured && (
          <p className="fine-print">
            当前使用初始化凭证。实例管理员可在用户列表为此账户设置密码。
          </p>
        )}
      </Panel>
      {owner && (
        <Panel
          title="实例用户"
          action={
            <Button
              kind="primary small"
              onClick={() => {
                setUsername("");
                setDisplayName("");
                setPassword("");
                setCreateOpen(true);
              }}
            >
              <Plus size={15} />
              创建用户
            </Button>
          }
        >
          <p className="section-description">
            实例用户不自动获得项目权限。创建后，将用户加入相应项目并选择角色。
          </p>
          {loading ? (
            <Spinner />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>登录名</th>
                    <th>状态</th>
                    <th>管理</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.id}>
                      <td>
                        <strong>{u.display_name}</strong>
                        {u.instance_admin && (
                          <span className="tiny-label">实例管理员</span>
                        )}
                      </td>
                      <td>
                        <code>{u.username}</code>
                      </td>
                      <td>
                        <Tag
                          value={u.status === "active" ? "valid" : "obsolete"}
                        >
                          {u.status === "active" ? "有效" : "已停用"}
                        </Tag>
                      </td>
                      <td>
                        <div className="row wrap">
                          <button
                            className="text-button"
                            disabled={working}
                            onClick={() => {
                              setResetUser(u);
                              setPassword("");
                            }}
                          >
                            重置密码
                          </button>
                          <button
                            className="text-button danger-text"
                            disabled={working || u.instance_admin}
                            onClick={() => setDisableUser(u)}
                          >
                            {u.status === "active" ? "停用" : "重新启用"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
      {canManage ? (
        <Panel
          title="项目成员与角色"
          action={
            <Button
              kind="primary small"
              onClick={() => {
                setEditing(null);
                setSelectedUser("");
                setRole("viewer");
                setStatus("active");
                setMemberOpen(true);
              }}
            >
              <Plus size={15} />
              加入项目成员
            </Button>
          }
        >
          <p className="section-description">
            {w.project.name} · 角色控制编辑、审核和发布；源码、Trace
            与录制另行授权。
          </p>
          {loading ? (
            <Spinner />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>成员</th>
                    <th>角色</th>
                    <th>敏感权限</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.user_id}>
                      <td>
                        <strong>
                          {m.display_name || m.username || m.user_id}
                        </strong>
                        <small>{m.username}</small>
                      </td>
                      <td>{roleName(m.role)}</td>
                      <td>
                        <div className="row wrap">
                          {m.permissions.length ? (
                            m.permissions.map((p) => (
                              <Tag key={p}>
                                {sensitive.find((s) => s.scope === p)?.name ||
                                  p}
                              </Tag>
                            ))
                          ) : (
                            <span className="muted">未授予</span>
                          )}
                        </div>
                      </td>
                      <td>
                        <Tag
                          value={m.status === "active" ? "valid" : "obsolete"}
                        >
                          {m.status === "active" ? "有效" : "已停用"}
                        </Tag>
                      </td>
                      <td>
                        <div className="row wrap">
                          <button
                            className="text-button"
                            disabled={
                              working || (!administrator && m.role === "admin")
                            }
                            onClick={() => editMember(m)}
                          >
                            角色
                          </button>
                          <button
                            className="text-button"
                            disabled={
                              working ||
                              !administrator ||
                              m.status !== "active" ||
                              m.role === "admin"
                            }
                            title={
                              m.role === "admin"
                                ? "项目管理员具有完整管理权限"
                                : undefined
                            }
                            onClick={() => {
                              setEditing(m);
                              setMemberOpen(false);
                              setPermissions([...m.permissions]);
                            }}
                          >
                            独立授权
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!members.length && (
                <Empty
                  title="暂无成员"
                  description="加入已有用户，建立项目授权范围。"
                />
              )}
            </div>
          )}
        </Panel>
      ) : (
        <Notice>
          项目成员和敏感权限由项目管理员维护。当前账户可以查看已授权的业务知识。
        </Notice>
      )}
      {createOpen && (
        <Modal
          title="创建团队用户"
          onClose={() => {
            setCreateOpen(false);
            setPassword("");
          }}
        >
          <form
            onSubmit={(e) => {
              void create(e);
            }}
          >
            <label>
              登录名
              <input
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                pattern="[a-z][a-z0-9._\-]*"
                maxLength={80}
                placeholder="例如：alice"
                autoComplete="off"
              />
            </label>
            <label>
              显示名称
              <input
                required
                maxLength={160}
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </label>
            <label>
              初始密码
              <input
                type="password"
                required
                minLength={12}
                maxLength={1024}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
            </label>
            <Notice>
              密码至少 12
              个字符，只保存不可逆密码摘要。请通过你们受控的方式将登录信息交给用户。
            </Notice>
            <div className="modal-actions">
              <Button
                onClick={() => {
                  setCreateOpen(false);
                  setPassword("");
                }}
              >
                取消
              </Button>
              <Button kind="primary" disabled={working} type="submit">
                创建用户
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {memberOpen && (
        <Modal
          title={editing ? "调整项目角色" : "加入项目成员"}
          onClose={() => {
            setMemberOpen(false);
            setEditing(null);
          }}
        >
          <form
            onSubmit={(e) => {
              void saveMember(e);
            }}
          >
            {editing ? (
              <Notice>
                {editing.display_name || editing.username} · {editing.user_id}
              </Notice>
            ) : (
              <label>
                已有实例用户
                {owner ? (
                  <select
                    required
                    value={selectedUser}
                    onChange={(e) => setSelectedUser(e.target.value)}
                  >
                    <option value="">选择用户</option>
                    {users
                      .filter(
                        (u) =>
                          u.status === "active" &&
                          !members.some((m) => m.user_id === u.id),
                      )
                      .map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.display_name}（{u.username}）
                        </option>
                      ))}
                  </select>
                ) : (
                  <input
                    required
                    value={selectedUser}
                    onChange={(e) => setSelectedUser(e.target.value)}
                    placeholder="输入实例管理员提供的用户 ID"
                  />
                )}
              </label>
            )}
            <label>
              项目角色
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {roles.map((r) => (
                  <option
                    key={r.value}
                    value={r.value}
                    disabled={!administrator && r.value === "admin"}
                  >
                    {r.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              成员状态
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
              >
                <option value="active">有效</option>
                <option value="disabled">停用</option>
              </select>
            </label>
            <Notice>
              停用会撤销该用户的项目访问和相关令牌；最后一位管理员不能移除。实例管理员也不能绕过项目成员授权。
            </Notice>
            <div className="modal-actions">
              <Button
                onClick={() => {
                  setMemberOpen(false);
                  setEditing(null);
                }}
              >
                取消
              </Button>
              <Button
                kind="primary"
                disabled={working || (!editing && !selectedUser)}
                type="submit"
              >
                保存项目角色
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {editing && !memberOpen && (
        <Modal title="独立敏感权限" onClose={() => setEditing(null)}>
          <p>{editing.display_name || editing.username}</p>
          <div className="scope-options">
            {sensitive.map((p) => (
              <label className="scope-option" key={p.scope}>
                <input
                  type="checkbox"
                  checked={permissions.includes(p.scope)}
                  disabled={
                    editing.role === "viewer" && p.scope === "capture:run"
                  }
                  onChange={(e) =>
                    setPermissions((old) =>
                      e.target.checked
                        ? [...old, p.scope]
                        : old.filter((s) => s !== p.scope),
                    )
                  }
                />
                <span>
                  <strong>{p.name}</strong>
                  <small>{p.help}</small>
                </span>
                <code>{p.scope}</code>
              </label>
            ))}
          </div>
          <Notice>
            授权变化会撤销受影响的机器令牌；真实录制仍需有效环境、测试数据和审批。
          </Notice>
          <div className="modal-actions">
            <Button onClick={() => setEditing(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={working}
              onClick={() => {
                void saveSensitive();
              }}
            >
              保存独立权限
            </Button>
          </div>
        </Modal>
      )}
      {resetUser && (
        <Modal
          title="重置账户密码"
          onClose={() => {
            setResetUser(null);
            setPassword("");
          }}
        >
          <p>
            重置 {resetUser.display_name}（{resetUser.username}
            ）的密码，并撤销旧会话与其签发的令牌。
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void perform(
                () =>
                  api(
                    `/users/${resetUser.id}/password-reset`,
                    mutation("POST", { new_password: password }),
                  ),
                "密码已重置，旧会话与令牌已失效",
              ).then((ok) => {
                if (ok) {
                  setResetUser(null);
                  setPassword("");
                }
              });
            }}
          >
            <label>
              新密码
              <input
                autoFocus
                type="password"
                required
                minLength={12}
                maxLength={1024}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
            </label>
            <div className="modal-actions">
              <Button
                onClick={() => {
                  setResetUser(null);
                  setPassword("");
                }}
              >
                取消
              </Button>
              <Button kind="primary" disabled={working} type="submit">
                确认重置
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {disableUser && (
        <Modal
          title={
            disableUser.status === "active" ? "停用团队用户" : "重新启用用户"
          }
          onClose={() => setDisableUser(null)}
        >
          <Notice tone="amber">
            {disableUser.display_name} ·{" "}
            {disableUser.status === "active"
              ? "停用后撤销会话、令牌和项目成员授权。"
              : "重新启用后，项目成员授权需要管理员单独恢复。"}
          </Notice>
          <div className="modal-actions">
            <Button onClick={() => setDisableUser(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={working}
              onClick={() => {
                void perform(
                  () =>
                    api(
                      `/users/${disableUser.id}`,
                      mutation(
                        "PATCH",
                        {
                          status:
                            disableUser.status === "active"
                              ? "disabled"
                              : "active",
                        },
                        disableUser.row_version,
                      ),
                    ),
                  "用户状态已更新",
                ).then((ok) => {
                  if (ok) setDisableUser(null);
                });
              }}
            >
              确认{disableUser.status === "active" ? "停用" : "启用"}
            </Button>
          </div>
        </Modal>
      )}
      {passwordOpen && (
        <Modal
          title="修改我的密码"
          onClose={() => {
            setPasswordOpen(false);
            setPassword("");
          }}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const data = new FormData(e.currentTarget);
              setWorking(true);
              void api(
                "/auth/password",
                mutation("POST", {
                  current_password: data.get("current"),
                  new_password: data.get("next"),
                }),
              )
                .then(async () => {
                  setPasswordOpen(false);
                  notify("密码已更改，请重新登录");
                  await refresh();
                })
                .catch((e) => setError(e.message))
                .finally(() => setWorking(false));
            }}
          >
            <label>
              当前密码
              <input
                autoFocus
                type="password"
                required
                name="current"
                autoComplete="current-password"
              />
            </label>
            <label>
              新密码
              <input
                type="password"
                required
                minLength={12}
                maxLength={1024}
                name="next"
                autoComplete="new-password"
              />
            </label>
            <div className="modal-actions">
              <Button onClick={() => setPasswordOpen(false)}>取消</Button>
              <Button kind="primary" disabled={working} type="submit">
                修改并重新登录
              </Button>
            </div>
          </form>
        </Modal>
      )}
      <p className="fine-print">
        <ShieldCheck size={13} />
        本地团队账户，与企业单点登录分开配置。
      </p>
    </div>
  );
}
