import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { createServer } from "vite";
import react from "@vitejs/plugin-react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
let server, cacheDir, route, HashRouteSync, documentContext, JourneyPlanPreview, JourneyStepContext, WorkspaceContext, journeyListRouteMatches;
before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "vks-journey-navigation-"));
  server = await createServer({ configFile: false, cacheDir, optimizeDeps: { noDiscovery: true, include: [] }, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false } });
  route = await server.ssrLoadModule("/src/journey-navigation.ts");
  ({ journeyListRouteMatches } = await server.ssrLoadModule("/src/journey-list-route.ts"));
  ({ HashRouteSync } = await server.ssrLoadModule("/src/route-sync.ts"));
  ({ documentContext } = await server.ssrLoadModule("/src/document-context.ts"));
  ({ JourneyPlanPreview } = await server.ssrLoadModule("/src/JourneyPlanPreview.tsx"));
  ({ JourneyStepContext } = await server.ssrLoadModule("/src/JourneyStepContext.tsx"));
  ({ WorkspaceContext } = await server.ssrLoadModule("/src/context.ts"));
});
after(async () => { await server?.close(); await rm(cacheDir, { recursive: true, force: true }); });
const dsl = { entry: "start", semantics: { kind: "business_process" }, nodes: ["start", "check", "yes", "no"].map(key => ({ key })), edges: [{ key: "next", source: "start", target: "check" }, { key: "accept", source: "check", target: "yes" }, { key: "decline", source: "check", target: "no" }] };
const navigation = { journeyId: "flow-b", revisionId: "fixed", stepKey: "yes", choices: { check: "accept" } };
function hash(value) { const query = new URLSearchParams({ view: "journeys", project: "project", baseline: "release:fixed", entity: value.journeyId }); route.writeJourneyNavigation(query, value); return "#" + query; }

test("shareable reading state round trips exact journey, revision, selected step and branches", () => {
  assert.deepEqual(route.readJourneyNavigation(new URLSearchParams(hash(navigation).slice(1))), navigation);
  assert.equal(route.readJourneyNavigation(new URLSearchParams("view=journeys&entity=legacy-flow")).journeyId, "legacy-flow");
  for (const value of ['["not-object"]', "null", "{invalid", JSON.stringify({ ["x".repeat(97)]: "yes", check: ["wrong"] })])
    assert.deepEqual(route.readJourneyNavigation(new URLSearchParams({ journey_choices: value })).choices, {});
  assert.deepEqual(route.readJourneyNavigation(new URLSearchParams({ journey_choices: "x".repeat(25001) })).choices, {});
});

test("reading restore rejects stale revisions, nonexistent steps, invalid and unreachable choices", () => {
  assert.deepEqual(route.resolveJourneyReading(dsl, "fixed", navigation), { revisionId: "fixed", stepKey: "yes", choices: { check: "accept" } });
  assert.deepEqual(route.resolveJourneyReading(dsl, "new", navigation), { revisionId: "new", stepKey: "start", choices: {} });
  assert.deepEqual(route.resolveJourneyReading(dsl, "fixed", { ...navigation, choices: { start: "decline", check: "missing", no: "accept" } }), { revisionId: "fixed", stepKey: "start", choices: {} });
  assert.equal(route.resolveJourneyReading(dsl, "fixed", { ...navigation, stepKey: "no" }).stepKey, "start");
});

test("guide/source/back/forward history keeps native epochs authoritative over stale step writes", () => {
  const initial = hash({ ...navigation, stepKey: "check", choices: {} }), selected = hash(navigation);
  const sync = new HashRouteSync(initial), selectEpoch = sync.user();
  assert.equal(sync.commit(selectEpoch, initial, selected), "push");
  const source = selected.replace("view=journeys", "view=documents").replace("entity=flow-b", "entity=doc");
  const sourceEpoch = sync.user();
  assert.equal(sync.commit(sourceEpoch, selected, source), "push");
  const backEpoch = sync.native(selected);
  assert.equal(sync.commit(sourceEpoch, selected, source), null);
  assert.equal(sync.commit(backEpoch, selected, selected), "none");
  assert.equal(sync.commit(selectEpoch, selected, initial), null);
  const forwardEpoch = sync.native(source);
  assert.equal(sync.commit(backEpoch, source, selected), null);
  assert.equal(sync.commit(forwardEpoch, source, source), "none");
  assert.deepEqual(route.readJourneyNavigation(new URLSearchParams(source.slice(1))), navigation);
});

