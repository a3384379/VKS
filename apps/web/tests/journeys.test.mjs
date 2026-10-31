import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
let server,
  cacheDir,
  update,
  travel,
  newJourney,
  primary,
  group,
  route,
  merge,
  arrange,
  layered,
  HashRouteSync;
before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "vks-journey-"));
  server = await createServer({
    configFile: false,
    cacheDir,
    server: { middlewareMode: true, hmr: false, ws: false },
  });
  ({
    updateJourney: update,
    travelJourney: travel,
    newJourney,
  } = await server.ssrLoadModule("/src/journeys.ts"));
  ({
    primaryNavigation: primary,
    navigationGroup: group,
    routeParameters: route,
  } = await server.ssrLoadModule("/src/workspace-navigation.ts"));
  ({ mergeJourney: merge, arrangeJourney: arrange } =
    await server.ssrLoadModule("/src/journey-editing.ts"));
  ({ layeredMap: layered } = await server.ssrLoadModule(
    "/src/graph-layout.ts",
  ));
  ({ HashRouteSync } = await server.ssrLoadModule("/src/route-sync.ts"));
});

test("native history wins over stale passive effects and duplicate browser events", () => {
  const route = new HashRouteSync("#state=a");
  const user = route.user();
  assert.equal(route.commit(0, "#state=a", "#state=a"), null);
  assert.equal(route.commit(user, "#state=a", "#state=b"), "push");
  // Back changes location before its event or React's previous passive effect.
  assert.equal(route.commit(user, "#state=a", "#state=b"), null);
  const back = route.native("#state=a");
  assert.equal(route.native("#state=a"), null);
  assert.equal(route.commit(user, "#state=a", "#state=b"), null);
  assert.equal(route.commit(back, "#state=a", "#state=a"), "none");
  // Forward arrives after the Back DOM commits but before its passive effect.
  assert.equal(route.commit(back, "#state=b", "#state=a"), null);
  const forward = route.native("#state=b");
  assert.equal(route.commit(forward, "#state=b", "#state=b"), "none");
  assert.equal(
    route.commit(forward, "#state=b", "#state=b&ui_page=exact"),
    "replace",
  );
});

test("document deep links survive stale overview writes and normalize without extra history", () => {
  const route = new HashRouteSync("#view=overview");
  const target = "#view=documents&entity=doc";
  assert.equal(route.commit(0, target, "#view=overview"), null);
  const epoch = route.native(target);
  assert.equal(route.commit(0, target, "#view=overview"), null);
  assert.equal(route.commit(epoch, target, target), "none");
  assert.equal(
    route.commit(epoch, target, target + "&baseline=exact"),
    "replace",
  );
  route.restore(target);
  assert.equal(route.native(target), null);
  assert.equal(route.commit(epoch, target, target), "none");
});

test("completed Back and Forward commits reject every older render effect", () => {
  const route = new HashRouteSync("#state=ready");
  const user = route.user();
  assert.equal(route.commit(user, "#state=ready", "#state=b"), "push");
  const back = route.native("#state=ready");
  assert.equal(route.commit(back, "#state=ready", "#state=ready"), "none");
  // The native pending marker is now clear. An old passive effect must still
  // be rejected, otherwise it replaces the restored entry with stale state.
  assert.equal(route.commit(user, "#state=ready", "#state=b"), null);
  const forward = route.native("#state=b");
  assert.equal(route.commit(forward, "#state=b", "#state=b"), "none");
  assert.equal(route.commit(back, "#state=b", "#state=ready"), null);
});
after(async () => {
  await server?.close();
  await rm(cacheDir, { recursive: true, force: true });
});
test("five task navigation groups preserve original hashes and concept aliases", () => {
  assert.equal(primary.length, 5);
  assert.equal(group("documents"), "imports");
  assert.equal(group("journeys"), "workbench");
  assert.equal(group("relations"), "versions");
  assert.equal(route("explorer").get("view"), "workbench");
  assert.equal(
    route("view=documents&project=p&baseline=release:r&entity=e").toString(),
    "view=documents&project=p&baseline=release%3Ar&entity=e",
  );
  for (const old of [
    "overview",
    "imports",
    "workbench",
    "documents",
    "reviews",
    "graph",
    "versions",
    "settings",
  ])
    assert.equal(
      route(`view=${old}`).get("view"),
      old === "reviews" ? "relations" : old,
    );
});
test("journey undo redo preserves fixed bindings and forks history without mutation", () => {
  const dsl = newJourney({
    entity_id: "a",
    entity_revision_id: "fixed",
    name: "Action",
  });
  const original = JSON.stringify(dsl);
  let history = { past: [], present: dsl, future: [] };
  history = update(history, { ...dsl, title: "Edited" });
  assert.equal(JSON.stringify(dsl), original);
  assert.equal(history.present.nodes[0].binding.revision_id, "fixed");
  history = travel(history, "undo");
  assert.equal(history.present.title, dsl.title);
  history = travel(history, "redo");
  assert.equal(history.present.title, "Edited");
  history = travel(history, "undo");
  history = update(history, { ...dsl, title: "Other" });
  assert.equal(history.future.length, 0);
  assert.equal(
    JSON.stringify(JSON.parse(JSON.stringify(history.present))),
    JSON.stringify(history.present),
  );
});

