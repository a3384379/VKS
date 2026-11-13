"""Portable declarations for the visible raster portion of a fixed snapshot."""

from collections import defaultdict
from copy import deepcopy

from .reading import artifact_policy, prototype_surface


def add_prototypes(service, s, rows, capabilities, add_file, identity):
    from .exports import allow_artifact, read_original
    from .knowledge import SourceSnapshot

    actions = defaultdict(dict)
    for entity, revision, art in rows:
        if (
            entity.type == "action"
            and revision.metadata_json.get("prototype_action")
            and revision.metadata_json.get("status") != "removed"
        ):
            actions[revision.metadata_json["page_key"]][entity.stable_key] = (entity, revision, art)

    groups = defaultdict(list)
    included = {}
    originals = {}
    for entity, revision, origin in rows:
        manifest = revision.metadata_json.get("prototype_manifest")
        if not manifest or not origin or not allow_artifact(origin, capabilities, s):
            continue
        policy = artifact_policy(s, origin)
        if policy.get("requires_trace_read"):
            continue
        snapshot = s.get(SourceSnapshot, origin.snapshot_id)
        if not snapshot or snapshot.role != "prototype":
            continue
        group = snapshot.source_key
        sensitivity = "confidential" if policy.get("requires_source_read") else "internal"
        permitted = {
            key: (action, rev, art)
            for key, (action, rev, art) in actions[entity.stable_key].items()
            if allow_artifact(art, capabilities, s) and not artifact_policy(s, art).get("requires_trace_read")
        }
        states = deepcopy(manifest["states"])
        used = set()
        for state in states:
            state["anchors"] = [anchor for anchor in state["anchors"] if anchor["actionKey"] in permitted]
            used.update(anchor["actionKey"] for anchor in state["anchors"])
            surface = state.get("surface")
            if surface:
                art = prototype_surface(s, origin.project_id, origin.id, state)
                asset_policy = artifact_policy(s, art) if art else {}
                raw = (
                    read_original(service, art)
                    if art and allow_artifact(art, capabilities, s) and not asset_policy.get("requires_trace_read")
                    else None
                )
                if raw is None:
                    state.pop("surface", None)
                    continue
                path = f"export/surfaces/{group}/{art.sha256}.png"
                classification = "confidential" if asset_policy.get("requires_source_read") else art.sensitivity
                add_file(path, raw, "screenshot", group, "image/png", classification)
                originals[art.id, path] = art
                surface["artifactPath"] = path
        if any(artifact_policy(s, permitted[key][2]).get("requires_source_read") for key in used):
            sensitivity = "confidential"
        groups[group].append(
            (
                {
                    "pageKey": entity.stable_key,
                    "name": revision.name,
                    "routeAliases": manifest["routeAliases"],
                    "defaultStateKey": manifest["defaultStateKey"],
                    "states": states,
                    "actions": [{"actionKey": key, "label": permitted[key][1].name} for key in sorted(used)],
                },
                sensitivity,
            )
        )
        included[entity.stable_key] = group
        for key in used:
            included[key] = group
    sidecars, references = [], {}
    from .exports import canonical

    for group, entries in sorted(groups.items()):
        sensitivity = "confidential" if any(value == "confidential" for _, value in entries) else "internal"
        ref = add_file(
            f"export/prototypes/{group}.json",
            canonical({**identity, "sourceKey": group, "pages": [row for row, _ in entries]}),
            "fixture",
            group,
            "application/json",
            sensitivity,
        )
        sidecars.append(ref)
        references.update({key: ref for key, source in included.items() if source == group})
    return sidecars, references, originals


def portable_points(s, rows, relations, section_keys, action_keys, capabilities):
    from .exports import allow_artifact

    selected = {
        revision.id: (entity, revision, art)
        for entity, revision, art in rows
        if entity.type == "business_point"
        and allow_artifact(art, capabilities, s)
        and not artifact_policy(s, art).get("requires_trace_read")
    }
    cited = defaultdict(set)
    bindings = []
    for edge in relations:
        if edge["excluded"] or edge["validity_status"] != "valid":
            continue
        start, end = edge["from_revision_id"], edge["to_revision_id"]
        if edge["relation_type"] == "supported_by_section" and start in selected and end in section_keys:
            cited[start].add(section_keys[end])
        elif edge["relation_type"] == "has_business_point" and start in action_keys and end in selected:
            scope = edge.get("prototype_scope", {})
            bindings.append(
                {
                    "action": action_keys[start],
                    "point": selected[end][0].stable_key,
                    "states": scope.get("states", []),
                    "priority": scope.get("priority", 0),
                }
            )
    points = [
        {
            "key": entity.stable_key,
            "title": rev.name,
            "summary": rev.content,
            **{field: rev.metadata_json.get(field, "") for field in ("applicability", "outcome", "exceptions")},
            "sections": sorted(cited[rev.id]),
        }
        for entity, rev, _ in selected.values()
    ]
    sensitivity = (
        "confidential"
        if any(artifact_policy(s, art).get("requires_source_read") for _, _, art in selected.values())
        else "internal"
    )
    return points, bindings, sensitivity, selected
