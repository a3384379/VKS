import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import react from "@vitejs/plugin-react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

let testCache;
let server,
  policyPayload,
  publicJwks,
  ModelSettings,
  OAuthSettings,
  WorkspaceContext,
  RelationOperations,
  correctionPayload,
  deterministicRelationTypes,
  flattenModules,
  moduleBaseline;
let auditQuery, projectProfilePayload;
before(async () => {
  testCache = await mkdtemp(join(tmpdir(), "vks-web-test-"));
  server = await createServer({
    configFile: false,
    cacheDir: testCache,
    optimizeDeps: { noDiscovery: true, include: [] },
    plugins: [react()],
    server: { middlewareMode: true, hmr: false, ws: false },
  });
  ({ policyPayload, ModelSettings } = await server.ssrLoadModule(
    "/src/ModelSettings.tsx",
  ));
  ({ publicJwks, OAuthSettings } = await server.ssrLoadModule(
    "/src/OAuthSettings.tsx",
  ));
  ({ WorkspaceContext } = await server.ssrLoadModule("/src/context.ts"));
  ({ RelationOperations, correctionPayload, deterministicRelationTypes } =
    await server.ssrLoadModule("/src/RelationOperations.tsx"));
  ({ flattenModules, moduleBaseline } = await server.ssrLoadModule(
    "/src/ModuleTree.tsx",
  ));
  ({ auditQuery } = await server.ssrLoadModule("/src/AuditSearch.tsx"));
  ({ projectProfilePayload } = await server.ssrLoadModule(
    "/src/ProjectProfile.tsx",
  ));
});
after(async () => {
  await server?.close();
  if (testCache) await rm(testCache, { recursive: true, force: true });
});

test("model policy update strips GET-only metadata and preserves explicit budgets and data consents", () => {
  const policy = {
    enabled: false,
    protocol: "responses",
    endpoint: " https://model.example/v1 ",
    model_id: " model-name ",
    secret_ref: null,
    allow_external: false,
    allow_source: false,
    max_input_tokens: 12000,
    max_output_tokens: 2000,
    daily_token_limit: 100000,
    per_request_cost_limit_micro: 1000000,
    daily_cost_limit_micro: 10000000,
    input_rate_micro_per_million: 0,
    output_rate_micro_per_million: 0,
    timezone: " Asia/Shanghai ",
    row_version: 42,
    configured_secret: true,
    secret: "must-not-send",
  };
  const result = policyPayload(policy);
  assert.equal(result.endpoint, "https://model.example/v1");
  assert.equal(result.model_id, "model-name");
  assert.equal(result.timezone, "Asia/Shanghai");
  assert.equal(result.enabled, false);
  assert.equal(result.allow_external, false);
  assert.equal(result.allow_source, false);
  assert.equal(result.input_rate_micro_per_million, 0);
  assert.equal(result.daily_cost_limit_micro, 10000000);
  assert.equal(Object.hasOwn(result, "row_version"), false);
  assert.equal(Object.hasOwn(result, "configured_secret"), false);
  assert.equal(Object.hasOwn(result, "secret"), false);
});

test("OAuth registration only accepts public RS256 key material with unique identifiers", () => {
  const key = {
    kty: "RSA",
    kid: "public-signing-key",
    n: "example-public-modulus",
    e: "AQAB",
    alg: "RS256",
  };
  assert.deepEqual(publicJwks(JSON.stringify({ keys: [key] })), {
    keys: [key],
  });
  for (const name of [
    "d",
    "p",
    "q",
    "dp",
    "dq",
    "qi",
    "oth",
    "k",
    "jku",
    "x5u",
  ]) {
    assert.throws(
      () =>
        publicJwks(
          JSON.stringify({ keys: [{ ...key, [name]: "not-public" }] }),
        ),
      /公开 RS256/,
    );
  }
  assert.throws(() => publicJwks(JSON.stringify({ keys: [key, key] })), /唯一/);
  assert.throws(
    () => publicJwks(JSON.stringify({ keys: [{ ...key, alg: "HS256" }] })),
    /公开 RS256/,
  );
  assert.throws(() => publicJwks("not json"), /有效 JSON/);
});

const base = {
  project: { id: "project", name: "Project" },
  selected_analysis_set: { id: "draft", state: "draft" },
  relations: [],
  evidence: [],
  entities: [],
};
function render(component, grants = [], owner = false, props = {}) {
  const value = {
    w: base,
    session: {
      authenticated: true,
      user: { instance_admin: owner },
      project_permissions: { project: grants },
    },
    notify() {},
    refresh: async () => {},
  };
  return renderToStaticMarkup(
    createElement(
      WorkspaceContext.Provider,
      { value },
      createElement(component, props),
    ),
  );
}

