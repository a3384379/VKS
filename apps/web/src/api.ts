let csrf = "";
let sessionRevision = 0;
const unauthenticated = new Set<() => void>();

export function setCsrf(value?: string) {
  const next = value || "";
  if (next !== csrf) sessionRevision++;
  csrf = next;
}

export function observeUnauthenticated(listener: () => void) {
  unauthenticated.add(listener);
  return () => { unauthenticated.delete(listener); };
}

function checkSessionRevision(revision: number) {
  if (unauthenticated.size && revision !== sessionRevision)
    throw new DOMException("会话已切换", "AbortError");
}
export class ApiError extends Error {
  code: string;
  status: number;
  details: unknown;
  constructor(
    message: string,
    status: number,
    code: string,
    details?: unknown,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}
export type ApiMeta = {
  next_cursor?: string | null;
  request_id?: string;
  [key: string]: unknown;
};
export async function apiResponse(
  path: string,
  init: RequestInit = {},
  options: { acceptNotModified?: boolean } = {},
): Promise<Response> {
  const revision = sessionRevision;
  const headers = new Headers(init.headers);
  if (
    init.body &&
    !(init.body instanceof Blob) &&
    !(init.body instanceof ArrayBuffer) &&
    !headers.has("Content-Type")
  )
    headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method)) {
    headers.set("X-CSRF-Token", csrf);
  }
  let target = path;
  if (/^https?:\/\//.test(path)) {
    const url = new URL(path);
    if (url.origin !== location.origin)
      throw new Error("上传地址不属于当前工作空间");
    target = url.pathname + url.search;
  }
  target = target.startsWith("/api/") ? target : `/api/v1${target}`;
  const response = await fetch(
    target,
    { ...init, headers, credentials: "same-origin" },
  );
  checkSessionRevision(revision);
  if (options.acceptNotModified && response.status === 304) return response;
  if (!response.ok) {
    const json = await response.json().catch(() => ({}));
    checkSessionRevision(revision);
    if (
      response.status === 401 &&
      !/^\/api\/v[0-9]+\/auth\/(login|session)(?:\?|$)/.test(target)
    ) for (const listener of unauthenticated) listener();
    throw new ApiError(
      json.error?.message ||
        json.detail?.message ||
        (typeof json.detail === "string"
          ? json.detail
          : "服务暂时不可用，请稍后重试"),
      response.status,
      json.error?.code || "VKS_REQUEST_FAILED",
      json.error?.details || json.detail,
    );
  }
  return response;
}
async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<{ data?: T; meta?: ApiMeta }> {
  const revision = sessionRevision;
  const response = await apiResponse(path, init);
  const json = await response.json().catch(() => ({}));
  checkSessionRevision(revision);
  return json;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const json = await request<T>(path, init);
  return (json.data ?? json) as T;
}
export async function apiPage<T>(
  path: string,
  init: RequestInit = {},
): Promise<{ data: T; meta: ApiMeta }> {
  const json = await request<T>(path, init);
  return { data: (json.data ?? json) as T, meta: json.meta || {} };
}
export function mutation(
  method: string,
  body: unknown,
  version?: number,
): RequestInit {
  return {
    method,
    body: JSON.stringify(body),
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
      ...(version !== undefined ? { "If-Match": `"${version}"` } : {}),
    },
  };
}
export function date(value?: string) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Asia/Shanghai",
      });
}
export function bytes(value = 0) {
  return value > 1048576
    ? `${(value / 1048576).toFixed(1)} MB`
    : value > 1024
      ? `${(value / 1024).toFixed(1)} KB`
      : `${value} B`;
}
export async function sha256(file: File) {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
    ),
  )
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
}
