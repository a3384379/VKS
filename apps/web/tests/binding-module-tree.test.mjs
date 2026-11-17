import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import { transform } from "esbuild";

const source = await fs.readFile(
  new URL("../../studio/src/bindingModuleTree.ts", import.meta.url),
  "utf8",
);
const compiled = await transform(source, { loader: "ts", format: "esm" });
const { bindingModuleTree, bindingPageKey } = await import(
  "data:text/javascript;base64," + Buffer.from(compiled.code).toString("base64")
);
const row = (
  id,
  route,
  label,
  selector = "body",
  pack = "pack",
  frames = ["actualPage"],
) => ({
  id,
  package_id: pack,
  target: { route, label, selector, frame_path: frames },
});

test("module selection covers descendant feature pages and embedded controls without flattening them", () => {
  const rows = [
    row("add", "/actual/物业缴费/items.html", "新增", "#add"),
    row(
      "module",
      "/actual/物业缴费/items.html",
      "收费项目管理 · 真实模板离线原型",
    ),
    row("embedded", "/actual/物业缴费/items.html", "确定", "#ok", "pack", [
      "actualPage",
      "dialog",
    ]),
    row("reduce", "/actual/物业缴费/reduce.html", "减免类型"),
    row("report", "/actual/物业报表/stat.html", "统计"),
  ];
  const tree = bindingModuleTree(rows);
  const fees = tree.roots.find((n) => n.label === "物业缴费");
  assert.equal(tree.roots.length, 2);
  assert.deepEqual(fees.children.map((n) => n.label).sort(), [
    "减免类型",
    "收费项目管理",
  ]);
  assert.deepEqual([...fees.bindingIds].sort(), [
    "add",
    "embedded",
    "module",
    "reduce",
  ]);
  const page = tree.byPage.get(bindingPageKey(rows[0]));
  assert.deepEqual([...page.bindingIds].sort(), ["add", "embedded", "module"]);
  assert.deepEqual(page.path, ["物业缴费", "收费项目管理"]);
});

test("source titles name pages with no saved module binding, while unreadable/unbound metadata creates no nodes", () => {
  const rows = [
    row("control", "/actual/物业缴费/item.html", "删除", "#delete"),
  ];
  const tree = bindingModuleTree(
    rows,
    [],
    [
      {
        package_id: "pack",
        route: rows[0].target.route,
        label: "收费项目管理",
      },
      {
        package_id: "pack",
        route: "/actual/隐藏模块/private.html",
        label: "Hidden name",
      },
    ],
  );
  assert.equal(tree.roots.length, 1);
  assert.equal(tree.roots[0].children[0].label, "收费项目管理");
  assert.equal(tree.nodes.size, 2);
});

test("equal module and page labels from different source packages keep independent selections", () => {
  const rows = [
    row("a", "/actual/物业缴费/list.html", "列表", "body", "a"),
    row("b", "/actual/物业缴费/list.html", "列表", "body", "b"),
  ];
  const tree = bindingModuleTree(rows, [
    { id: "a", name: "管理前端" },
    { id: "b", name: "员工前端" },
  ]);
  assert.equal(tree.roots.length, 2);
  assert.notEqual(tree.roots[0].label, tree.roots[1].label);
  for (const root of tree.roots) {
    assert.equal(root.bindingIds.size, 1);
    assert.equal(root.children[0].path[0], root.label);
  }
});

test("nested directories decode safely and retain every business level", () => {
  const tree = bindingModuleTree([
    row(
      "a",
      "/actual/%E7%89%A9%E4%B8%9A%E7%BC%B4%E8%B4%B9/收费配置/items.html",
      "收费项",
    ),
  ]);
  assert.deepEqual(tree.roots[0].children[0].children[0].path, [
    "物业缴费",
    "收费配置",
    "收费项",
  ]);
  assert.doesNotThrow(() =>
    bindingModuleTree([row("b", "/actual/%bad/x.html", "X")]),
  );
});

test("standalone and legacy pages remain selectable without inventing a business category", () => {
  const tree = bindingModuleTree(
    [row("a", "/", "首页"), row("b", "/list.html", "旧列表")],
    [{ id: "pack", name: "收费前端" }],
  );
  assert.equal(tree.roots[0].label, "收费前端");
  assert.equal(tree.roots[0].bindingIds.size, 2);
  const mixed = bindingModuleTree([
    row("a", "/", "首页"),
    row("b", "/actual/物业缴费/list.html", "列表"),
  ]);
  assert.ok(mixed.roots.some((n) => n.label === "其他页面"));
});

test("page selection identity survives metadata loading and the shallow page wins over iframe titles", () => {
  const rows = [
    row("deep", "/actual/物业缴费/items.html", "弹窗", "body", "pack", [
      "actualPage",
      "dialog",
    ]),
    row("page", "/actual/物业缴费/items.html", "收费项目", "body"),
  ];
  const first = bindingModuleTree(rows);
  const later = bindingModuleTree(
    rows,
    [],
    [
      {
        package_id: "pack",
        route: rows[0].target.route,
        label: "完整功能名称",
        frame_path: ["actualPage"],
      },
    ],
  );
  assert.equal(first.roots[0].children[0].label, "收费项目");
  assert.equal(first.roots[0].children[0].id, later.roots[0].children[0].id);
  assert.equal(later.roots[0].children[0].label, "完整功能名称");
});
