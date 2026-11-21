import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const compiled = await build({
  entryPoints: [
    fileURLToPath(new URL("../../studio/src/taskEstimate.ts", import.meta.url)),
  ],
  bundle: true,
  write: false,
  format: "esm",
  platform: "node",
});
const { observeProgress, estimatedSeconds, remainingRange, roundRemaining } =
  await import(
    "data:text/javascript;base64," +
      Buffer.from(compiled.outputFiles[0].text).toString("base64")
  );

const progress = (previous, seconds, processed, run = {}) =>
  observeProgress(previous, {
    startedAt: 0,
    baseline: 0,
    total: 104,
    processed,
    at: seconds * 1000,
    ...run,
  });

test("initial waves and several polls without new results do not inflate the forecast", () => {
  let estimate = progress(null, 10, 1);
  estimate = progress(estimate, 14, 4);
  estimate = progress(estimate, 24, 8);
  assert.equal(estimatedSeconds(estimate, 28_000), null);
  estimate = progress(estimate, 30, 10);
  const first = estimatedSeconds(estimate, 30_000);
  assert.ok(first > 0);
  const unchanged = progress(estimate, 37, 10);
  assert.strictEqual(unchanged, estimate);
  assert.equal(estimatedSeconds(unchanged, 37_000), first - 7);
  assert.equal(estimatedSeconds(unchanged, 60_000), null);
});

test("a burst does not recalculate the finish time until the next calibration window", () => {
  let estimate = progress(null, 10, 4);
  estimate = progress(estimate, 20, 8);
  estimate = progress(estimate, 30, 12);
  const previousFinish = estimate.finishAt;
  const previousPace = estimate.pace;
  estimate = progress(estimate, 31, 20);
  assert.equal(estimate.finishAt, previousFinish);
  estimate = progress(estimate, 40, 28);
  assert.equal(estimate.calculatedAt, 40_000);
  assert.ok(estimate.pace < previousPace);
  assert.ok(estimate.pace > 1000, "recent measurements are smoothed");
  assert.ok(estimate.finishAt < previousFinish);
});

test("partial progress from a previous attempt does not supply a pace", () => {
  let estimate = progress(null, 10, 10);
  estimate = progress(estimate, 30, 25);
  const resumed = { startedAt: 100_000, baseline: 25 };
  assert.equal(progress(estimate, 100, 25, resumed), null);
  estimate = progress(estimate, 101, 28, resumed);
  assert.equal(estimatedSeconds(estimate, 101_000), null);
  estimate = progress(estimate, 121, 38, resumed);
  assert.equal(estimatedSeconds(estimate, 121_000), 132);
  assert.equal(roundRemaining(132), 135);
  assert.equal(roundRemaining(14), 15);
});

test("missing server or reordered samples do not generate a deadline", () => {
  assert.equal(progress(null, 0, 0), null);
  let estimate = progress(null, 10, 4);
  estimate = progress(estimate, 10, 8);
  assert.equal(estimatedSeconds(estimate, 10_000), null);
  assert.strictEqual(progress(estimate, 5, 12), estimate);
});

test("slow tasks still calibrate and a stall starts a fresh window", () => {
  let estimate = progress(null, 35, 1);
  estimate = progress(estimate, 70, 2);
  estimate = progress(estimate, 105, 3);
  assert.equal(estimate.pace, 35_000);
  assert.ok(estimatedSeconds(estimate, 140_000) > 0);
  assert.equal(estimatedSeconds(estimate, 175_000), null);
  estimate = progress(estimate, 200, 4);
  assert.equal(estimatedSeconds(estimate, 200_000), null);
  assert.equal(estimate.samples.length, 1);
});

test("history provides an initial range, then yields gradually to measured pace", () => {
  const history = { lower_seconds: 100, upper_seconds: 210 };
  assert.deepEqual(remainingRange(history, 0, null, 0), {
    lower: 100,
    upper: 210,
    source: "history",
  });
  assert.deepEqual(remainingRange(history, 20, 140, 1), {
    lower: 95,
    upper: 177.5,
    source: "blended",
  });
  assert.deepEqual(remainingRange(history, 20, 140, 2), {
    lower: 110,
    upper: 165,
    source: "blended",
  });
  assert.deepEqual(remainingRange(history, 20, 140, 4), {
    lower: 140,
    upper: 140,
    source: "measured",
  });
  assert.deepEqual(remainingRange(history, 220, null, 0), {
    lower: 0,
    upper: 0,
    source: "history",
  });
  assert.deepEqual(remainingRange(history, 220, 90, 1), {
    lower: 90,
    upper: 90,
    source: "measured",
  });
});
