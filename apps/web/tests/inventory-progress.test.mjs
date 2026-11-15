import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const compiled = await build({
  entryPoints: [
    fileURLToPath(
      new URL("../../studio/src/inventoryProtocol.ts", import.meta.url),
    ),
  ],
  bundle: true,
  write: false,
  format: "esm",
  platform: "node",
  define: { "process.env.NODE_ENV": '"production"' },
});
const { mergeScanSnapshot, scanReceipt } = await import(
  "data:text/javascript;base64," +
    Buffer.from(compiled.outputFiles[0].text).toString("base64")
);

test("late page ACK cannot roll back independently completed AI, but retry polls may reduce failed count", () => {
  const newer = {
    id: "run",
    row_version: 3,
    pages_completed: 2,
    ai: { completed: 4, saved: 3, failed: 2, queued: 0 },
  };
  const late = {
    id: "run",
    row_version: 3,
    pages_completed: 2,
    ai: { completed: 2, saved: 1, failed: 0, queued: 2 },
  };
  assert.deepEqual(mergeScanSnapshot(newer, late, true).ai, newer.ai);
  assert.equal(
    mergeScanSnapshot(newer, { ...late, row_version: 2 }, true),
    newer,
  );
  const retry = {
    ...late,
    ai: { completed: 4, saved: 3, failed: 0, queued: 2 },
  };
  assert.deepEqual(mergeScanSnapshot(newer, retry).ai, retry.ai);
  const newPage = { ...late, row_version: 4, pages_completed: 3 };
  const accepted = mergeScanSnapshot(newer, newPage, true);
  assert.equal(accepted.pages_completed, 3);
  assert.deepEqual(accepted.ai, newer.ai);
});

test("page progress requires an ACK bound to its exact persistent run", () => {
  const ack = {
    scan_run: {
      id: "run",
      row_version: 4,
      pages_completed: 3,
      pages_attempted: 3,
      pages_failed: 0,
      controls: 9,
    },
  };
  assert.equal(scanReceipt(ack, "run"), ack);
  assert.throws(() => scanReceipt({}, "run"), /进度确认/);
  assert.throws(() => scanReceipt(ack, "other"), /进度确认/);
  assert.throws(
    () => scanReceipt({ scan_run: { ...ack.scan_run, controls: -1 } }, "run"),
    /进度确认/,
  );
});
