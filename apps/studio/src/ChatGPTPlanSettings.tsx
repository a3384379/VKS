import { useEffect, useState } from "react";
import { api, mutation } from "../../web/src/api";

const usageUrl = "https://chatgpt.com/#settings/usage";
const preference = (project: string) => "vks-studio-chatgpt-plan:" + project;

export function chatGPTPlanHeaders(project: string): Record<string, string> {
  try {
    return sessionStorage.getItem(preference(project)) === "enabled"
      ? { "X-VKS-Model-Access": "chatgpt-plan" }
      : {};
  } catch {
    return {};
  }
}

type PlanAction = "status" | "connect" | "models" | "enable" | "disable" | "disconnect";
type Status = {
  available: boolean;
  connected?: boolean;
  pending?: boolean;
  enabled?: boolean;
  account?: string;
  model?: string;
  message?: string;
  models?: { id: string; name: string }[];
};

function request(project: string, action: PlanAction, model = "", allowed = false) {
  return api<Status>(
    "/api/v3/projects/" + encodeURIComponent(project) + "/chatgpt-plan",
    mutation("POST", { action, model, allow_external: allowed }),
  );
}

export function ChatGPTPlanSettings({ project }: { project: string }) {
  const [status, setStatus] = useState<Status>({ available: false });
  const [models, setModels] = useState<Status["models"]>([]);
  const [model, setModel] = useState("");
  const [allowed, setAllowed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    const refresh = () =>
      request(project, "status").then((value) => {
        if (current) {
          setStatus(value);
          if (value.model) setModel(value.model);
        }
      }).catch(() => {});
    void refresh();
    const timer = status.pending ? window.setInterval(() => void refresh(), 1500) : undefined;
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [project, status.pending]);

  async function change(action: PlanAction) {
    setBusy(true);
    setError("");
    try {
      const value = await request(project, action, model, allowed);
      setStatus(value);
      if (action === "models") {
        setModels(value.models);
        setModel(value.model || value.models?.[0]?.id || "");
      }
      if (action === "enable") sessionStorage.setItem(preference(project), "enabled");
      if (action === "disable" || action === "disconnect") {
        sessionStorage.removeItem(preference(project));
        if (action === "disconnect") setModels([]);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!status.available) return null;
  const selected = !!chatGPTPlanHeaders(project)["X-VKS-Model-Access"];
  return (
    <section className="ai-connection-form" aria-labelledby="chatgpt-plan-title">
      <h2 id="chatgpt-plan-title">ChatGPT 订阅</h2>
      <p>用自己的 ChatGPT 额度生成回答和单条 AI 讲解。项目检索在本机进行。</p>
      {status.account && <p>当前账号 {status.account}</p>}
      <div className="ai-title-row">
        {!status.connected && <button type="button" disabled={busy || status.pending}
          onClick={() => void change("connect")}>使用 ChatGPT 登录</button>}
        {status.connected && <>
          <button type="button" disabled={busy} onClick={() => void change("models")}>读取可用模型</button>
          <button type="button" disabled={busy} onClick={() => void change("disconnect")}>退出 ChatGPT</button>
        </>}
        <a href={usageUrl} target="_blank" rel="noreferrer">管理 ChatGPT 用量</a>
      </div>
      {!!models?.length && <div>
        <label>订阅模型
          <select value={model} onChange={(event) => setModel(event.target.value)}>
            {models.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label>
          <input type="checkbox" checked={allowed} onChange={(event) => setAllowed(event.target.checked)} />
          允许提问时将必要的项目片段发送给 OpenAI
        </label>
        <button type="button" disabled={busy || !allowed || !model}
          onClick={() => void change("enable")}>本页使用 ChatGPT 额度</button>
      </div>}
      {selected && <p>本页回答使用 ChatGPT 额度。  <button type="button" disabled={busy}
        onClick={() => void change("disable")}>切换回项目模型</button></p>}
      <p role="status">{status.pending ? "请在系统浏览器完成授权。" : status.message}</p>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
