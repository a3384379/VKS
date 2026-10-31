import type { Entity, Relation } from "./types";
import { journeyKinds } from "./journey-reading";
import type { JourneySemantics } from "./journeys";

export function materialKind(entity: Entity): JourneySemantics["kind"] {
  const dsl = entity.metadata.journey_dsl as
    | { semantics?: { kind?: string } }
    | undefined;
  const kind = dsl?.semantics?.kind || "unclassified";
  return Object.hasOwn(journeyKinds, kind)
    ? (kind as JourneySemantics["kind"])
    : "unclassified";
}

// Keep aligned with the server's action knowledge coverage: membership is context,
// while a visible knowledge endpoint establishes the counted association.
export function coveredActionRevisions(
  entities: Entity[],
  relations: Relation[],
) {
  const actions = new Set(
    entities
      .filter((e) => e.type === "action")
      .map((e) => e.entity_revision_id),
  );
  const knowledge = new Set(
    entities
      .filter((e) =>
        ["api", "symbol", "sql", "table", "document", "section"].includes(
          e.type,
        ),
      )
      .map((e) => e.entity_revision_id),
  );
  const result = new Set<string>();
  for (const relation of relations) {
    if (
      relation.excluded ||
      relation.validity_status !== "valid" ||
      ["contains", "renders"].includes(
        relation.relation_type || relation.type || "",
      )
    )
      continue;
    if (
      actions.has(relation.from_revision_id) &&
      knowledge.has(relation.to_revision_id)
    )
      result.add(relation.from_revision_id);
    if (
      actions.has(relation.to_revision_id) &&
      knowledge.has(relation.from_revision_id)
    )
      result.add(relation.to_revision_id);
  }
  return result;
}

export function exactResultEntity(
  entities: Entity[],
  kind: string,
  id: string,
) {
  const types: Record<string, string> = {
    document_revision: "document",
    journey_revision: "journey",
    section_revision: "section",
  };
  return entities.find(
    (e) =>
      e.entity_revision_id === id && (!types[kind] || e.type === types[kind]),
  );
}
