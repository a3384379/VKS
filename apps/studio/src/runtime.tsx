import { useEffect, useRef, useState } from "react";
export type Target = {
  route: string;
  frame_path: string[];
  selector: string;
  label: string;
  state: string;
  open_steps: string[];
};
export type RuntimeEvent = {
  type: string;
  payload: unknown;
  runtime_instance_id: string;
  sequence: number;
  request_id: string;
  protocol_version: number;
};
export function isTarget(value: unknown): value is Target {
  if (!value || typeof value !== "object") return false;
  const t = value as Record<string, unknown>;
  const bounded = (v: unknown, n: number) =>
    typeof v === "string" && v.length > 0 && v.length <= n;
  return (
    bounded(t.route, 1000) &&
    (t.route as string).startsWith("/") &&
    bounded(t.selector, 2000) &&
    bounded(t.label, 160) &&
    typeof t.state === "string" &&
    t.state.length <= 500 &&
    Array.isArray(t.frame_path) &&
    t.frame_path.length <= 8 &&
    t.frame_path.every((v) => bounded(v, 160)) &&
    Array.isArray(t.open_steps) &&
    t.open_steps.length <= 8 &&
    t.open_steps.every((v) => bounded(v, 2000))
  );
}
export type RuntimeHandle = { send: (type: string, payload?: unknown) => void };
export type RuntimeStorageScope = {
  accountId: string;
  projectId: string;
  packageId: string;
};
export type DocumentLocation = {
  path?: string;
  document_anchor?: string;
  articleId?: string;
  anchor?: string;
  selector?: string;
  heading?: string;
  heading_level?: number;
  heading_ordinal?: number;
};
export function OriginalRuntime({
  url,
  digest,
  kind,
  storageScope,
  networkOrigins = [],
  onEvent,
  handle,
}: {
  url: string;
  digest: string;
  kind: string;
  storageScope: RuntimeStorageScope;
  networkOrigins?: string[];
  onEvent: (e: RuntimeEvent) => void;
  handle: { current: RuntimeHandle | null };
}) {
  const iframe = useRef<HTMLIFrameElement>(null),
    eventRef = useRef(onEvent);
  eventRef.current = onEvent;
  const [identity] = useState(() => crypto.randomUUID());
  const [status, setStatus] = useState("正在读取原始资料…");
  const runtimePort = import.meta.env.VITE_RUNTIME_PORT || 5195;
  const networkKey = JSON.stringify(kind === "prototype" ? [] : networkOrigins);
  useEffect(() => {
    const controller = new AbortController();
    let port: MessagePort | null = null,
      disposed = false,
      transferred = false,
      lastSequence = 0,
      navigationRequest = "",
      origin = "";
    setStatus("正在读取原始资料…");
    if (iframe.current) iframe.current.src = "about:blank";
    const bytes = fetch(url, {
      signal: controller.signal,
      credentials: "same-origin",
      redirect: "error",
    }).then(async (r) => {
      if (!r.ok) throw new Error("无法读取资料，请检查当前版本和权限");
      const b = await r.arrayBuffer();
      const h = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", b)),
      )
        .map((n) => n.toString(16).padStart(2, "0"))
        .join("");
      if (h !== digest) throw new Error("资料摘要校验失败");
      return b;
    });
    bytes.catch((e) => {
      if (!disposed) setStatus(e.message);
    });
    const send = (type: string, payload: unknown = {}) => {
      const request = crypto.randomUUID();
      if (type === "target.reveal" || type === "document.reveal")
        navigationRequest = request;
      port?.postMessage({
        protocol_version: 1,
        runtime_instance_id: identity,
        epoch: 1,
        request_id: request,
        type,
        payload,
      });
    };
    const listener = (e: MessageEvent) => {
      if (
        e.source !== iframe.current?.contentWindow ||
        e.origin !== origin ||
        e.data?.type !== "vks.runtime.ready" ||
        e.data?.instance !== identity ||
        port
      )
        return;
      const channel = new MessageChannel();
      port = channel.port1;
      port.onmessage = async ({ data }) => {
        if (
          disposed ||
          data?.protocol_version !== 1 ||
          data?.runtime_instance_id !== identity ||
          !Number.isSafeInteger(data.sequence) ||
          data.sequence <= lastSequence
        )
          return;
        lastSequence = data.sequence;
        if (data.type === "runtime.ready" && !transferred) {
          transferred = true;
          try {
            const raw = await bytes;
            if (disposed) return;
            port?.postMessage(
              {
                protocol_version: 1,
                runtime_instance_id: identity,
                type: "material.load",
                payload: { bytes: raw },
              },
              [raw],
            );
          } catch (e) {
            if (!disposed) setStatus(String(e));
          }
        }
        if (data.type === "material.loaded") setStatus("");
        if (
          data.type !== "command.result" ||
          data.request_id === navigationRequest
        ) {
          if (data.type === "context.menu" && iframe.current) {
            const rect = iframe.current.getBoundingClientRect();
            const p = data.payload;
            if (
              !Number.isFinite(p?.x) ||
              !Number.isFinite(p?.y) ||
              p.x < 0 ||
              p.x > 1 ||
              p.y < 0 ||
              p.y > 1
            )
              return;
            data = {
              ...data,
              payload: {
                ...p,
                x: rect.left + p.x * rect.width,
                y: rect.top + p.y * rect.height,
              },
            };
          }
          eventRef.current(data);
        }
      };
      port.start();
      handle.current = { send };
      iframe.current?.contentWindow?.postMessage(
        { type: "vks.connect", instance: identity },
        origin,
        [channel.port2],
      );
    };
    window.addEventListener("message", listener);
    // The server accepts registration only from the configured Studio origin.
    // Material bytes and platform credentials never enter this request.
    void fetch(`http://localhost:${runtimePort}/register`, {
      method: "POST",
      mode: "cors",
      credentials: "omit",
      redirect: "error",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: kind === "prototype" ? "prototype" : "document",
        digest,
        storageScope,
        networkOrigins: JSON.parse(networkKey),
      }),
    })
      .then(async (response) => {
        const registration = await response.json();
        if (!response.ok)
          throw new Error(registration.error || "无法登记原始资料运行环境");
        const expected = new URL(registration.origin);
        if (
          expected.protocol !== "http:" ||
          !/^vks-[a-f0-9]{48}\.localhost$/.test(expected.hostname) ||
          expected.port !== String(runtimePort) ||
          typeof registration.registration !== "string"
        )
          throw new Error("原始资料运行环境地址无效");
        if (disposed) return;
        origin = expected.origin;
        if (iframe.current)
          iframe.current.src = `${origin}/?registration=${encodeURIComponent(registration.registration)}&instance=${identity}`;
      })
      .catch((error) => {
        if (!disposed) setStatus(error.message);
      });
    return () => {
      disposed = true;
      controller.abort();
      window.removeEventListener("message", listener);
      port?.close();
      handle.current = null;
    };
  }, [
    url,
    digest,
    kind,
    storageScope.accountId,
    storageScope.projectId,
    storageScope.packageId,
    networkKey,
    runtimePort,
    identity,
    handle,
  ]);
  return (
    <div className="runtime-wrap">
      {status && (
        <div className="runtime-status" role="status">
          {status}
        </div>
      )}
      <iframe
        ref={iframe}
        title={kind === "prototype" ? "原 HTML 原型" : "原 HTML 手册"}
        sandbox={
          "allow-scripts allow-same-origin allow-modals allow-downloads" +
          (kind === "prototype" ? "" : " allow-forms")
        }
      />
    </div>
  );
}