test("list normalization checks the latest exact URL and tolerates only semantically equal parameter ordering", () => {
  const current = { projectId: "project", baseline: "release:fixed", selectedEntity: "flow-b", journey: navigation };
  assert.equal(journeyListRouteMatches(hash(navigation), current), true);
  for (const [key, value] of [["view", "documents"], ["project", "other"], ["baseline", "release:other"], ["entity", "flow-c"], ["journey", "flow-c"], ["journey_revision", "old"], ["journey_step", "check"], ["journey_choices", "{}"]]) {
    const query = new URLSearchParams(hash(navigation).slice(1)); query.set(key, value);
    assert.equal(journeyListRouteMatches("#" + query, current), false, key);
  }
  const two = { ...navigation, choices: { later: "second", check: "accept" } };
  assert.equal(journeyListRouteMatches(hash(two), { ...current, journey: two }), true);
});

const doc = { id: "doc", entity_id: "doc", entity_revision_id: "doc-fixed", type: "document", name: "准确手册", metadata: { section_revision_ids: ["section-fixed"] } };
const section = { id: "section", entity_id: "section", entity_revision_id: "section-fixed", type: "section", name: "章节", content: "[下载](images/map.png)", metadata: { document_key: "untrusted-key" }, artifact_id: "same-artifact" };
test("attachment ownership uses only exact section revision mappings and fails closed on ambiguity", () => {
  assert.deepEqual(documentContext(section, [doc]), { documentId: "doc", revisionId: "doc-fixed" });
  assert.deepEqual(documentContext(doc, []), { documentId: "doc", revisionId: "doc-fixed" });
  assert.equal(documentContext({ ...section, entity_revision_id: "old" }, [{ ...doc, artifact_id: "same-artifact" }]).documentId, undefined);
  assert.equal(documentContext(section, [doc, { ...doc, entity_id: "other" }]).documentId, undefined);
});

test("declared step inline section downloads retain the owning document and immutable revision", () => {
  const value = { w: { project: { id: "project" }, entities: [doc, section], documents: [doc], sources: [] }, session: { scopes: ["knowledge:read"] }, baseline: "release:fixed" };
  const html = renderToStaticMarkup(createElement(WorkspaceContext.Provider, { value }, createElement(JourneyStepContext, { node: { key: "declare", kind: "step", title: "说明", description: "来源说明" }, sourceRefs: [{ entity_id: "section", revision_id: "section-fixed" }], onOpen() {} })));
  assert.match(html, /documents\/doc\/revisions\/doc-fixed\/attachments\?path=images%2Fmap.png/);
  assert.doesNotMatch(html, /documents\/section\//);
});

test("plan confirmation explains goal, conditions, outcome, transitions and exact references", () => {
  const journey = { purpose: "检查申请", preconditions: ["先登录"], outcome: "返回明确结果", entry: "check", nodes: [{ key: "check", kind: "decision", title: "核对资格", description: "核对来源声明", target_revision_id: "doc-fixed" }, { key: "end", kind: "end", title: "登记完成", description: null, target_revision_id: null }], edges: [{ source: "check", target: "end", label: "资格满足" }], source_revision_ids: ["doc-fixed", "unavailable-revision"] };
  const html = renderToStaticMarkup(createElement(JourneyPlanPreview, { journey, entities: [doc, { ...doc, name: "其他版本名称", entity_revision_id: "new" }] }));
  for (const text of ["检查申请", "先登录", "返回明确结果", "核对资格", "资格满足", "准确手册", "doc-fixed", "unavailable-revision", "准确输入修订当前不可见"])
    assert.ok(html.includes(text), text);
  assert.ok(!html.includes("其他版本名称"));
});
