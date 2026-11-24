import { cp, mkdir, writeFile } from "node:fs/promises";

await mkdir("dist/server", { recursive: true });
await cp("client", "dist/client", { recursive: true });
await cp("worker.mjs", "dist/server/index.js");
await writeFile(
  "dist/server/wrangler.json",
  JSON.stringify({
    main: "index.js",
    compatibility_date: "2026-07-01",
    assets: { directory: "../client", binding: "ASSETS", run_worker_first: true },
  }) + "\n",
);
