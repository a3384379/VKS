import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.mjs";

const site = "https://studio.chatgpt.site";
const user = { "oai-authenticated-user-id": "site-owner" };

test("requires Site identity even when local preview is configured", async () => {
  const result = await worker.fetch(new Request(site + "/api/v1/projects"), {
    VKS_LOCAL_PREVIEW: "1",
  });
  assert.equal(result.status, 401);
});

test("blocks cross-origin writes and remote bootstrap", async () => {
  const crossOrigin = await worker.fetch(new Request(site + "/api/v1/projects", {
    method: "POST", headers: { ...user, Origin: "https://other.example" },
  }), {});
  assert.equal(crossOrigin.status, 403);
  const bootstrap = await worker.fetch(new Request(site + "/api/v1/auth/session/", {
    method: "POST", headers: { ...user, Origin: site },
  }), {});
  assert.equal(bootstrap.status, 404);
});

test("serves client assets with diagram CORS and without gateway credentials", async () => {
  const request = new Request(site + "/assets/frame.js", { headers: user });
  const result = await worker.fetch(request, {
    ASSETS: { fetch: async () => new Response("frame") },
  });
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("access-control-allow-origin"), "*");
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.equal(await result.text(), "frame");
});

test("allows private browser caching only for a successful versioned assistant asset", async () => {
  const path = "/assets/vks-document-assistant-shell-0123456789abcdef.html.gz";
  const request = new Request(site + path, { headers: user });
  const ok = await worker.fetch(request, {
    ASSETS: { fetch: async () => new Response("shell") },
  });
  assert.equal(ok.headers.get("cache-control"), "private, max-age=31536000, immutable");
  const missing = await worker.fetch(request, {
    ASSETS: { fetch: async () => new Response(null, { status: 404 }) },
  });
  assert.equal(missing.headers.get("cache-control"), "no-store");
});

test("preserves an authenticated prototype revalidation without a response body", async (context) => {
  const tag = 'W/"' + "0".repeat(64) + '"';
  context.mock.method(globalThis, "fetch", async (_upstream, request) => {
    assert.equal(request.headers.get("if-none-match"), tag);
    assert.equal(request.headers.get("cookie"), "vks_session=test");
    return new Response(null, {
      status: 304, headers: { ETag: tag, "X-VKS-Prototype-Cache": "revalidate" },
    });
  });
  const result = await worker.fetch(new Request(site + "/api/v3/projects/a/interactive/packages/b/content", {
    headers: { ...user, Cookie: "vks_session=test", "If-None-Match": tag, "X-VKS-Prototype-Cache": "revalidate" },
  }), { VKS_MAC_GATEWAY: "https://gateway.example", VKS_SITES_GATEWAY_TOKEN: "test-token" });
  assert.equal(result.status, 304);
  assert.equal(result.body, null);
  assert.equal(result.headers.get("etag"), tag);
  assert.equal(result.headers.get("cache-control"), "no-store");
});

test("streams a same-origin write and forwards only the VKS session cookie", async (context) => {
  context.mock.method(globalThis, "fetch", async (target, request) => {
    assert.equal(target.href, "https://gateway.example/api/v1/projects?mode=active");
    assert.equal(request.headers.get("cookie"), "vks_session=test");
    assert.equal(request.headers.get("x-csrf-token"), "csrf");
    assert.equal(request.headers.get("x-vks-sites-origin"), site);
    assert.equal(request.headers.get("x-vks-sites-gateway"), "test-token");
    assert.equal(request.headers.get("oai-authenticated-user-id"), null);
    assert.equal(await new Response(request.body).text(), '{"name":"测试"}');
    return new Response("saved", {
      headers: { "Set-Cookie": "vks_session=next; Path=/; HttpOnly; SameSite=Strict; Domain=gateway.example" },
    });
  });
  const result = await worker.fetch(new Request(site + "/api/v1/projects?mode=active", {
    method: "POST",
    headers: {
      ...user, Origin: site, Cookie: "site_login=private; vks_session=test",
      "X-CSRF-Token": "csrf", "Content-Type": "application/json",
    },
    body: '{"name":"测试"}',
  }), { VKS_MAC_GATEWAY: "https://gateway.example", VKS_SITES_GATEWAY_TOKEN: "test-token" });
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("set-cookie"), "vks_session=next; Path=/; HttpOnly; SameSite=Strict; Secure");
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.equal(await result.text(), "saved");
});
