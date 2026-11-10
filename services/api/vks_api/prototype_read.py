"""Version-scoped, permission-filtered projections for prototype navigation."""

from collections import defaultdict

from sqlalchemy import select

from .models import Entity, PrototypeSettings, Revision
from .reading import ReadingService, chunks, explanation_relation, relation_is_visible
from .service import DomainError


def missing():
    return DomainError("VKS_NOT_FOUND", "当前版本未找到可读取的原型或知识", 404)


def find_selected(s, service, actor, pid, choices, kind, key):
    entity = s.scalar(select(Entity).where(Entity.project_id == pid, Entity.type == kind, Entity.stable_key == key))
    rid = choices.get(entity.id) if entity else None
    row = s.get(Revision, rid) if rid else None
    if not row or not service._visible_revision(s, actor, pid, row):
        raise missing()
    return entity, row


def selected_id(s, service, actor, pid, choices, rid, kind):
    row = s.get(Revision, rid)
    entity = s.get(Entity, row.entity_id) if row else None
    if (
        not entity
        or entity.type != kind
        or choices.get(entity.id) != rid
        or not service._visible_revision(s, actor, pid, row)
    ):
        raise missing()
    return entity, row


def page_state(page, state_key=None):
    manifest = page.metadata_json.get("prototype_manifest")
    if not manifest:
        raise missing()
    state = next(
        (row for row in manifest["states"] if row["stateKey"] == (state_key or manifest["defaultStateKey"])), None
    )
    if not state:
        raise missing()
    return state


