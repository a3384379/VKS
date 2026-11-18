import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const result = await build({ entryPoints: [fileURLToPath(new URL("../../studio/src/associationDiff.ts", import.meta.url))], bundle: true, write: false, format: "esm", platform: "node" });
const { associationDiff, comparisonGroups } = await import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64"));

test("formula replacements preserve line numbers, old rules and new table rows", () => {
  const before = "# 模块\n\n用途\n单价 × 月数\n条件不变\n";
  const after = "# 模块\n\n用途\n单价 × 面积 × 月数\n|变量|单位|\n|面积|㎡|\n条件不变\n";
  const diff = associationDiff(before, after);
  assert.equal(diff.removed, 1);
  assert.equal(diff.added, 3);
  assert.deepEqual(diff.lines.filter(r => r.before !== undefined).map(r => r.before), before.trimEnd().split("\n"));
  assert.deepEqual(diff.lines.filter(r => r.after !== undefined).map(r => r.after), after.trimEnd().split("\n"));
  const formula = diff.lines.find(r => r.before === "单价 × 月数");
  assert.equal(formula.after, "单价 × 面积 × 月数");
  assert.equal(formula.oldLine, 4);
  assert.equal(formula.newLine, 4);
});

test("unchanged context folds without losing lines, repeated rules stay correctly aligned", () => {
  const before = Array.from({ length: 1000 }, (_, i) => `规则 ${i}`).join("\n");
  const after = before.replace("规则 500\n", "新增公式\n");
  const diff = associationDiff(before, after);
  const groups = comparisonGroups(diff.lines);
  assert.equal(diff.removed, 1);
  assert.equal(diff.added, 1);
  assert.ok(groups.some(g => g.collapsed));
  assert.equal(groups.reduce((n, g) => n + g.end - g.start, 0), diff.lines.length);
  assert.ok(groups.length < 12);
  assert.deepEqual(associationDiff("重复\n重复\n", "重复\n新规则\n重复\n").lines.filter(r => r.after !== undefined).map(r => r.after), ["重复", "新规则", "重复"]);
});

test("new associations, identical text and a changed final newline remain distinguishable", () => {
  assert.equal(associationDiff("", "新规则").added, 1);
  assert.equal(associationDiff("原规则", "原规则").added, 0);
  assert.equal(associationDiff("原规则", "原规则").removed, 0);
  assert.equal(associationDiff("原规则", "原规则\n").added, 1);
});
