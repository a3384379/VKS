import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { build } from "esbuild";

const destination = process.argv[2];
if (!destination || process.argv.length !== 3) {
  throw new Error("用法: node deploy/sites/stage.mjs <独立 Sites 源码目录>");
}
const checkout = resolve(destination);
const marker = resolve(checkout, ".openai/hosting.json");
const hosting = JSON.parse(await readFile(marker, "utf8"));
if (!hosting.project_id || hosting.static) throw new Error("需要已登记的 Worker Sites 目录");

const built = new URL("../../apps/studio/dist/", import.meta.url);
const html = await readFile(new URL("index.html", built), "utf8");
const mermaid = await readFile(new URL("../../packages/handbook-template/standalone/mermaid.js", import.meta.url));
const assetName = (kind, bytes, suffix) =>
  `vks-document-assistant-${kind}-${createHash("sha256").update(bytes).digest("hex").slice(0, 16)}.${suffix}`;
const mermaidFile = assetName("mermaid", mermaid, "js");
const renderer = fileURLToPath(new URL("../../services/api/vks_api/document_assistant.py", import.meta.url));
const shell = gzipSync(execFileSync(process.env.VKS_BUILD_PYTHON || "python3", [
  "-c",
  "import runpy, sys; sys.stdout.buffer.write(runpy.run_path(sys.argv[1])['assistant_html'](sys.argv[2]).encode('utf-8'))",
  renderer,
  "/assets/" + mermaidFile,
]));
const shellFile = assetName("shell", shell, "html.gz");
await mkdir(resolve(checkout, "client"), { recursive: true });
// Vite's production output consists of these two entry pages and hashed assets.
await rm(resolve(checkout, "client/assets"), { recursive: true, force: true });
await cp(fileURLToPath(new URL("assets/", built)), resolve(checkout, "client/assets"), { recursive: true });
await writeFile(resolve(checkout, "client/assets", mermaidFile), mermaid);
await writeFile(resolve(checkout, "client/assets", shellFile), shell);
const diagramMeta = await Promise.all(["mermaid", "plantuml"].map(async (kind) => {
  const { outputFiles } = await build({
    entryPoints: [fileURLToPath(new URL("../../packages/markdown-runtime/src/diagram-frame.ts", import.meta.url))],
    bundle: true,
    minify: true,
    format: "iife",
    platform: "browser",
    target: "es2022",
    define: {
      __VKS_DIAGRAM_ENGINE__: JSON.stringify(kind),
      "import.meta.url": "location.href",
    },
    write: false,
  });
  const script = outputFiles[0].contents;
  const file = `vks-diagram-${kind}-${createHash("sha256").update(script).digest("hex").slice(0, 16)}.js`;
  await writeFile(resolve(checkout, "client/assets", file), script);
  return `<meta name="vks-diagram-${kind}" content="/assets/${file}">`;
}));
await cp(new URL("diagram-frame.html", built), resolve(checkout, "client/diagram-frame.html"));
await writeFile(
  resolve(checkout, "client/index.html"),
  html.replace("</head>", `<meta name="vks-hosted-studio" content="1"><meta name="vks-document-assistant" content="/assets/${shellFile}">${diagramMeta.join("")}</head>`),
);
for (const item of ["worker.mjs", "build.mjs"]) {
  await cp(new URL(item, import.meta.url), resolve(checkout, item));
}
await writeFile(resolve(checkout, "package.json"), JSON.stringify({
  name: "vks-studio-sites", private: true, type: "module",
  scripts: { build: "node build.mjs" },
}, null, 2) + "\n");
await writeFile(resolve(checkout, ".gitignore"), "dist/\n.env*\n");
