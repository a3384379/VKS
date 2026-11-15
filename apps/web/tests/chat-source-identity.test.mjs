import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import { transform } from "esbuild";

const source = await fs.readFile(
  new URL("../../studio/src/chatSourceFingerprint.ts", import.meta.url),
  "utf8",
);
const compiled = await transform(source, { loader: "ts", format: "esm" });
const { chatSourceFingerprint } = await import(
  "data:text/javascript;base64," + Buffer.from(compiled.code).toString("base64")
);

test("chat sources survive binding counters and catalog order, but reset for exact source replacements", () => {
  const catalog = {
    selection_version: 1,
    documents: [
      { id: "a", revision_id: "a1" },
      { id: "b", revision_id: "b1" },
    ],
    packages: [
      { id: "p", kind: "prototype", sha256: "sha1", revision_id: "p1" },
    ],
  };
  const first = chatSourceFingerprint(catalog);
  assert.equal(
    chatSourceFingerprint({
      ...catalog,
      selection_version: 8,
      documents: [...catalog.documents].reverse(),
    }),
    first,
  );
  assert.notEqual(
    chatSourceFingerprint({
      ...catalog,
      documents: [{ id: "a", revision_id: "a2" }, catalog.documents[1]],
    }),
    first,
  );
  assert.notEqual(
    chatSourceFingerprint({
      ...catalog,
      packages: [{ ...catalog.packages[0], sha256: "sha2" }],
    }),
    first,
  );
  assert.equal(
    chatSourceFingerprint({
      ...catalog,
      source_fingerprint: "authorized-selected-chapters-1",
    }),
    "authorized-selected-chapters-1",
  );
  assert.notEqual(
    chatSourceFingerprint({
      ...catalog,
      source_fingerprint: "authorized-selected-chapters-2",
    }),
    "authorized-selected-chapters-1",
  );
});
