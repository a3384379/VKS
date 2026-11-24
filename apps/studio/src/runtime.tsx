import { useEffect, useRef, useState } from "react";
import { apiResponse } from "../../web/src/api";
import runtimeShell from "../../runtime/index.html?raw";
import runtimeBridge from "../../runtime/bridge.js?raw";
export const hostedStudio =
  typeof document !== "undefined" &&
  document.querySelector('meta[name="vks-hosted-studio"]') !== null;
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
export type PrototypePosition = Pick<Target, "route" | "frame_path" | "state">;
export type RevealResult = {
  ok: boolean;
  message: string;
  navigation_level?: "page" | "control" | "state";
  route?: string;
  frame_path?: string[];
  target?: Target;
  observed_state?: string;
  state_verified: boolean;
};
export function samePage(
  a: Pick<Target, "route" | "frame_path">,
  b: Pick<Target, "route" | "frame_path">,
) {
  return a.route === b.route && JSON.stringify(a.frame_path) === JSON.stringify(b.frame_path);
}
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
export type RuntimeHandle = {
  instanceId: string;
  send: (type: string, payload?: unknown) => string;
};
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

function prototypeFrameUrl(instance: string) {
  const config = JSON.stringify({
    mode: "prototype",
    host: location.origin,
    instance,
  }).replaceAll("<", "\\u003c");
  const policy =
    "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob: data:; style-src 'unsafe-inline' blob: data:; img-src blob: data:; font-src blob: data:; media-src blob: data:; frame-src blob: data: about:; worker-src blob:; connect-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'";
  const html = runtimeShell
    .replace("<head>", `<head><meta http-equiv="Content-Security-Policy" content="${policy}">`)
    .replace("<!--RUNTIME_CONFIG-->", `<script id="vks-runtime-config" type="application/json">${config}</script>`)
    .replace('<script src="/bridge.js"></script>', () =>
      "<script>" + runtimeBridge.replace(/<\/script/gi, "<\\/script") + "</script>",
    );
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

const prototypeCacheName = "vks-private-prototype-v1";

function prototypeCacheKey(accountId: string) {
  return location.origin + "/__vks_private_prototype?account=" + encodeURIComponent(accountId);
}

export async function clearPrototypeCache(accountId: string) {
  if (!hostedStudio || !("caches" in window)) return;
  const cache = await caches.open(prototypeCacheName).catch(() => undefined);
  await cache?.delete(prototypeCacheKey(accountId), { ignoreVary: true }).catch(() => {});
}

async function verifiedMaterial(
  url: string, digest: string, signal: AbortSignal, compressed = false, accountId?: string,
) {
  const cache = accountId && "caches" in window
    ? await caches.open(prototypeCacheName).catch(() => undefined)
    : undefined;
  const key = accountId ? prototypeCacheKey(accountId) : "";
  const tag = 'W/"' + digest + '"';
  const candidate = await cache?.match(key, { ignoreVary: true }).catch(() => undefined);
  const cached = candidate?.headers.get("etag") === tag ? candidate : undefined;
  const network = await apiResponse(url, {
    signal,
    redirect: "error",
    cache: "no-store",
    ...(compressed ? { headers: {
      "X-VKS-Prototype-Cache": "revalidate",
      ...(typeof DecompressionStream === "function" ? { "X-VKS-Prototype-Encoding": "gzip" } : {}),
      ...(cached ? { "If-None-Match": tag } : {}),
    } } : {}),
  }, { acceptNotModified: !!cached });
  const response = network.status === 304 && cached ? cached : network;
  const save = cache && network.status === 200 &&
    network.headers.get("x-vks-prototype-cache") === "revalidate" &&
    network.headers.get("etag") === tag
    ? network.clone()
    : undefined;
  const body = response.headers.get("X-VKS-Prototype-Encoding") === "gzip" && response.body
    ? new Response(response.body.pipeThrough(new DecompressionStream("gzip")))
    : response;
  let bytes: ArrayBuffer;
  try {
    bytes = await body.arrayBuffer();
    const hash = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (hash !== digest) throw new Error("资料摘要校验失败");
  } catch (error) {
    void save?.body?.cancel().catch(() => {});
    if (!signal.aborted && network.status === 304 && cache) {
      await cache.delete(key, { ignoreVary: true }).catch(() => {});
      return verifiedMaterial(url, digest, signal, compressed);
    }
    throw error;
  }
  // Keep one verified package per account on this device. A later viewer
  // always checks the original URL and current grant before using these bytes.
  if (save && cache) void cache.put(key, save).then(async () => {
    if (signal.aborted) {
      const entry = await cache.match(key, { ignoreVary: true });
      if (entry?.headers.get("etag") === tag)
        await cache.delete(key, { ignoreVary: true });
    }
  }).catch(() => {});
  return bytes;
}

// One current prototype per authenticated catalog scope. A copy is transferred
// to the isolated frame so it cannot detach the prefetched bytes.
export class PrototypeMaterial {
  private controller = new AbortController();
  private pending?: Promise<ArrayBuffer>;
  constructor(private url: string, private digest: string, private accountId: string) {}
  load() {
    return (this.pending ??= verifiedMaterial(this.url, this.digest, this.controller.signal, true, this.accountId)
      .catch((error) => {
        this.pending = undefined;
        throw error;
      }));
  }
  dispose() {
    this.controller.abort();
    this.pending = undefined;
  }
}

export function OriginalRuntime({
  url,
  digest,
  kind,
  storageScope,
  networkOrigins = [],
  prefetchedMaterial,
  inventoryOnly = false,
  onEvent,
  onFailure,
  handle,
}: {
  url: string;
  digest: string;
  kind: string;
  storageScope: RuntimeStorageScope;
  networkOrigins?: string[];
  prefetchedMaterial?: PrototypeMaterial;
  inventoryOnly?: boolean;
  onEvent: (e: RuntimeEvent) => void;
  onFailure?: (message: string) => void;
  handle: { current: RuntimeHandle | null };
}) {
  const iframe = useRef<HTMLIFrameElement>(null),
    eventRef = useRef(onEvent),
    failureRef = useRef(onFailure);
  eventRef.current = onEvent;
  failureRef.current = onFailure;
  const [identity] = useState(() => crypto.randomUUID());
  const [status, setStatus] = useState("正在读取原始资料…");
  const configuredPort = document.querySelector<HTMLMetaElement>(
    'meta[name="vks-runtime-port"]',
  )?.content;
  const runtimePort = configuredPort || import.meta.env.VITE_RUNTIME_PORT || 5195;
  const networkKey = JSON.stringify(kind === "prototype" ? [] : networkOrigins);
  useEffect(() => {
    if (hostedStudio && kind !== "prototype") {
      const message = "远程站点可使用标准文档视图。原样文档请在 Mac 的 Studio 中打开。";
      setStatus(message);
      failureRef.current?.(message);
      return;
    }
    const controller = new AbortController();
    const opaquePrototype = hostedStudio && kind === "prototype";
    const instance = opaquePrototype ? crypto.randomUUID() : identity;
    let port: MessagePort | null = null,
      disposed = false,
      transferred = false,
      lastSequence = 0,
      navigationRequest = "",
      origin = opaquePrototype ? "null" : "";
    setStatus("正在读取原始资料…");
    if (iframe.current) iframe.current.src = "about:blank";
    const failed = (error: unknown) => {
      if (disposed) return;
      const message = error instanceof Error ? error.message : String(error);
      setStatus(message);
      failureRef.current?.(message);
    };
    const bytes = prefetchedMaterial
      ? prefetchedMaterial.load()
      : verifiedMaterial(url, digest, controller.signal, opaquePrototype);
    void bytes.catch(failed);
    const send = (type: string, payload: unknown = {}) => {
      const request = crypto.randomUUID();
      if (type === "target.reveal" || type === "document.reveal")
        navigationRequest = request;
      if (type === "target.cancel" || type === "document.cancel") navigationRequest = "";
      port?.postMessage({
        protocol_version: 1,
        runtime_instance_id: instance,
        epoch: 1,
        request_id: request,
        type,
        payload,
      });
      return request;
    };
    const listener = (e: MessageEvent) => {
      if (
        e.source !== iframe.current?.contentWindow ||
        e.origin !== origin ||
        e.data?.type !== "vks.runtime.ready" ||
        e.data?.instance !== instance ||
        (opaquePrototype && e.ports.length !== 1) ||
        port
      )
        return;
      const channel = opaquePrototype ? null : new MessageChannel();
      const peer = channel ? channel.port1 : e.ports[0];
      port = peer;
      port.onmessage = async ({ data }) => {
        if (
          disposed ||
          data?.protocol_version !== 1 ||
          data?.runtime_instance_id !== instance ||
          !Number.isSafeInteger(data.sequence) ||
          data.sequence <= lastSequence
        )
          return;
        lastSequence = data.sequence;
        if (data.type === "runtime.ready" && !transferred) {
          transferred = true;
          try {
            const verified = await bytes;
            if (disposed) return;
            const raw = prefetchedMaterial ? verified.slice(0) : verified;
            if (kind === "prototype") setStatus("正在初始化原型画面…");
            port?.postMessage(
              {
                protocol_version: 1,
                runtime_instance_id: instance,
                type: "material.load",
                payload: { bytes: raw },
              },
              [raw],
            );
          } catch {
            // The verified material request reports failures itself.
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
      handle.current = { instanceId: instance, send };
      if (channel) {
        iframe.current?.contentWindow?.postMessage(
          { type: "vks.connect", instance },
          origin,
          [channel.port2],
        );
      }
    };
    window.addEventListener("message", listener);
    // The server accepts registration only from the configured Studio origin.
    // Material bytes and platform credentials never enter this request.
    if (opaquePrototype) {
      if (iframe.current) iframe.current.src = prototypeFrameUrl(instance);
    } else void fetch(`http://localhost:${runtimePort}/register`, {
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
        iframe.current?.contentWindow?.location.replace(
          `${origin}/?registration=${encodeURIComponent(registration.registration)}&instance=${instance}`,
        );
      })
      .catch(failed);
    return () => {
      disposed = true;
      controller.abort();
      window.removeEventListener("message", listener);
      port?.close();
      if (handle.current?.instanceId === instance) handle.current = null;
    };
  }, [
    url,
    digest,
    kind,
    prefetchedMaterial,
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
        title={
          inventoryOnly
            ? "预扫描原型"
            : kind === "prototype"
              ? "原 HTML 原型"
              : "原 HTML 手册"
        }
        tabIndex={inventoryOnly ? -1 : undefined}
        sandbox={
          "allow-scripts allow-same-origin" +
          (inventoryOnly ? "" : " allow-modals allow-downloads") +
          (kind === "prototype" ? "" : " allow-forms")
        }
      />
    </div>
  );
}