test("readers cannot render model policy editor, model administration controls", () => {
  const html = render(ModelSettings, ["knowledge:read"]);
  assert.match(html, /模型策略由项目管理员维护/);
  assert.doesNotMatch(html, /服务基础地址|保存策略/);
});

test("project administration does not imply instance OAuth issuer administration", () => {
  const projectAdmin = render(OAuthSettings, [
    "knowledge:read",
    "project:manage",
  ]);
  assert.match(projectAdmin, /当前项目的外部客户端/);
  assert.doesNotMatch(projectAdmin, /实例授权服务|登记授权服务<\/button>/);
  const owner = render(
    OAuthSettings,
    ["knowledge:read", "project:manage"],
    true,
  );
  assert.match(owner, /实例授权服务/);
  assert.match(owner, /登记授权服务/);
});

test("relationship corrections preserve exact original versions and feedback never implies a target mutation", () => {
  const originals = [
    { id: "one", row_version: 4 },
    { id: "two", row_version: 9 },
  ];
  const targets = [
    {
      to_revision_id: "new-target",
      evidence_ids: ["source-evidence", "target-evidence"],
    },
  ];
  const payload = correctionPayload(
    "draft",
    "merge",
    "  merged with evidence  ",
    targets,
    originals,
    "",
  );
  assert.deepEqual(payload, {
    analysis_set_id: "draft",
    operation: "merge",
    reason: "merged with evidence",
    targets,
    merge_relation_ids: ["one", "two"],
    expected_versions: { one: 4, two: 9 },
  });
  const feedback = correctionPayload(
    "draft",
    "feedback",
    "wrong target",
    targets,
    originals,
    "correct-target",
  );
  assert.deepEqual(feedback, {
    analysis_set_id: "draft",
    operation: "feedback",
    reason: "wrong target",
    targets: [],
    correct_target_revision_id: "correct-target",
  });
  const split = correctionPayload(
    "draft",
    "split",
    "split",
    targets,
    originals,
    "",
  );
  assert.equal(Object.hasOwn(split, "merge_relation_ids"), false);
});

test("structural relationship types remain distinct from inferred links", () => {
  assert.equal(deterministicRelationTypes.has("handler_of"), true);
  assert.equal(deterministicRelationTypes.has("maps_to_sql"), true);
  assert.equal(deterministicRelationTypes.has("calls"), false);
  assert.equal(deterministicRelationTypes.has("related_to"), false);
});

test("readers can submit correction feedback but cannot replace endpoints", () => {
  const relation = {
    id: "relation",
    row_version: 1,
    from_name: "Action",
    to_name: "API",
  };
  const html = render(RelationOperations, ["knowledge:read"], false, {
    relation,
  });
  assert.match(html, /关联不正确/);
  assert.doesNotMatch(html, /改选目标|拆分|合并/);
});

test("module hierarchy keeps identities and search scope pins one exact baseline", () => {
  const root = {
    id: "parent",
    key: "parent",
    children: [{ id: "child", key: "child", children: [] }],
  };
  assert.deepEqual(
    flattenModules([root]).map(({ id, depth }) => ({ id, depth })),
    [
      { id: "parent", depth: 0 },
      { id: "child", depth: 1 },
    ],
  );
  assert.equal(moduleBaseline("draft", "release"), "release_id=release");
  assert.equal(moduleBaseline("draft"), "analysis_set_id=draft");
  assert.equal(moduleBaseline(), "");
});

test("audit filters use server-side fields and preserve precise date and pagination scope", () => {
  const query = new URLSearchParams(
    auditQuery(
      {
        actor_id: " actor ",
        action: " relation.review ",
        resource_id: " relation ",
        outcome: "denied",
        since: "2026-10-25T00:00:00Z",
        until: "2026-10-26T00:00:00Z",
        limit: 50,
      },
      "last-id",
    ),
  );
  assert.equal(query.get("actor_id"), "actor");
  assert.equal(query.get("action"), "relation.review");
  assert.equal(query.get("resource_id"), "relation");
  assert.equal(query.get("outcome"), "denied");
  assert.equal(query.get("since"), "2026-10-25T00:00:00.000Z");
  assert.equal(query.get("cursor"), "last-id");
  assert.equal(query.get("limit"), "50");
  assert.throws(
    () =>
      auditQuery({
        since: "2026-10-26",
        until: "2026-10-25",
        limit: 100,
        actor_id: "",
        action: "",
        resource_id: "",
        outcome: "",
      }),
    /开始时间/,
  );
});

test("project profile update excludes response metadata and always retains member visibility", () => {
  assert.deepEqual(
    projectProfilePayload({
      project_id: "project",
      row_version: 3,
      business_domain: " billing ",
      responsible_user_id: "member",
      responsible_name: "Member",
      visibility: "project_members",
      default_analysis_language: "mixed",
    }),
    {
      business_domain: "billing",
      responsible_user_id: "member",
      visibility: "project_members",
      default_analysis_language: "mixed",
    },
  );
});
