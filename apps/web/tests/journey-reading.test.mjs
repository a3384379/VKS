import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";

let server, cacheDir, reading, prepare, merge;
before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "vks-journey-reading-"));
  server = await createServer({
    configFile: false,
    cacheDir,
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false },
  });
  reading = await server.ssrLoadModule("/src/journey-reading.ts");
  ({ prepareJourneyDraft: prepare } =
    await server.ssrLoadModule("/src/journeys.ts"));
  ({ mergeJourney: merge } = await server.ssrLoadModule(
    "/src/journey-editing.ts",
  ));
});
after(async () => {
  await server?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

const node = (key, kind = "action") => ({ key, kind, title: key, x: 0, y: 0 });
const dsl = () => ({
  schema_version: "2.0",
  key: "test",
  title: "业务流程候选",
  entry: "start",
  nodes: [
    node("start"),
    node("decision", "decision"),
    node("success", "end"),
    node("failure", "end"),
  ],
  edges: [
    { key: "a", source: "start", target: "decision", label: "" },
    { key: "b", source: "decision", target: "success", label: "条件满足" },
    { key: "c", source: "decision", target: "failure", label: "条件不满足" },
  ],
});

test("a legacy title and graph shape never promote material to a business process", () => {
  const legacy = dsl(),
    before = JSON.stringify(legacy);
  assert.deepEqual(reading.journeySemantics(legacy), {
    kind: "unclassified",
    basis: "unknown",
  });
  assert.equal(JSON.stringify(legacy), before);
  for (const kind of [
    "evidence_navigation",
    "observation_comparison",
    "operation_sequence",
  ]) {
    assert.equal(
      reading.journeySemantics({
        ...legacy,
        semantics: { kind, basis: "observed" },
      }).kind,
      kind,
    );
  }
});

test("reading follows stored edges and pauses at each branch until an exact choice", () => {
  const graph = dsl(),
    original = JSON.stringify(graph);
  let result = reading.journeyPath(graph, {});
  assert.deepEqual(
    result.path.map((n) => n.key),
    ["start", "decision"],
  );
  assert.deepEqual(
    result.branches.map((e) => e.key),
    ["b", "c"],
  );
  assert.deepEqual(
    reading.journeyPath(graph, { decision: "c" }).path.map((n) => n.key),
    ["start", "decision", "failure"],
  );
  assert.equal(
    reading.journeyPath(graph, { decision: "not-an-edge" }).branches.length,
    2,
  );
  assert.equal(JSON.stringify(graph), original);
});

test("passive navigation requires exact visible identity and revision for entities and replay", () => {
  const action = {
    id: "action",
    entity_id: "action",
    entity_revision_id: "new",
    type: "action",
    name: "保存",
  };
  const bound = {
    ...node("save"),
    binding: { entity_id: "action", revision_id: "old" },
  };
  assert.equal(reading.journeyTarget(bound, [action]).kind, "unavailable");
  assert.equal(reading.journeySource(bound, [action]).available, false);
  assert.equal(
    reading.journeyTarget(
      { ...bound, binding: { entity_id: "action", revision_id: "new" } },
      [action],
    ).kind,
    "entity",
  );
  const scene = {
    id: "scene",
    entity_id: "scene",
    entity_revision_id: "r1",
    type: "scenario",
    name: "保存的场景",
  };
  const state = {
    ...node("state", "reference"),
    state_ref: {
      scenario_id: "scene",
      scenario_revision_id: "r1",
      state_key: "ok",
    },
  };
  assert.equal(reading.journeyTarget(state, [scene]).kind, "state");
  assert.equal(
    reading.journeyTarget(state, [{ ...scene, entity_revision_id: "r2" }]).kind,
    "unavailable",
  );
  const capture = {
    ...state,
    capture_ref: {
      scenario_id: "scene",
      scenario_revision_id: "r1",
      capture_id: "capture",
      snapshot_index: 3,
    },
  };
  assert.equal(
    reading.journeyTarget(capture, [scene]).reference.snapshot_index,
    3,
  );
  assert.equal(reading.journeyTarget(capture, []).kind, "unavailable");
});

test("ordinary business forms trim optional empty rows and require a goal and outcome without changing legacy DSL", () => {
  const original = dsl();
  assert.deepEqual(prepare(original), original);
  assert.equal(Object.hasOwn(prepare(original), "semantics"), false);
  const form = {
    ...original,
    semantics: {
      kind: "business_process",
      basis: "declared",
      purpose: " 申请新增用户 ",
      outcome: " 保存或说明失败 ",
      preconditions: ["已登录", "", "  "],
      unknowns: ["", "未验收真实权限分支"],
    },
  };
  const prepared = prepare(form);
  assert.deepEqual(prepared.semantics.preconditions, ["已登录"]);
  assert.equal(prepared.semantics.purpose, "申请新增用户");
  assert.deepEqual(prepared.semantics.unknowns, ["未验收真实权限分支"]);
  assert.throws(
    () => prepare({ ...form, semantics: { ...form.semantics, outcome: " " } }),
    /业务目标和预期结果/,
  );
  assert.throws(
    () =>
      prepare({
        ...form,
        semantics: { ...form.semantics, preconditions: Array(31).fill("条件") },
      }),
    /30 项/,
  );
  assert.equal(form.semantics.purpose, " 申请新增用户 ");
  const declared = {
    ...form,
    nodes: [{ ...node("manual", "step"), description: "手册中的明确步骤" }],
  };
  assert.throws(() => prepare(declared), /场景依据资料/);
  const supported = {
    ...declared,
    semantics: {
      ...declared.semantics,
      source_refs: [{ entity_id: "doc", revision_id: "fixed-doc" }],
    },
  };
  assert.deepEqual(
    prepare(supported).semantics.source_refs,
    supported.semantics.source_refs,
  );
});

test("three-way merge preserves meaning metadata and reports competing claims atomically", () => {
  const base = dsl(),
    local = structuredClone(base),
    remote = structuredClone(base);
  local.semantics = {
    kind: "business_process",
    basis: "declared",
    purpose: "申请",
    outcome: "得到结果",
  };
  remote.nodes[0].description = "准确步骤说明";
  let result = merge(base, local, remote);
  assert.deepEqual(result.merged.semantics, local.semantics);
  assert.equal(result.merged.nodes[0].description, "准确步骤说明");
  remote.semantics = { kind: "evidence_navigation", basis: "unknown" };
  result = merge(base, local, remote, { semantics: "remote" });
  assert.deepEqual(result.merged.semantics, remote.semantics);
  assert.ok(result.conflicts.some((conflict) => conflict.path === "semantics"));
});
