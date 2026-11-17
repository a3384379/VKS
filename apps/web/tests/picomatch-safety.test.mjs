import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = new URL("../../../", import.meta.url);
const patched = fileURLToPath(
  new URL("packages/picomatch-safe/index.js", root),
);
const require = createRequire(new URL("package.json", root));
const pm = require(patched);

test("build glob consumers resolve the repaired local implementation", () => {
  assert.equal(
    realpathSync(require.resolve("picomatch")),
    realpathSync(patched),
  );
  for (const name of ["vite", "fdir", "tinyglobby"]) {
    const consumer = createRequire(
      new URL(`node_modules/${name}/package.json`, root),
    );
    assert.equal(
      realpathSync(consumer.resolve("picomatch")),
      realpathSync(patched),
      name,
    );
  }
});

test("normal build globs and safe repeated forms keep their matching behavior", () => {
  for (const [glob, input, expected] of [
    ["**/*.{ts,tsx}", "apps/studio/src/main.tsx", true],
    ["**/*.{ts,tsx}", "apps/studio/src/style.css", false],
    ["{apps,packages}/**/*.js", "packages/runtime/index.js", true],
    ["**/!(*.spec).ts", "apps/studio/src/main.ts", true],
    ["**/!(*.spec).ts", "apps/studio/src/main.spec.ts", false],
    ["+(ab)", "abab", true],
    ["+([ab])", "abba", true],
    ["+(*(a)|*(b))", "abba", true],
    ["+(*(a)|*(b))", "abc", false],
  ]) {
    assert.equal(pm(glob)(input), expected, `${glob}: ${input}`);
  }
  assert.equal(typeof pm.scan("apps/**/*.tsx").base, "string");
  assert.ok(pm.makeRe("**/*.tsx") instanceof RegExp);
});

test("single-branch repeated extglobs terminate on a long failing suffix", () => {
  const patterns = [
    "+(*)z",
    "*(*)z",
    "+(*a)z",
    "*(*a)z",
    "+(a*)z",
    "*(a*)z",
    "+(@(a|aa))z",
  ];
  const child = spawnSync(
    process.execPath,
    [
      "--eval",
      `const pm=require(${JSON.stringify(patched)});for(const p of ${JSON.stringify(patterns)}){if(pm(p)('a'.repeat(2048)+'!'))process.exit(2)}process.stdout.write('completed')`,
    ],
    { encoding: "utf8", timeout: 3000 },
  );
  assert.equal(
    child.error,
    undefined,
    "glob matching exceeded its child-process deadline",
  );
  assert.equal(child.status, 0, child.stderr);
  assert.equal(child.stdout, "completed");
});
