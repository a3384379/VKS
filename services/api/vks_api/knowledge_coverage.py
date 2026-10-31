"""Count action knowledge associations, separately from structural UI membership."""

KNOWLEDGE_TYPES = frozenset(("api", "symbol", "sql", "table", "document", "section"))
STRUCTURAL_RELATIONS = frozenset(("contains", "renders"))


def action_knowledge_revisions(entities, relations):
    """Inputs must already be filtered to the caller's selected, visible baseline."""
    actions = {row["entity_revision_id"] for row in entities if row["type"] == "action"}
    knowledge = {row["entity_revision_id"] for row in entities if row["type"] in KNOWLEDGE_TYPES}
    covered = set()
    for row in relations:
        if (
            (row["excluded"] is True)
            or row["validity_status"] != "valid"
            or row["relation_type"] in STRUCTURAL_RELATIONS
        ):
            continue
        source, target = row["from_revision_id"], row["to_revision_id"]
        if source in actions and target in knowledge:
            covered.add(source)
        if target in actions and source in knowledge:
            covered.add(target)
    return covered