test("three-way editing preserves independent edits and resolves exact binding conflicts atomically", () => {
  const base = newJourney({
      entity_id: "a",
      entity_revision_id: "r1",
      name: "A",
    }),
    original = JSON.stringify(base);
  const local = structuredClone(base),
    remote = structuredClone(base);
  local.nodes[0].x = 200;
  remote.title = "Remote title";
  let result = merge(base, local, remote);
  assert.equal(result.conflicts.length, 0);
  assert.equal(result.merged.title, "Remote title");
  assert.equal(result.merged.nodes[0].x, 200);
  local.nodes[0].binding = { entity_id: "local", revision_id: "local-r" };
  remote.nodes[0].binding = { entity_id: "remote", revision_id: "remote-r" };
  result = merge(base, local, remote, { "nodes.step.binding": "remote" });
  assert.deepEqual(result.merged.nodes[0].binding, {
    entity_id: "remote",
    revision_id: "remote-r",
  });
  assert.equal(result.conflicts[0].path, "nodes.step.binding");
  assert.equal(JSON.stringify(base), original);
  const deleted = structuredClone(base);
  deleted.nodes = deleted.nodes.slice(1);
  deleted.edges = [];
  result = merge(base, deleted, remote, { "nodes.step": "local" });
  assert.ok(result.conflicts.some((c) => c.path === "nodes.step"));
  assert.equal(result.merged.nodes.length, 1);
});

test("automatic arrangement keeps 120 nodes within DSL bounds and preserves exact references and undo", () => {
  const base = newJourney();
  base.nodes = Array.from({ length: 120 }, (_, i) => ({
    key: "n" + i,
    kind: i === 119 ? "end" : "action",
    title: "Node " + i,
    x: 0,
    y: 0,
    ...(i === 119
      ? {}
      : { binding: { entity_id: "e" + i, revision_id: "r" + i } }),
  }));
  base.entry = "n0";
  base.edges = Array.from({ length: 119 }, (_, i) => ({
    key: "e" + i,
    source: "n" + i,
    target: "n" + (i + 1),
    label: "",
  }));
  const original = JSON.stringify(base),
    actual = arrange(base);
  assert.equal(new Set(actual.nodes.map((n) => `${n.x}:${n.y}`)).size, 120);
  assert.ok(actual.nodes.every((n) => n.x <= 4000 && n.y <= 4000));
  assert.deepEqual(actual.edges, base.edges);
  assert.deepEqual(
    actual.nodes.map((n) => n.binding),
    base.nodes.map((n) => n.binding),
  );
  assert.equal(JSON.stringify(base), original);
  let history = update({ past: [], present: base, future: [] }, actual);
  assert.deepEqual(travel(history, "undo").present, base);
  const cycle = structuredClone(base);
  cycle.edges.push({ key: "cycle", source: "n119", target: "n0", label: "" });
  assert.throws(() => arrange(cycle), /循环/);
});

test("layered knowledge map places all bounded nodes including more than the old 13-node radial limit", () => {
  const keys = Array.from({ length: 100 }, (_, i) => "r" + i),
    edges = keys
      .slice(1)
      .map((key) => ({ from_revision_id: "r0", to_revision_id: key }));
  const layout = layered(keys, edges, "r0");
  assert.equal(layout.points.size, 100);
  assert.equal(
    new Set([...layout.points.values()].map((p) => `${p.x}:${p.y}`)).size,
    100,
  );
  assert.ok(
    [...layout.points.values()].every(
      (p) => p.x > 0 && p.y > 0 && p.x < layout.width && p.y < layout.height,
    ),
  );
  assert.ok(layout.points.get("r1").x > layout.points.get("r0").x);
});
