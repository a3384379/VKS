import { useEffect, useRef, useState } from "react";
import { Globe, Settings2, X } from "lucide-react";
import { OriginalRuntime } from "./runtime";
import type { RuntimeEvent, RuntimeHandle } from "./runtime";

export type NativeSection = {
  revision_id: string;
  anchor: string | null;
  selector?: string | null;
  heading: string;
  heading_level?: number;
  heading_ordinal?: number;
};
export type NativeDocument = {
  package_id: string;
  package_key: string;
  revision_id: string | null;
  sha256: string;
  format: string;
  path: string;
  document_anchor: string | null;
  sections: NativeSection[];
  matches_index: boolean;
  source_document_revision_id: string | null;
};
export type NativeLocation = {
  path: string;
  anchor?: string;
  articleId?: string;
};
export type NativePackage = {
  id: string;
  package_key?: string;
  revision_id?: string;
  sha256: string;
  name: string;
};

const defaultOrigins = [
  "https://api.openai.com",
  "https://api.deepseek.com",
  "https://dashscope.aliyuncs.com",
];
function origin(value: string): string {
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash ||
    /(^|\.)(localhost|local|internal|invalid|test|example)$/.test(host) ||
    /(^|\.)(lan|home|onion)$/.test(host) ||
    !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(host) ||
    host.includes("..") ||
    /^[\d.]+$/.test(host) ||
    host.includes(":") ||
    !host.includes(".") ||
    url.origin === location.origin
  )
    throw new Error("请填写公开的 HTTPS 服务地址，不含路径、参数或账号。");
  return url.origin;
}
function storageKey(account: string, project: string, pack: string) {
  return `vks.studio.native-network.${account}.${project}.${pack}`;
}
function readOrigins(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return defaultOrigins;
    const saved: unknown = JSON.parse(raw);
    if (Array.isArray(saved) && saved.length <= 20)
      return [...new Set(saved.map((item) => origin(String(item))))];
  } catch {
    // An old or malformed preference cannot expand the runtime network scope.
  }
  return [];
}

export function NativeNetworkSettings({
  account,
  project,
  packageId,
  onOrigins,
}: {
  account: string;
  project: string;
  packageId: string;
  onOrigins: (origins: string[]) => void;
}) {
  const key = storageKey(account, project, packageId);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const callback = useRef(onOrigins);
  callback.current = onOrigins;
  useEffect(() => {
    const origins = readOrigins(key);
    setDraft(origins.join("\n"));
    setOpen(false);
    setError("");
    callback.current(origins);
  }, [key]);
  return (
    <div className="native-settings">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls="native-network-settings"
      >
        <Settings2 size={14} /> 外部资源访问
      </button>
      {open && (
        <section
          id="native-network-settings"
          className="native-settings-panel"
          aria-label="外部资源访问"
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpen(false);
          }}
        >
          <div className="native-settings-title">
            <b>
              <Globe size={16} /> 外部资源访问
            </b>
            <button
              type="button"
              aria-label="关闭外部资源访问"
              onClick={() => setOpen(false)}
            >
              <X size={16} />
            </button>
          </div>
          <p>
            原样 HTML
            需要加载外部字体、图片或页面接口时，可在此允许对应地址。平台 AI
            连接请在“AI 设置”中配置。
          </p>
          <label>
            允许的 HTTPS 服务地址，每行一个
            <textarea
              aria-label="文档允许联网地址"
              rows={5}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="https://api.openai.com"
              spellCheck={false}
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <small>
            保存会重新打开当前文档；原文档的本地配置按账号、项目、资料包和内容版本隔离。
          </small>
          <button
            type="button"
            className="primary"
            onClick={() => {
              try {
                const values = draft.split(/\s+/).filter(Boolean);
                if (values.length > 20)
                  throw new Error("最多填写 20 个服务地址。");
                const origins = [...new Set(values.map(origin))];
                localStorage.setItem(key, JSON.stringify(origins));
                callback.current(origins);
                setError("");
                setOpen(false);
              } catch (error) {
                setError(
                  error instanceof Error ? error.message : "无法保存文档设置",
                );
              }
            }}
          >
            保存并重新打开
          </button>
        </section>
      )}
    </div>
  );
}

