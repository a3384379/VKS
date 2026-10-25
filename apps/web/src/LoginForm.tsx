import { useState, type FormEvent } from "react";
import { ArrowRight, KeyRound, UserRound } from "lucide-react";
import { Button, Notice } from "./components";

export function LoginForm({
  busy,
  error,
  onLogin,
}: {
  busy: boolean;
  error: string;
  onLogin: (
    mode: "password" | "bootstrap",
    values: Record<string, string>,
  ) => Promise<void>;
}) {
  const [mode, setMode] = useState<"password" | "bootstrap">("password");
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    void onLogin(
      mode,
      mode === "password"
        ? {
            username: String(data.get("username")),
            password: String(data.get("password")),
          }
        : { bootstrap_token: String(data.get("token")) },
    );
  }
  return (
    <>
      <p>
        {mode === "password"
          ? "使用项目管理员创建的本地团队账户登录。"
          : "使用本机初始化凭证，进入实例管理会话。"}
      </p>
      <div className="login-mode-tabs">
        <button
          type="button"
          className={mode === "password" ? "active" : ""}
          onClick={() => setMode("password")}
        >
          <UserRound size={15} />
          团队账户
        </button>
        <button
          type="button"
          className={mode === "bootstrap" ? "active" : ""}
          onClick={() => setMode("bootstrap")}
        >
          <KeyRound size={15} />
          初始化凭证
        </button>
      </div>
      <form key={mode} onSubmit={submit}>
        {mode === "password" ? (
          <>
            <label>
              登录名
              <input
                autoFocus
                name="username"
                autoComplete="username"
                required
                maxLength={80}
                placeholder="输入登录名"
              />
            </label>
            <label>
              密码
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
                maxLength={1024}
                placeholder="输入账户密码"
              />
            </label>
            <small className="field-help">
              初始管理员账号 / 密码：owner /
              owner。其他团队账户由实例管理员创建。
            </small>
          </>
        ) : (
          <>
            <input
              name="username"
              type="hidden"
              autoComplete="username"
              value="owner"
            />
            <label>
              初始化凭证
              <input
                autoFocus
                name="token"
                type="password"
                required
                maxLength={256}
                autoComplete="current-password"
                placeholder="输入本机 bootstrap token"
              />
            </label>
            <small className="field-help">
              从项目的 <code>.vks/bootstrap-token</code>{" "}
              文件读取。凭据不会进入页面地址或本地存储。
            </small>
          </>
        )}
        {error && <Notice tone="red">{error}</Notice>}
        <Button kind="primary full" type="submit" disabled={busy}>
          {busy ? "正在验证…" : "打开工作空间"}
          <ArrowRight size={17} />
        </Button>
      </form>
    </>
  );
}
