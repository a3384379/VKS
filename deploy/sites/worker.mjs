const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const ASSET = /^\/assets\/[A-Za-z0-9_.-]+$/;
const VERSIONED_RUNTIME = /^\/assets\/vks-(?:document-assistant-(?:shell-[a-f0-9]{16}\.html\.gz|mermaid-[a-f0-9]{16}\.js)|diagram-(?:mermaid|plantuml)-[a-f0-9]{16}\.js)$/;
const API = /^\/api\/v(?:1|3)\/[A-Za-z0-9_./-]+$/;
const PROTOTYPE_CONTENT = /^\/api\/v3\/projects\/[a-z0-9_-]+\/interactive\/packages\/[a-z0-9_-]+\/content$/i;

export const isPrototypeContent = (path) => PROTOTYPE_CONTENT.test(path);

// Keep the bootstrap token exchange on the Mac. The hosted Studio uses
// the normal VKS account login and keeps the user's project permissions.
export function isStudioApi(path, method) {
  return (
    API.test(path) &&
    !path.includes("//") &&
    (method !== "POST" || path.replace(/\/$/, "") !== "/api/v1/auth/session")
  );
}

export function isStudioAsset(path) {
  return path === "/" || path === "/index.html" || path === "/diagram-frame.html" || ASSET.test(path);
}

function problem(status, code, message) {
  return Response.json(
    { error: { code, message } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

function sessionCookie(header) {
  return header?.split(";").map((value) => value.trim()).find((value) =>
    value.startsWith("vks_session=")
  );
}

function localPreview(url, env) {
  return env.VKS_LOCAL_PREVIEW === "1" && url.origin === "http://127.0.0.1:5293";
}

function withSiteHeaders(response, path, api) {
  const headers = new Headers(response.headers);
  // Preserve only the VKS session, and bind it to the Site's HTTPS origin.
  const cookie = headers.get("Set-Cookie");
  headers.delete("Set-Cookie");
  if (api && cookie?.startsWith("vks_session=")) {
    const session = cookie.replace(/;\s*Domain=[^;]+/gi, "");
    headers.set("Set-Cookie", /;\s*Secure(?:;|$)/i.test(session) ? session : session + "; Secure");
  }
  headers.set("Cache-Control", response.ok && VERSIONED_RUNTIME.test(path)
    ? "private, max-age=31536000, immutable"
    : "no-store");
  if (ASSET.test(path)) headers.set("Access-Control-Allow-Origin", "*");
  return new Response(response.status === 304 ? null : response.body, { status: response.status, headers });
}

async function upstream(request, env, url, api) {
  if (!env.VKS_MAC_GATEWAY || !env.VKS_SITES_GATEWAY_TOKEN) {
    return problem(503, "VKS_MAC_NOT_CONFIGURED", "Mac 上的 Studio 连接尚未配置");
  }
  let base;
  try {
    base = new URL(env.VKS_MAC_GATEWAY);
  } catch {
    return problem(503, "VKS_MAC_NOT_CONFIGURED", "Mac 网关地址无效");
  }
  if (base.protocol !== "https:" && !(localPreview(url, env) && base.protocol === "http:" && base.hostname === "127.0.0.1")) {
    return problem(503, "VKS_MAC_NOT_CONFIGURED", "Mac 网关需要 HTTPS 地址");
  }
  const target = new URL(url.pathname + url.search, base);
  const headers = new Headers({
    "X-VKS-Sites-Gateway": env.VKS_SITES_GATEWAY_TOKEN,
    "X-VKS-Sites-Origin": url.origin,
  });
  for (const name of ["accept", "content-type", "if-match", "if-none-match", "idempotency-key", "range", "x-csrf-token", "x-vks-model-access", "x-vks-prototype-cache", "x-vks-prototype-encoding"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const cookie = api && sessionCookie(request.headers.get("cookie"));
  if (cookie) headers.set("Cookie", cookie);
  if (env.CF_ACCESS_CLIENT_ID && env.CF_ACCESS_CLIENT_SECRET) {
    headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
    headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  }
  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.body,
      ...(request.body ? { duplex: "half" } : {}),
      redirect: "manual",
      signal: request.signal,
    });
    if (response.status === 502 || response.status === 503 || response.status === 504 || response.status === 530) {
      await response.body?.cancel();
      return problem(503, "VKS_MAC_OFFLINE", "Mac 上的 Studio 暂时不可用，请确认 Mac 和隧道正在运行");
    }
    if (response.status >= 300 && response.status < 400 && response.status !== 304) {
      await response.body?.cancel();
      return problem(503, "VKS_MAC_UNREACHABLE", "Mac 网关返回了重定向，请检查连接配置");
    }
    return withSiteHeaders(response, url.pathname, api);
  } catch {
    return problem(503, "VKS_MAC_OFFLINE", "Mac 上的 Studio 暂时不可用，请确认 Mac 和隧道正在运行");
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method;
    const reading = method === "GET" || method === "HEAD";
    const api = isStudioApi(url.pathname, method);
    if (!request.headers.get("oai-authenticated-user-id") && !localPreview(url, env)) {
      return problem(401, "VKS_SITE_SIGN_IN_REQUIRED", "请用站点所有者的 ChatGPT 账号登录");
    }
    if (api && (reading || WRITE_METHODS.has(method))) {
      if (!reading && request.headers.get("Origin") !== url.origin) {
        return problem(403, "VKS_SITE_ORIGIN_DENIED", "跨来源写请求被拒绝");
      }
      return upstream(request, env, url, true);
    }
    if (reading && isStudioAsset(url.pathname)) {
      if (env.ASSETS) {
        const response = await env.ASSETS.fetch(request);
        return withSiteHeaders(response, url.pathname, false);
      }
      return upstream(request, env, url, false);
    }
    return problem(404, "VKS_SITE_NOT_FOUND", "未找到此页面或接口");
  },
};
