import { createHash, timingSafeEqual } from "node:crypto";
import { createServer, request as requestLocal } from "node:http";
import { createInterface } from "node:readline";
import { pipeline } from "node:stream";
import { buffer } from "node:stream/consumers";
import { promisify } from "node:util";
import { createGzip, gzip } from "node:zlib";
import { isPrototypeContent, isStudioApi, isStudioAsset } from "./worker.mjs";

let secret = process.env.VKS_SITES_GATEWAY_TOKEN;
if (!secret) {
  const input = createInterface({ input: process.stdin, terminal: false });
  const { value: line } = await input[Symbol.asyncIterator]().next();
  input.close();
  secret = line?.trim() || "";
}
const token = Buffer.from(secret);
if (token.length < 32) throw new Error("VKS Sites 网关凭据至少需要 32 个字节");

const origins = new Set(
  (process.env.VKS_SITES_ORIGINS || "").split(",").map((origin) => origin.trim()).filter(Boolean),
);
if (origins.size === 0) throw new Error("请在 VKS_SITES_ORIGINS 中登记 Sites 来源");
const studioPort = Number(process.env.VKS_STUDIO_PORT || 5193);
const gatewayPort = Number(process.env.VKS_SITES_GATEWAY_PORT || 5294);
const localOrigin = "http://127.0.0.1:" + studioPort;
const forwarded = ["accept", "content-length", "content-type", "cookie", "idempotency-key", "if-match", "range", "x-csrf-token", "x-vks-model-access"];
const responseHeaders = ["accept-ranges", "cache-control", "content-disposition", "content-length", "content-range", "content-security-policy", "content-type", "referrer-policy", "set-cookie", "x-content-type-options", "x-frame-options", "x-request-id", "x-vks-instance-id"];
const compress = promisify(gzip);
const MAX_REVALIDATED_BYTES = 24 * 1024 * 1024;

function fail(response, status, message) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify({ error: { message } }));
}

async function revalidatePrototype(request, response, source, headers, useGzip) {
  try {
    // The upstream endpoint has already checked the current session, project
    // grant and exact version. Its authorized bytes decide any conditional hit.
    const raw = await buffer(source);
    if (response.destroyed) return;
    const tag = 'W/"' + createHash("sha256").update(raw).digest("hex") + '"';
    headers.etag = tag;
    headers["x-vks-prototype-cache"] = "revalidate";
    delete headers["accept-ranges"];
    if (useGzip) {
      headers["content-type"] = "application/gzip";
      headers["x-vks-prototype-encoding"] = "gzip";
    }
    if (request.headers["if-none-match"]?.split(",").some((value) => value.trim() === tag)) {
      delete headers["content-length"];
      response.writeHead(304, headers).end();
      return;
    }
    const data = useGzip ? await compress(raw, { level: 3 }) : raw;
    if (response.destroyed) return;
    headers["content-length"] = data.length;
    response.writeHead(200, headers).end(data);
  } catch {
    if (!response.destroyed) fail(response, 502, "原型资料传输失败");
  }
}

createServer((request, response) => {
  const sent = Buffer.from(request.headers["x-vks-sites-gateway"] || "");
  const origin = request.headers["x-vks-sites-origin"];
  if (sent.length !== token.length || !timingSafeEqual(sent, token) || !origins.has(origin)) {
    return fail(response, 403, "Mac 网关鉴权失败");
  }
  let route;
  try {
    if (!request.url.startsWith("/") || request.url.startsWith("//")) throw new Error();
    route = new URL(request.url, localOrigin);
  } catch {
    return fail(response, 400, "请求地址无效");
  }
  const reading = request.method === "GET" || request.method === "HEAD";
  const api = isStudioApi(route.pathname, request.method);
  if (!api && !(reading && isStudioAsset(route.pathname))) {
    return fail(response, 404, "未找到此页面或接口");
  }

  const headers = { Host: new URL(localOrigin).host };
  for (const name of forwarded) {
    if (request.headers[name] != null) headers[name] = request.headers[name];
  }
  if (!reading) headers.Origin = localOrigin;
  if (!api) delete headers.cookie;
  const upstream = requestLocal(
    new URL(route.pathname + route.search, localOrigin),
    { method: request.method, headers },
    (result) => {
      const outgoing = {};
      for (const name of responseHeaders) {
        if (result.headers[name] != null) outgoing[name] = result.headers[name];
      }
      outgoing["cache-control"] = "no-store";
      const prototype = (
        result.statusCode === 200 &&
        request.method === "GET" &&
        !request.headers.range &&
        isPrototypeContent(route.pathname)
      );
      const useGzip = request.headers["x-vks-prototype-encoding"] === "gzip";
      const size = Number(result.headers["content-length"]);
      if (prototype && request.headers["x-vks-prototype-cache"] === "revalidate" &&
          Number.isInteger(size) && size > 0 && size <= MAX_REVALIDATED_BYTES) {
        void revalidatePrototype(request, response, result, outgoing, useGzip);
        return;
      }
      if (prototype && useGzip) {
        // The browser decodes this explicitly before comparing the saved SHA-256.
        // Use a separate header so intermediaries do not implicitly decode it.
        delete outgoing["content-length"];
        delete outgoing["accept-ranges"];
        outgoing["content-type"] = "application/gzip";
        outgoing["x-vks-prototype-encoding"] = "gzip";
        outgoing["vary"] = "X-VKS-Prototype-Encoding";
        response.writeHead(200, outgoing);
        pipeline(result, createGzip({ level: 3 }), response, () => {});
        return;
      }
      response.writeHead(result.statusCode, outgoing);
      pipeline(result, response, () => {});
    },
  );
  upstream.on("error", () => {
    if (response.headersSent) response.destroy();
    else fail(response, 502, "Mac 上的 Studio 暂时不可用");
  });
  request.on("aborted", () => upstream.destroy());
  response.on("close", () => {
    if (!response.writableEnded) upstream.destroy();
  });
  request.pipe(upstream);
}).listen(gatewayPort, "127.0.0.1", () => {
  console.log("VKS Sites Mac gateway · http://127.0.0.1:" + gatewayPort);
});
