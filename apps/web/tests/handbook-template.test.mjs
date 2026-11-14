import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";

test("offline skin is an exact generated copy of the shared framework", () => {
  execFileSync(
    process.execPath,
    ["../../scripts/reader_handbook.mjs", "--check"],
    {
      stdio: "pipe",
    },
  );
});

let cache, server, framework, HandbookTemplate;
before(async () => {
  cache = await mkdtemp(join(tmpdir(), "vks-handbook-test-"));
  server = await createServer({
    configFile: false,
    cacheDir: cache,
    plugins: [react()],
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false },
  });
  framework = await server.ssrLoadModule(
    "/../../packages/handbook-template/src/index.ts",
  );
  ({ HandbookTemplate } = await server.ssrLoadModule(
    "/../studio/src/HandbookTemplate.tsx",
  ));
});
after(async () => {
  await server?.close();
  if (cache) await rm(cache, { recursive: true, force: true });
});

const docs = [
  {
    id: "entity-a",
    revision_id: "a",
    name: "Overview",
    path: "guide/overview.md",
    package_id: "one",
  },
  {
    id: "entity-b",
    revision_id: "b",
    name: "Rules",
    path: "business/rules.md",
    package_id: "one",
  },
  {
    id: "entity-c",
    revision_id: "c",
    name: "Other",
    path: "other.md",
    package_id: "two",
  },
];
const tree = {
  packages: [
    {
      id: "one",
      name: "One",
      categories: [
        {
          id: "intro",
          name: "Introduction",
          path: "guide",
          children: [
            {
              kind: "folder",
              id: "folder",
              name: "Guide",
              path: "guide",
              children: [
                {
                  kind: "document",
                  id: "a",
                  name: "OLD NAME",
                  path: "guide/overview.md",
                },
              ],
            },
          ],
        },
        {
          id: "business",
          name: "Business",
          path: "business",
          children: [
            {
              kind: "document",
              id: "b",
              name: "Rules",
              path: "business/rules.md",
            },
          ],
        },
        {
          id: "secret",
          name: "Hidden category",
          path: "hidden",
          children: [
            {
              kind: "document",
              id: "hidden",
              name: "Hidden name",
              path: "secret.md",
            },
          ],
        },
      ],
    },
    {
      id: "two",
      name: "Two",
      categories: [
        {
          id: "other",
          name: "Other",
          path: "",
          children: [
            { kind: "document", id: "c", name: "Other", path: "other.md" },
          ],
        },
      ],
    },
  ],
};

test("template is empty and all project content is host supplied", async () => {
  assert.deepEqual(framework.handbookTemplateManifest.content, {
    documents: [],
    categories: [],
    vectors: [],
  });
  const files = ["index.ts", "skin.css", "framework.css"];
  for (const file of files) {
    const source = await readFile(
      new URL(
        "../../../packages/handbook-template/src/" + file,
        import.meta.url,
      ),
      "utf8",
    );
    assert.doesNotMatch(
      source,
      /云收费|fh-pack-data|CLOUD CHARGE|sk-proj-|fh-rag-vectors/,
    );
  }
  const html = renderToStaticMarkup(
    createElement(HandbookTemplate, {
      projectName: "Synthetic Project",
      documents: [],
      selectedDocument: "",
      selectedSection: "",
      currentDocument: null,
      scopeKey: "empty",
      loadDocument: async () => assert.fail("No document should load"),
      onOpen() {},
      onAskAI() {},
      onUpdate() {},
      renderReader: null,
    }),
  );
  assert.match(html, /Synthetic Project/);
  assert.match(html, /从第一篇文档开始/);
  assert.match(html, /导入文档/);
  assert.doesNotMatch(html, /云收费/);
});

test("directory prunes invisible names and keeps exact package order for next article", () => {
  const before = JSON.stringify(tree);
  const visible = framework.handbookNavigation(tree, docs);
  assert.doesNotMatch(
    JSON.stringify(visible),
    /Hidden category|Hidden name|secret.md|OLD NAME/,
  );
  assert.equal(JSON.stringify(tree), before);
  const entries = framework.handbookEntries(visible);
  assert.deepEqual(
    entries.map((entry) => entry.document.id),
    ["a", "b", "c"],
  );
  assert.deepEqual(
    entries[0].folders.map((folder) => folder.id),
    ["folder"],
  );
  assert.equal(framework.handbookNeighbors(entries, "a").next.document.id, "b");
  assert.equal(
    framework.handbookNeighbors(entries, "b").previous.document.id,
    "a",
  );
  assert.equal(framework.handbookNeighbors(entries, "b").next, undefined);
  assert.equal(framework.handbookNeighbors(entries, "c").previous, undefined);
});

test("fallback directories keep source packages separate and configuration contains plain data", () => {
  const visible = framework.handbookNavigation(undefined, docs);
  assert.equal(visible.packages.length, 2);
  assert.equal(
    framework.handbookNeighbors(framework.handbookEntries(visible), "b").next,
    undefined,
  );
  assert.equal(
    framework.handbookConfig("Project", {
      accent: "red; background:url(https://invalid.test)",
    }).accent,
    "#087e76",
  );
  assert.equal(
    framework.handbookConfig("Project", {
      title: "  Custom title  ",
      accent: "#123456",
    }).title,
    "Custom title",
  );
  assert.equal(
    framework.handbookConfig("Project", { accent: "#123456" }).accent,
    "#123456",
  );
});

test("outline follows exact section revisions and nested levels without copying body", () => {
  const article = {
    revision_id: "a",
    name: "Overview",
    sections: [
      { id: "s0", revision_id: "s0", name: "Overview", content: "# Overview" },
      { id: "s1", revision_id: "s1", name: "Intro", content: "## Intro" },
      { id: "s2", revision_id: "s2", name: "Detail", content: "### Detail" },
      { id: "s3", revision_id: "s3", name: "End", content: "## End" },
    ],
  };
  const outline = framework.handbookOutline(article);
  assert.deepEqual(
    outline.map((row) => row.section.revision_id),
    ["s1", "s3"],
  );
  assert.equal(outline[0].children[0].section.revision_id, "s2");
  assert.equal(
    framework.handbookSearchSnippet(
      "Keep the exact BUSINESS rule.",
      "business",
    ),
    "Keep the exact BUSINESS rule.",
  );
});

test("readonly host retains reading, exact links and AI but removes document update controls", () => {
  const html = renderToStaticMarkup(
    createElement(HandbookTemplate, {
      projectName: "Synthetic Project",
      documents: docs,
      navigation: tree,
      selectedDocument: "a",
      selectedSection: "",
      currentDocument: null,
      scopeKey: "readonly",
      readOnly: true,
      loadDocument: async () => assert.fail("SSR must not fetch"),
      onOpen() {},
      onAskAI() {},
      onUpdate() {},
      renderReader: createElement(
        "article",
        { className: "document-reader" },
        createElement("strong", {}, "Exact source"),
      ),
    }),
  );
  assert.match(html, /向手册 AI 提问/);
  assert.match(html, /打印 \/ PDF/);
  assert.match(html, /下一篇/);
  assert.match(html, /<strong>Exact source<\/strong>/);
  assert.doesNotMatch(html, /更新文档|导入文档|Hidden category|Hidden name/);
});
