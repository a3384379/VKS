import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";
import { transform } from "esbuild";

const source = await fs.readFile(
  new URL(
    "../../../packages/assistant-runtime/src/floating.ts",
    import.meta.url,
  ),
  "utf8",
);
const compiled = await transform(source, { loader: "ts", format: "esm" });
const { clampWindowBox } = await import(
  "data:text/javascript;base64," + Buffer.from(compiled.code).toString("base64")
);
test("shared floating geometry fits initial and saved panels inside small viewports", () => {
  assert.deepEqual(
    clampWindowBox(
      { left: 900, top: 800, width: 600, height: 700 },
      undefined,
      { width: 220, height: 230 },
    ),
    { left: 8, top: 8, width: 204, height: 214 },
  );
  assert.deepEqual(
    clampWindowBox(
      { left: 30, top: 40, width: 54, height: 54 },
      { width: 0, height: 0 },
      { width: 1280, height: 900 },
    ),
    { left: 30, top: 40, width: 54, height: 54 },
  );
  assert.deepEqual(
    clampWindowBox(
      { left: -30, top: -40, width: 200, height: 200 },
      undefined,
      { width: 1280, height: 900 },
    ),
    { left: 8, top: 8, width: 360, height: 280 },
  );
});