class PrototypeReader:
    def __init__(self, service):
        self.service = service
        self.reading = ReadingService(service)

    def scope(self, s, actor, pid, analysis_set_id, release_id):
        if bool(analysis_set_id) == bool(release_id):
            raise DomainError("VKS_INVALID_INPUT", "请选择准确的草稿或快照", 422)
        analysis, release = self.reading.baseline(s, actor, pid, analysis_set_id, release_id)
        choices, index = self.reading.selections(s, analysis, release)
        return analysis, release, choices, index

    def _entities(self, s, actor, pid, choices, types):
        for batch in chunks(choices):
            for entity in s.scalars(select(Entity).where(Entity.id.in_(batch), Entity.type.in_(types))):
                row = s.get(Revision, choices[entity.id])
                if self.service._visible_revision(s, actor, pid, row):
                    yield entity, row

    def menu(self, actor, pid, analysis_set_id, release_id, after, limit):
        with self.reading.db() as s:
            _, _, choices, _ = self.scope(s, actor, pid, analysis_set_id, release_id)
            rows = [
                {
                    "page_key": entity.stable_key,
                    "name": rev.name,
                    "page_revision_id": rev.id,
                    "routes": manifest["routeAliases"],
                    "has_surface": "surface" in page_state(rev),
                }
                for entity, rev in self._entities(s, actor, pid, choices, {"page"})
                if (manifest := rev.metadata_json.get("prototype_manifest"))
            ]
            settings = s.get(PrototypeSettings, pid)
            order = {key: idx for idx, key in enumerate(settings.menu_order if settings else [])}
            rows.sort(key=lambda row: (order.get(row["page_key"], len(order)), row["page_key"]))
            positions = {row["page_key"]: idx for idx, row in enumerate(rows)}
            if after and after not in positions:
                raise DomainError("VKS_VERSION_CONFLICT", "原型目录已变动，请重新打开", 409)
            available = rows[(positions[after] + 1) if after else 0 :]
            entry = settings.entry_page_key if settings else None
            return {
                "pages": available[:limit],
                "total": len(rows),
                "entry_page_key": entry
                if any(row["page_key"] == entry and row["has_surface"] for row in rows)
                else None,
                "next_cursor": available[limit - 1]["page_key"] if len(available) > limit else None,
            }

    def page(self, actor, pid, page_key, state_key, analysis_set_id, release_id):
        service = self.service
        with self.reading.db() as s:
            _, _, choices, _ = self.scope(s, actor, pid, analysis_set_id, release_id)
            _, row = find_selected(s, service, actor, pid, choices, "page", page_key)
            state = page_state(row, state_key)
            anchors = []
            for anchor in state["anchors"]:
                try:
                    action, rev = find_selected(s, service, actor, pid, choices, "action", anchor["actionKey"])
                except DomainError as exc:
                    if exc.status == 404:
                        continue
                    raise
                if rev.metadata_json.get("page_key") == page_key and rev.metadata_json.get("status") != "removed":
                    anchors.append({**anchor, "action_id": action.id, "action_revision_id": rev.id, "label": rev.name})
            surface = state.get("surface")
            return {
                "page_key": page_key,
                "page_revision_id": row.id,
                "name": row.name,
                "state_key": state["stateKey"],
                "state_name": state["name"],
                "origin": state["origin"],
                "viewport": state.get("viewport"),
                "image_sha256": surface["sha256"] if surface else None,
                "anchors": anchors,
            }

    def _edges(self, s, actor, pid, analysis, index, choices, ids, types, policy):
        edges = [
            edge
            for batch in chunks(ids)
            for edge in self.reading.edge_rows(s, analysis.id, index, frontier=set(batch))
            if edge["relation_type"] in types and explanation_relation(edge, policy)
        ]
        refs = {edge[key] for edge in edges for key in ("from_revision_id", "to_revision_id")}
        visible = set(
            self.reading.visible_ids(s, actor, pid, {eid: rid for eid, rid in choices.items() if rid in refs}).values()
        )
        evidence = self.reading.visible_evidence(
            s, actor, pid, analysis.id, {eid for edge in edges for eid in edge["evidence_ids"]}
        )
        source, trace = self.reading.permissions(s, actor, pid)
        return list(
            {
                edge["id"]: {**edge, "evidence_ids": [eid for eid in edge["evidence_ids"] if eid in evidence]}
                for edge in edges
                if relation_is_visible(edge, visible, evidence, s=s, source_allowed=source, trace_allowed=trace)
            }.values()
        )

    def context(self, actor, pid, page_key, action_key, trigger, analysis_set_id, release_id, policy="valid"):
        service = self.service
        with self.reading.db() as s:
            analysis, release, choices, index = self.scope(s, actor, pid, analysis_set_id, release_id)
            _, page = find_selected(s, service, actor, pid, choices, "page", page_key)
            action, rev = find_selected(s, service, actor, pid, choices, "action", action_key)
            state = page_state(page, trigger)
            anchor = next((row for row in state["anchors"] if row["actionKey"] == action_key), None)
            if (
                not anchor
                or rev.metadata_json.get("page_key") != page_key
                or rev.metadata_json.get("status") == "removed"
            ):
                raise missing()
            target = page_state(page, anchor.get("targetStateKey") or trigger)
            edges = self._edges(
                s, actor, pid, analysis, index, choices, {rev.id}, {"has_business_point", "documents"}, policy
            )
            links = []
            documents = []
            for edge in edges:
                if edge["from_revision_id"] != rev.id:
                    continue
                if edge["relation_type"] == "documents":
                    documents.append(self._section(s, edge))
                    continue
                scope = edge.get("prototype_scope", {})
                if not scope.get("states") or trigger in scope["states"]:
                    links.append(edge)
            ids = {row["to_revision_id"] for row in links}
            sources = defaultdict(list)
            for edge in self._edges(s, actor, pid, analysis, index, choices, ids, {"supported_by_section"}, policy):
                if edge["from_revision_id"] in ids:
                    sources[edge["from_revision_id"]].append(self._section(s, edge))
            points = []
            for edge in sorted(
                links,
                key=lambda row: (
                    not bool(row.get("prototype_scope", {}).get("states")),
                    row.get("prototype_scope", {}).get("priority", 0),
                    row["id"],
                ),
            ):
                point = s.get(Revision, edge["to_revision_id"])
                entity = s.get(Entity, point.entity_id)
                points.append(
                    {
                        "point_key": entity.stable_key,
                        "point_revision_id": point.id,
                        "title": point.name,
                        "summary": point.content,
                        "applicability": point.metadata_json.get("applicability", ""),
                        "outcome": point.metadata_json.get("outcome", ""),
                        "exceptions": point.metadata_json.get("exceptions", ""),
                        "sources": sorted(sources[point.id], key=lambda row: row["section_key"]),
                        "binding_id": edge["id"],
                        "binding_version": edge["row_version"],
                        "state_keys": edge.get("prototype_scope", {}).get("states", []),
                        "priority": edge.get("prototype_scope", {}).get("priority", 0),
                        "validity": edge["validity_status"],
                    }
                )
            return {
                "scope": {"analysis_set_id": analysis.id, **({"release_id": release.id} if release else {})},
                "page_key": page_key,
                "page_revision_id": page.id,
                "action_key": action_key,
                "action_id": action.id,
                "action_revision_id": rev.id,
                "trigger_state_key": trigger,
                "display_state_key": target["stateKey"] if "surface" in target else trigger,
                "missing_target_surface": target["stateKey"] != trigger and "surface" not in target,
                "points": points,
                "documents": sorted(documents, key=lambda row: row["section_key"]),
                "availability": "available" if points or documents else "no_visible_explanation",
            }

    @staticmethod
    def _section(s, edge):
        row = s.get(Revision, edge["to_revision_id"])
        entity = s.get(Entity, row.entity_id)
        return {
            "section_key": entity.stable_key,
            "section_id": entity.id,
            "section_revision_id": row.id,
            "title": row.name,
            "excerpt": row.content[:400],
            "evidence_ids": edge["evidence_ids"],
            "validity": edge["validity_status"],
        }

    def references(self, actor, pid, section_key, analysis_set_id, release_id, after, limit):
        with self.reading.db() as s:
            analysis, _, choices, index = self.scope(s, actor, pid, analysis_set_id, release_id)
            _, section = find_selected(s, self.service, actor, pid, choices, "section", section_key)
            incoming = self._edges(
                s, actor, pid, analysis, index, choices, {section.id}, {"documents", "supported_by_section"}, "valid"
            )
            points, links = set(), []
            for edge in incoming:
                if edge["to_revision_id"] == section.id:
                    if edge["relation_type"] == "supported_by_section":
                        points.add(edge["from_revision_id"])
                    else:
                        links.append(edge)
            links.extend(
                edge
                for edge in self._edges(
                    s, actor, pid, analysis, index, choices, points, {"has_business_point"}, "valid"
                )
                if edge["to_revision_id"] in points
            )
            rows = {}
            for edge in links:
                action = s.get(Revision, edge["from_revision_id"])
                page_key = action.metadata_json.get("page_key")
                if not page_key or action.metadata_json.get("status") == "removed":
                    continue
                try:
                    page, rev = find_selected(s, self.service, actor, pid, choices, "page", page_key)
                except DomainError as exc:
                    if exc.status == 404:
                        continue
                    raise
                action_entity = s.get(Entity, action.entity_id)
                states = edge.get("prototype_scope", {}).get("states", [])
                for state in rev.metadata_json.get("prototype_manifest", {}).get("states", []):
                    key = state["stateKey"]
                    if (states and key not in states) or not any(
                        a["actionKey"] == action_entity.stable_key for a in state["anchors"]
                    ):
                        continue
                    cursor = f"{page.stable_key}:{key}:{action_entity.stable_key}"
                    rows[cursor] = {
                        "page_key": page.stable_key,
                        "page_id": page.id,
                        "page_name": rev.name,
                        "state_key": key,
                        "state_name": state["name"],
                        "action_key": action_entity.stable_key,
                        "action_id": action_entity.id,
                        "action_name": action.name,
                    }
            keys = sorted(key for key in rows if not after or key > after)
            return {
                "section_revision_id": section.id,
                "references": [rows[key] for key in keys[:limit]],
                "total": len(rows),
                "next_cursor": keys[limit - 1] if len(keys) > limit else None,
            }