export function NativeDocumentPane({
  pack,
  native,
  section,
  account,
  project,
  contentUrl,
  origins,
  visible,
  requestKey,
  onLocation,
  onFailure,
}: {
  pack: NativePackage;
  native: NativeDocument | null;
  section: string;
  account: string;
  project: string;
  contentUrl: string;
  origins: string[];
  visible: boolean;
  requestKey: string;
  onLocation: (location: NativeLocation) => void;
  onFailure: (message: string) => void;
}) {
  const container = useRef<HTMLElement>(null);
  const handle = useRef<RuntimeHandle | null>(null);
  const [ready, setReady] = useState(false);
  const [retry, setRetry] = useState(0);
  const observed = useRef<NativeLocation | null>(null);
  const sent = useRef("");
  const lastRequest = useRef("");
  const requestOwner = useRef("");
  const currentRequest = useRef(requestKey);
  currentRequest.current = requestKey;
  const callbacks = useRef({ onLocation, onFailure });
  callbacks.current = { onLocation, onFailure };
  const policy = JSON.stringify(origins);
  useEffect(() => {
    setReady(false);
    sent.current = "";
    observed.current = null;
    lastRequest.current = "";
  }, [pack.id, policy, retry]);
  useEffect(() => {
    if (!visible) {
      lastRequest.current = "";
      sent.current = "";
    }
  }, [visible]);
  useEffect(() => {
    if (!ready || !visible || !native) return;
    const chapter = section
      ? native.sections.find((item) => item.revision_id === section)
      : null;
    if (section && !chapter) {
      const key = native.package_id + ":" + section;
      if (sent.current !== key) {
        sent.current = key;
        callbacks.current.onFailure("此章节尚无准确原文定位，已打开标准阅读。");
      }
      return;
    }
    const location = {
      path: native.path,
      anchor: chapter?.anchor || native.document_anchor || undefined,
      selector: chapter?.selector || undefined,
      heading: chapter?.heading,
      heading_level: chapter?.heading_level,
      heading_ordinal: chapter?.heading_ordinal,
      articleId: native.document_anchor || undefined,
    };
    const key = JSON.stringify(location);
    if (sent.current === key) return;
    sent.current = key;
    if (
      observed.current?.path === location.path &&
      (!location.anchor || observed.current.anchor === location.anchor)
    )
      return;
    lastRequest.current = key;
    requestOwner.current = requestKey;
    handle.current?.send("document.reveal", { location });
  }, [ready, visible, native, section, requestKey]);
  function event(event: RuntimeEvent) {
    if (event.type === "material.loaded") {
      sent.current = "";
      setReady(true);
    } else if (event.type === "document.selection_menu" && visible) {
      const data = event.payload as {
        text?: string;
        x?: number;
        y?: number;
      } | null;
      const bounds = container.current
        ?.querySelector("iframe")
        ?.getBoundingClientRect();
      if (
        bounds &&
        typeof data?.text === "string" &&
        Number.isFinite(data.x) &&
        Number.isFinite(data.y)
      )
        window.dispatchEvent(
          new CustomEvent("vks-document-selection-menu", {
            detail: {
              text: data.text.slice(0, 4000),
              x: bounds.left + Math.max(0, Math.min(bounds.width, data.x!)),
              y: bounds.top + Math.max(0, Math.min(bounds.height, data.y!)),
            },
          }),
        );
    } else if (event.type === "document.selection_dismiss" && visible) {
      document.dispatchEvent(new Event("vks-close-selection-menu"));
    } else if (event.type === "document.changed") {
      const data = event.payload as Partial<NativeLocation> | null;
      if (
        !data ||
        typeof data.path !== "string" ||
        data.path.length > 2000 ||
        (data.anchor !== undefined &&
          (typeof data.anchor !== "string" || data.anchor.length > 2000)) ||
        (data.articleId !== undefined &&
          (typeof data.articleId !== "string" || data.articleId.length > 2000))
      )
        return;
      observed.current = {
        path: data.path,
        anchor: data.anchor,
        articleId: data.articleId,
      };
      if (visible) callbacks.current.onLocation(observed.current);
    } else if (event.type === "command.result") {
      const data = event.payload as { ok?: boolean; message?: string } | null;
      if (
        visible &&
        lastRequest.current &&
        requestOwner.current === currentRequest.current &&
        data?.ok === false
      ) {
        lastRequest.current = "";
        callbacks.current.onFailure(
          typeof data.message === "string"
            ? data.message.slice(0, 500)
            : "原文定位失败，已打开标准阅读。",
        );
      }
    }
  }
  return (
    <section
      ref={container}
      className="handbook-space native-document-space"
      hidden={!visible}
      aria-label="原样文档"
    >
      <OriginalRuntime
        key={pack.id + ":" + policy + ":" + retry}
        url={contentUrl}
        digest={pack.sha256}
        kind="handbook"
        storageScope={{
          accountId: account,
          projectId: project,
          packageId: pack.package_key || pack.id,
        }}
        networkOrigins={origins}
        handle={handle}
        onEvent={event}
      />
      {!ready && (
        <button
          className="native-runtime-retry"
          onClick={() => setRetry((count) => count + 1)}
        >
          重新打开文档
        </button>
      )}
    </section>
  );
}
