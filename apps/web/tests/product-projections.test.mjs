import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
let server, cacheDir, projection, scopeWorkspace;
before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "vks-product-test-"));
  server = await createServer({
    configFile: false,
    cacheDir,
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false },
  });
  projection = await server.ssrLoadModule("/src/product-projections.ts");
  ({ moduleWorkspace: scopeWorkspace } = await server.ssrLoadModule(
    "/src/module-context.ts",
  ));
});
after(async () => {
  await server?.close();
  await rm(cacheDir, { recursive: true, force: true });
});
const entity = (type, id, metadata = {}) => ({
  id,
  entity_id: id,
  entity_revision_id: `${id}-rev`,
  type,
  metadata,
});
const relation = (from, to, overrides = {}) => ({
  from_revision_id: `${from}-rev`,
  to_revision_id: `${to}-rev`,
  relation_type: "explains",
  excluded: false,
  validity_status: "valid",
  ...overrides,
});
test("business counts require explicit semantics even when an approved title looks like a process", () => {
  assert.equal(
    projection.materialKind(entity("journey", "业务流程", { excluded: false })),
    "unclassified",
  );
  assert.equal(
    projection.materialKind(
      entity("journey", "j", {
        journey_dsl: { semantics: { kind: "evidence_navigation" } },
      }),
    ),
    "evidence_navigation",
  );
  assert.equal(
    projection.materialKind(
      entity("journey", "j", {
        journey_dsl: { semantics: { kind: "business_process" } },
      }),
    ),
    "business_process",
  );
});
test("knowledge coverage excludes structure and invalid evidence associations in both directions", () => {
  const entities = ["a", "b", "c", "d", "e"]
    .map((id) => entity("action", id))
    .concat([
      entity("page", "page"),
      entity("api", "api"),
      entity("section", "section"),
    ]);
  const edges = [
    relation("page", "a", { relation_type: "contains" }),
    relation("b", "api", { relation_type: "contains" }),
    relation("c", "api", { excluded: true }),
    relation("d", "api", { validity_status: "stale" }),
    relation("section", "e"),
  ];
  assert.deepEqual(
    [...projection.coveredActionRevisions(entities, edges)],
    ["e-rev"],
  );
});
test("task result refs select an exact revision and kind rather than another document with the same name", () => {
  const entities = [
    entity("document", "old"),
    entity("document", "new"),
    entity("journey", "journey"),
  ];
  for (const item of entities) item.name = "Repeated title";
  assert.equal(
    projection.exactResultEntity(entities, "document_revision", "new-rev").id,
    "new",
  );
  assert.equal(
    projection.exactResultEntity(entities, "document_revision", "journey-rev"),
    undefined,
  );
  assert.equal(
    projection.exactResultEntity(entities, "document_revision", "hidden-rev"),
    undefined,
  );
});

test("module context keeps parent page but never selects a sibling action or unrelated state", () => {
  const first = {
    ...entity("action", "first"),
    metadata: { page_key: "page" },
  };
  const second = {
    ...entity("action", "second"),
    metadata: { page_key: "page" },
  };
  const page = { ...entity("page", "page"), stable_key: "page" };
  const scene = entity("scenario", "scene");
  const result = scopeWorkspace(
    { actions: [first, second], pages: [page], scenarios: [scene] },
    [second],
  );
  assert.deepEqual(result.actions, [second]);
  assert.deepEqual(result.pages, [page]);
  assert.deepEqual(result.scenarios, []);
});
test("an unresolved or document-only module has no global action fallback", () => {
  const w = {
    actions: [entity("action", "outside")],
    pages: [entity("page", "outside-page")],
    scenarios: [entity("scenario", "outside-state")],
  };
  for (const members of [null, [], [entity("document", "notes")]]) {
    const result = scopeWorkspace(w, members);
    assert.deepEqual(
      [result.actions, result.pages, result.scenarios],
      [[], [], []],
    );
  }
});
