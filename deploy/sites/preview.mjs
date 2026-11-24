import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve } from "node:path";
import { createInterface } from "node:readline";
import { Readable, pipeline } from "node:stream";
import worker from "./worker.mjs";

const input = createInterface({ input: process.stdin, terminal: false });
const { value: token } = await input[Symbol.asyncIterator]().next();
input.close();
if (!token?.trim()) throw new Error("预览需要通过标准输入接收网关凭据");
const client = resolve(process.argv[2] || ".vks/studio-sites/hosting/client");
const contentTypes = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
};
const origin = "http://127.0.0.1:5293";
const env = {
  VKS_LOCAL_PREVIEW: "1",
  VKS_MAC_GATEWAY: "http://127.0.0.1:" + (process.env.VKS_SITES_GATEWAY_PORT || 5294),
  VKS_SITES_GATEWAY_TOKEN: token.trim(),
  ASSETS: {
    async fetch(request) {
      const path = new URL(request.url).pathname;
      const file = resolve(client, path === "/" ? "index.html" : path.slice(1));
      try {
        const metadata = await stat(file);
        if (!metadata.isFile()) return new Response(null, { status: 404 });
        return new Response(
          request.method === "HEAD" ? null : Readable.toWeb(createReadStream(file)),
          { headers: { "Content-Type": contentTypes[extname(file)] || "application/octet-stream" } },
        );
      } catch {
        return new Response(null, { status: 404 });
      }
    },
  },
};

createServer(async (incoming, outgoing) => {
  if (incoming.headers.host !== "127.0.0.1:5293" || !incoming.url.startsWith("/") || incoming.url.startsWith("//")) {
    outgoing.writeHead(400).end();
    return;
  }
  try {
    const reading = incoming.method === "GET" || incoming.method === "HEAD";
    const request = new Request(origin + incoming.url, {
      method: incoming.method,
      headers: incoming.headers,
      ...(reading ? {} : { body: Readable.toWeb(incoming), duplex: "half" }),
    });
    const response = await worker.fetch(request, env);
    outgoing.writeHead(response.status, Object.fromEntries(response.headers));
    if (response.body) pipeline(Readable.fromWeb(response.body), outgoing, () => {});
    else outgoing.end();
  } catch {
    if (outgoing.headersSent) outgoing.destroy();
    else outgoing.writeHead(502).end();
  }
}).listen(5293, "127.0.0.1", () => console.log("VKS Sites 本机预览 · " + origin));
