type SourceCatalog = {
  source_fingerprint?: string;
  packages: {
    id: string;
    kind: string;
    sha256: string;
    revision_id?: string;
  }[];
  documents: { id: string; revision_id: string }[];
};

/** Binding saves change the draft counter, but do not replace the chat's sources. */
export function chatSourceFingerprint(catalog: SourceCatalog | null): string {
  if (!catalog) return "";
  // New servers also include permission-filtered selected chapter identities.
  if (catalog.source_fingerprint) return catalog.source_fingerprint;
  const ordered = (rows: string[][]) =>
    rows.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  return JSON.stringify({
    documents: ordered(
      catalog.documents.map((doc) => [doc.id, doc.revision_id]),
    ),
    packages: ordered(
      catalog.packages.map((pack) => [
        pack.id,
        pack.kind,
        pack.revision_id || "",
        pack.sha256,
      ]),
    ),
  });
}
