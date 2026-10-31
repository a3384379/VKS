import type { Entity } from "./types";

/** A section's owner is established only by the exact immutable revision mapping. */
export function documentContext(entity: Entity, documents: Entity[]) {
  const owners =
    entity.type === "document"
      ? [entity]
      : ["section", "document_section"].includes(entity.type)
        ? documents.filter(
            (document) =>
              Array.isArray(document.metadata.section_revision_ids) &&
              document.metadata.section_revision_ids.includes(
                entity.entity_revision_id,
              ),
          )
        : [];
  const document = owners.length === 1 ? owners[0] : undefined;
  return {
    documentId: document?.entity_id,
    revisionId: document?.entity_revision_id,
  };
}
