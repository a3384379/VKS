"""Version comparison, evidence-preserving migration proposals, and revalidation."""

from __future__ import annotations

import json
from collections import deque
from hashlib import sha256

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .documents import body_fingerprint
from .models import (
    Analysis,
    Artifact,
    Entity,
    Evidence,
    Relation,
    Release,
    Revision,
    relation_payload,
    uid,
)

PROTOTYPE_RELATIONS = {"has_business_point", "supported_by_section"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(code, message, status=422, details=None):
    from .service import DomainError

    raise DomainError(code, message, status, details)


class VersionService:
    def __init__(self, service):
        self.service = service

    def _baselines(self, s, pid, release_id, analysis_id):
        old, new = s.get(Release, release_id), s.get(Analysis, analysis_id)
        if not old or not new or old.project_id != pid or new.project_id != pid:
            fail("VKS_NOT_FOUND", "未找到版本基线", 404)
        if sha256(canonical(old.manifest_json).encode()).hexdigest() != old.manifest_sha256:
            fail("VKS_INTEGRITY_ERROR", "版本对比的发布清单摘要不匹配", 409)
        return old, new

    def compare(self, s, pid, from_release_id, to_analysis_set_id):
        old, new = self._baselines(s, pid, from_release_id, to_analysis_set_id)
        before = {x["entity_id"]: s.get(Revision, x["entity_revision_id"]) for x in old.manifest_json["selections"]}
        after = {r.entity_id: r for r in self.service._revisions(s, new.id)}
        changes = []
        java_tokens = {}

        def lexical(artifact):
            if artifact.id not in java_tokens:
                from packages.analyzers.java_spring import java_lexical_sha

                raw = (self.service.data_dir / "objects" / artifact.object_key).read_bytes()
                java_tokens[artifact.id] = java_lexical_sha(raw) if sha256(raw).hexdigest() == artifact.sha256 else None
            return java_tokens[artifact.id]

        for eid in sorted(set(before) | set(after)):
            a, b = before.get(eid), after.get(eid)
            if a and b and a.id == b.id:
                continue
            row = {"entity_id": eid, "change_kind": "added" if not a else "removed" if not b else "modified"}
            if a:
                row["old_revision_id"] = a.id
            if b:
                row["new_revision_id"] = b.id
            if a and b:
                first = s.get(Artifact, a.artifact_id) if a.artifact_id else None
                second = s.get(Artifact, b.artifact_id) if b.artifact_id else None
                same_body = (
                    a.semantic_content_sha256 == b.semantic_content_sha256
                    or s.get(Entity, eid).type == "section"
                    and body_fingerprint(a) == body_fingerprint(b)
                )
                if a.name != b.name and same_body:
                    row["change_kind"] = "renamed"
                elif same_body and (
                    a.locator != b.locator
                    or a.artifact_id
                    and b.artifact_id
                    and s.get(Artifact, a.artifact_id).relative_path != s.get(Artifact, b.artifact_id).relative_path
                ):
                    row["change_kind"] = "moved"
                if (
                    first
                    and second
                    and first.kind == second.kind == "source"
                    and first.relative_path == second.relative_path
                    and first.relative_path.lower().endswith(".java")
                    and first.sha256 != second.sha256
                ):
                    token = lexical(first)
                    if token is not None and token == lexical(second):
                        row.update(
                            change_kind="non_semantic",
                            semantic_boundary="whole_file_java_tokens_equal_comments_or_formatting_only",
                        )
            row["entity_type"] = s.get(Entity, eid).type
            if any(rev and rev.metadata_json.get("prototype_manifest") for rev in (a, b)):
                row["change_scope"] = "prototype_layout"
            elif any(rev and rev.metadata_json.get("prototype_action") for rev in (a, b)):
                row["change_scope"] = "prototype_action"
            elif row["entity_type"] in ("document", "section"):
                row["change_scope"] = "document_content"
            elif row["entity_type"] == "business_point":
                row["change_scope"] = "business_point"
            changes.append(row)
        changed = {c["entity_id"] for c in changes}
        relations = list(s.scalars(select(Relation).where(Relation.analysis_set_id == new.id)))
        binding_changes = self._binding_changes(s, old, relations, {row.id for row in after.values()})
        impacted = [
            self.service.relation_dto(r)
            for r in relations
            if any(s.get(Revision, rid).entity_id in changed for rid in (r.from_revision_id, r.to_revision_id))
        ]
        paths, path_scope = self.impact_paths(s, changed, relations, with_scope=True)
        affected = changed | {path["impacted_entity_id"] for path in paths}
        journey_impacts = []
        for revision in [*before.values(), *after.values()]:
            if s.get(Entity, revision.entity_id).type != "journey":
                continue
            dsl = revision.metadata_json.get("journey_dsl", {})
            for binding in dsl.get("semantics", {}).get("source_refs", []):
                if binding["entity_id"] in affected:
                    journey_impacts.append(
                        dict(
                            journey_id=revision.entity_id,
                            journey_revision_id=revision.id,
                            step_key="semantics",
                            binding_entity_id=binding["entity_id"],
                            binding_revision_id=binding["revision_id"],
                            basis="explicit_journey_source",
                        )
                    )
            for node in dsl.get("nodes", []):
                bindings = [node.get("binding", {})]
                for field in ("state_ref", "capture_ref"):
                    if node.get(field):
                        bindings.append(
                            dict(entity_id=node[field]["scenario_id"], revision_id=node[field]["scenario_revision_id"])
                        )
                for binding in bindings:
                    if binding.get("entity_id") in affected:
                        journey_impacts.append(
                            dict(
                                journey_id=revision.entity_id,
                                journey_revision_id=revision.id,
                                step_key=node["key"],
                                binding_entity_id=binding["entity_id"],
                                binding_revision_id=binding["revision_id"],
                                basis="explicit_journey_binding",
                            )
                        )
        removed_count = sum(row["change_kind"] == "removed" for row in changes)
        all_journey_impacts = list({canonical(row): row for row in journey_impacts}.values())
        path_relation_ids = {rid for path in paths for rid in path["relation_ids"]}
        return {
            "from_release_id": from_release_id,
            "to_analysis_set_id": to_analysis_set_id,
            "to_selection_version": new.selection_version,
            "source_vectors": {"from": old.manifest_json.get("sources", []), "to": new.sources},
            "impact_scope": dict(
                path_scope,
                journey_limit=200,
                journey_truncated=len(all_journey_impacts) > 200,
                is_truncated=path_scope["is_truncated"] or len(all_journey_impacts) > 200,
            ),
            "_target_revision_ids": sorted(row.id for row in after.values()),
            "_binding_changes": binding_changes,
            "_path_relations": [
                dict(
                    id=row.id,
                    from_revision_id=row.from_revision_id,
                    to_revision_id=row.to_revision_id,
                    evidence_ids=list(row.evidence_ids),
                )
                for row in relations
                if row.id in path_relation_ids
            ],
            "changes": changes,
            "impacted_relations": impacted,
            "journey_impacts": all_journey_impacts[:200],
            "scope_anomaly": len(before) >= 10 and removed_count / len(before) >= 0.25,
            "impact_paths": paths,
            "unknowns": ["材料或定位变化不等于业务语义已改变；重新确认不产生运行因果证据"],
        }

    def _binding_changes(self, s, release, relations, selected):
        def by_endpoints(rows, scope):
            result = {}
            for row in rows:
                kind = row["relation_type"]
                if kind not in PROTOTYPE_RELATIONS and kind != "documents":
                    continue
                if row["from_revision_id"] not in scope or row["to_revision_id"] not in scope:
                    continue
                start = s.get(Revision, row["from_revision_id"])
                end = s.get(Revision, row["to_revision_id"])
                if not start or not end or (kind == "documents" and not start.metadata_json.get("prototype_action")):
                    continue
                result[kind, start.entity_id, end.entity_id] = row
            return result

        def signature(row):
            scope = row.get("prototype_scope", {})
            proofs = []
            for eid in row["evidence_ids"]:
                ev = s.get(Evidence, eid)
                if ev:
                    art = s.get(Artifact, ev.artifact_id)
                    proofs.append(
                        canonical([ev.evidence_kind, ev.content_sha256, ev.locator, art.sha256 if art else None])
                    )
            return row["excluded"], row["validity_status"], scope, sorted(set(proofs))

        before = by_endpoints(
            (relation_payload(row) for row in release.manifest_json["relations"]),
            {row["entity_revision_id"] for row in release.manifest_json["selections"]},
        )
        after = by_endpoints((self.service.relation_dto(row) for row in relations), selected)
        changes = []
        for kind, source, target in sorted(before.keys() | after.keys()):
            key = (kind, source, target)
            prior, current = before.get(key), after.get(key)
            if (not prior and current["excluded"]) or (prior and current and signature(prior) == signature(current)):
                continue
            changes.append(
                {
                    "relation_type": kind,
                    "from_entity_id": source,
                    "to_entity_id": target,
                    "change_kind": "added"
                    if not prior
                    else "removed"
                    if not current or current["excluded"]
                    else "modified",
                    "before": prior,
                    "after": current,
                }
            )
        return changes

    def impact_paths(self, s, seeds, relations, depth=3, max_nodes=200, *, with_scope=False, path_limit=2000):
        adjacency = {}
        for relation in relations:
            source = s.get(Revision, relation.from_revision_id).entity_id
            target = s.get(Revision, relation.to_revision_id).entity_id
            adjacency.setdefault(source, []).append((target, relation.id))
            adjacency.setdefault(target, []).append((source, relation.id))
        output = []
        depth_truncated, nodes_truncated, paths_truncated = False, False, False
        for seed in sorted(seeds):
            queue = deque([(seed, [seed], [])])
            seen = {seed}
            while queue and len(seen) <= max_nodes:
                current, nodes, edges = queue.popleft()
                if len(edges) >= depth:
                    depth_truncated = depth_truncated or any(
                        target not in seen for target, _ in adjacency.get(current, [])
                    )
                    continue
                for target, relation in sorted(adjacency.get(current, [])):
                    if target in seen:
                        continue
                    if len(seen) >= max_nodes:
                        nodes_truncated = True
                        continue
                    if len(output) >= path_limit:
                        paths_truncated = True
                        break
                    seen.add(target)
                    path = [*nodes, target]
                    links = [*edges, relation]
                    output.append(
                        {
                            "changed_entity_id": seed,
                            "impacted_entity_id": target,
                            "entity_ids": path,
                            "relation_ids": links,
                            "depth": len(links),
                        }
                    )
                    queue.append((target, path, links))
                if paths_truncated:
                    break
            if paths_truncated:
                break
        scope = dict(
            max_hops=depth,
            max_nodes_per_change=max_nodes,
            path_limit=path_limit,
            depth_truncated=depth_truncated,
            nodes_truncated=nodes_truncated,
            paths_truncated=paths_truncated,
            is_truncated=depth_truncated or nodes_truncated or paths_truncated,
        )
        return (output, scope) if with_scope else output

    def _copy_evidence(self, s, analysis, ids):
        output = []
        for eid in ids:
            evidence = s.get(Evidence, eid)
            if not evidence or evidence.project_id != analysis.project_id:
                continue
            key = "drift-copy." + eid
            existing = s.scalar(
                select(Evidence).where(Evidence.analysis_set_id == analysis.id, Evidence.stable_key == key)
            )
            if not existing:
                existing = Evidence(
                    id=uid(),
                    project_id=analysis.project_id,
                    analysis_set_id=analysis.id,
                    artifact_id=evidence.artifact_id,
                    stable_key=key,
                    evidence_kind=evidence.evidence_kind,
                    content_sha256=evidence.content_sha256,
                    locator=evidence.locator,
                    excerpt=evidence.excerpt,
                    claim=evidence.claim,
                )
                s.add(existing)
                s.flush()
            output.append(existing.id)
        return output

    def detect_drift(self, s, pid, from_release_id, to_analysis_set_id, actor_id=None):
        old, new = self._baselines(s, pid, from_release_id, to_analysis_set_id)
        s.refresh(new, with_for_update=True)
        if new.state == "frozen":
            fail("VKS_STATE_CONFLICT", "只能更新可编辑知识版本", 409)
        report = self.compare(s, pid, old.id, new.id)
        if report.get("scope_anomaly"):
            fail("VKS_SYNC_SCOPE_ANOMALY", "大量实体缺失，请核对采集范围", 409)
        after = {r.entity_id: r for r in self.service._revisions(s, new.id)}
        historical = [relation_payload(row) for row in old.manifest_json["relations"]]
        ids = {row["id"] for row in historical}
        historical.extend(
            self.service.relation_dto(row)
            for row in s.scalars(
                select(Relation).where(Relation.analysis_set_id == old.analysis_set_id, Relation.excluded == True)
            )
            if row.id not in ids
        )
        changed = False
        for prior in historical:
            before = [s.get(Revision, prior[k]) for k in ("from_revision_id", "to_revision_id")]
            source, target = [after.get(row.entity_id) for row in before]
            source_id, target_id = [
                current.id if current else previous.id for current, previous in zip((source, target), before)
            ]
            if source_id == before[0].id and target_id == before[1].id and source and target:
                continue
            fingerprint = sha256(canonical([old.id, prior["id"], source_id, target_id]).encode()).hexdigest()
            rows = list(
                s.scalars(
                    select(Relation).where(
                        Relation.analysis_set_id == new.id, Relation.relation_type == prior["relation_type"]
                    )
                )
            )
            current = next((r for r in rows if (r.from_revision_id, r.to_revision_id) == (source_id, target_id)), None)
            if not current:
                current = next(
                    (r for r in rows if (r.from_revision_id, r.to_revision_id) == (before[0].id, before[1].id)), None
                )
            if current and fingerprint in (current.metadata_json or {}).get("drift_inputs", []):
                continue
            original = s.get(Relation, prior["id"])
            evidence = self._copy_evidence(s, new, prior["evidence_ids"])
            if not current:
                current = Relation(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=new.id,
                    from_revision_id=source_id,
                    to_revision_id=target_id,
                    relation_type=prior["relation_type"],
                    excluded=prior["excluded"],
                    manual_lock=original.manual_lock if original else False,
                    evidence_ids=evidence,
                    causal_basis="not_established",
                    reason=prior.get("reason", ""),
                    metadata_json=dict(original.metadata_json or {}) if original else {},
                )
                s.add(current)
            else:
                current.from_revision_id, current.to_revision_id = source_id, target_id
                current.evidence_ids = list(dict.fromkeys([*current.evidence_ids, *evidence]))
                if current.causal_basis == "correlated_runtime":
                    current.causal_basis = "not_established"
                current.row_version += 1
            current.validity_status = "stale" if source and target else "broken"
            metadata = dict(current.metadata_json or {})
            metadata["drift_inputs"] = list(dict.fromkeys([*metadata.get("drift_inputs", []), fingerprint]))
            current.metadata_json = metadata
            changed = True
        if changed:
            new.selection_version += 1
            s.flush()
            self.service.audit(
                s, actor_id or new.created_by, pid, "version.detect_drift", new.id, metadata={"from_release_id": old.id}
            )
        report = self.compare(s, pid, old.id, new.id)
        report["to_selection_version"] = new.selection_version
        return report

    def public_report(self, actor, pid, result, session=None):
        if session is None:
            with self.service.db() as s:
                return self.public_report(actor, pid, result, s)
        s = session
        self.service.require(s, actor, pid)
        self.service._selected(s, actor, pid, result["to_analysis_set_id"], None)
        report = self.service._filter_diff(
            s,
            actor,
            pid,
            {
                k: v
                for k, v in result.items()
                if not k.startswith("_")
                and k
                not in (
                    "impact_paths",
                    "proposal_ids",
                    "journey_impacts",
                    "scope_anomaly",
                    "source_vectors",
                    "impact_scope",
                )
            },
        )
        old, new = self._baselines(s, pid, result["from_release_id"], result["to_analysis_set_id"])
        from .reading import ReadingService, chunks

        reading = ReadingService(self.service)
        old_scope = {row["entity_id"]: row["entity_revision_id"] for row in old.manifest_json["selections"]}
        new_scope = {}
        if "_target_revision_ids" in result:
            for batch in chunks(result["_target_revision_ids"]):
                new_scope.update(
                    s.execute(
                        select(Revision.entity_id, Revision.id).where(
                            Revision.project_id == pid, Revision.id.in_(batch)
                        )
                    ).all()
                )
        else:
            new_scope = {row.entity_id: row.id for row in self.service._revisions(s, new.id)}
        visible_old = set(reading.visible_ids(s, actor, pid, old_scope).values())
        visible_new = set(reading.visible_ids(s, actor, pid, new_scope).values())
        visible_revisions = visible_old | visible_new

        def endpoint_visible(row, ids):
            return row["from_revision_id"] in ids and row["to_revision_id"] in ids

        def entity_visible(eid):
            prior, current = old_scope.get(eid), new_scope.get(eid)
            return (
                bool(prior or current)
                and (prior is None or prior in visible_old)
                and (current is None or current in visible_new)
            )

        report["changes"] = [row for row in report["changes"] if entity_visible(row["entity_id"])]
        report["impacted_relations"] = [
            row for row in report["impacted_relations"] if endpoint_visible(row, visible_new)
        ]
        visible_relations = {row["id"] for row in report["impacted_relations"]}
        report["binding_changes"] = []
        for change in result.get("_binding_changes", []):
            if not all(entity_visible(change[key]) for key in ("from_entity_id", "to_entity_id")):
                continue
            endpoints = {}
            for side in ("before", "after"):
                row = change[side]
                if row is None:
                    continue
                if not endpoint_visible(row, visible_old if side == "before" else visible_new):
                    break
                visible = self.service._filter_diff(s, actor, pid, {"impacted_relations": [row]})["impacted_relations"]
                if not visible or set(visible[0]["evidence_ids"]) != set(row["evidence_ids"]):
                    break
                endpoints[side] = visible[0]
            else:
                report["binding_changes"].append({**change, **endpoints})
        # Impact traversal can pass through unchanged relations too. Filter
        # every edge and both baseline revisions before returning a path.
        path_relations = result.get("_path_relations")
        if path_relations is None:
            path_relations = [
                self.service.relation_dto(row)
                for row in s.scalars(select(Relation).where(Relation.analysis_set_id == new.id))
            ]
        for relation in path_relations:
            if (
                self.service._visible_relation(s, actor, pid, relation)
                and endpoint_visible(relation, visible_new)
                and all(
                    (evidence := s.get(Evidence, eid)) and self.service._visible_evidence(s, actor, pid, evidence)
                    for eid in relation["evidence_ids"]
                )
            ):
                visible_relations.add(relation["id"])
        report["impact_paths"] = [
            path
            for path in result.get("impact_paths", [])
            if all(entity_visible(eid) for eid in path["entity_ids"])
            and all(rid in visible_relations for rid in path["relation_ids"])
        ]
        report["journey_impacts"] = [
            item
            for item in result.get("journey_impacts", [])
            if item["journey_revision_id"] in visible_revisions
            and item["binding_revision_id"] in visible_revisions
            and entity_visible(item["journey_id"])
            and entity_visible(item["binding_entity_id"])
        ]
        if self.service._can(s, actor, pid, "source:read") and self.service._can(s, actor, pid, "trace:read"):
            report["scope_anomaly"] = result.get("scope_anomaly", False)
        if "source_vectors" in result:
            report["source_vectors"] = {
                name: self.service._visible_sources(s, actor, pid, result["source_vectors"].get(name, []))
                for name in ("from", "to")
            }
        if "impact_scope" in result:
            original_scope = result["impact_scope"]
            scope = {
                key: original_scope[key]
                for key in ("max_hops", "max_nodes_per_change", "journey_limit", "path_limit")
                if key in original_scope
            }
            complete_visibility = self.service._can(s, actor, pid, "source:read") and self.service._can(
                s, actor, pid, "trace:read"
            )
            scope.update(
                visibility_limited=not complete_visibility,
                returned_path_count=len(report["impact_paths"]),
                returned_journey_count=len(report["journey_impacts"]),
            )
            if complete_visibility:
                scope.update(
                    {
                        key: bool(original_scope.get(key))
                        for key in ("depth_truncated", "nodes_truncated", "paths_truncated", "journey_truncated")
                    }
                )
                scope["is_truncated"] = (
                    bool(original_scope.get("is_truncated"))
                    or len(report["impact_paths"]) < len(result.get("impact_paths", []))
                    or len(report["journey_impacts"]) < len(result.get("journey_impacts", []))
                )
            else:
                scope["is_truncated"] = True
            report["impact_scope"] = scope
            if scope["is_truncated"]:
                report["unknowns"] = [
                    *report.get("unknowns", []),
                    "影响分析受遍历上限或当前可见范围限制，返回数量只计可见结果，不能视为全量影响",
                ]
        return report

    def revalidate(self, actor, rid, payload, version):
        with self.service.db() as s:
            relation = s.get(Relation, rid)
            if not relation:
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            self.service.require(s, actor, relation.project_id, "knowledge:edit")
            if not actor.human:
                fail("VKS_FORBIDDEN", "有效性复核需要人员会话", 403)
            if relation.analysis_set_id != payload["analysis_set_id"]:
                fail("VKS_NOT_FOUND", "未找到当前基线关系", 404)
            if not self.service._visible_relation(s, actor, relation.project_id, relation):
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            if relation.relation_type in PROTOTYPE_RELATIONS:
                from .relationships import RelationshipService

                RelationshipService(self.service)._visible(s, actor, relation)
                fail("VKS_INVALID_INPUT", "请在页面与动作维护中保存业务点及准确出处")
            analysis = s.get(Analysis, relation.analysis_set_id)
            s.refresh(analysis, with_for_update=True)
            if analysis.state == "frozen":
                fail("VKS_STATE_CONFLICT", "已发布关系不可原地复核", 409)
            self.service._version(relation.row_version, version)
            selected = {r.id: r for r in self.service._revisions(s, analysis.id)}
            endpoints = [
                selected.get(identifier) for identifier in (relation.from_revision_id, relation.to_revision_id)
            ]
            if not all(endpoints):
                fail("VKS_INVALID_EVIDENCE", "断链端点尚未恢复，不得以复核清除")
            if not all(self.service._visible_revision(s, actor, analysis.project_id, r) for r in endpoints):
                fail("VKS_NOT_FOUND", "未找到关系", 404)
            snapshots = {row["source_snapshot_id"] for row in analysis.sources}
            evidence = []
            for identifier in payload["evidence_ids"]:
                ev = s.get(Evidence, identifier)
                artifact = s.get(Artifact, ev.artifact_id) if ev else None
                if (
                    not ev
                    or ev.project_id != analysis.project_id
                    or ev.analysis_set_id != analysis.id
                    or not artifact
                    or artifact.snapshot_id not in snapshots
                    or not self.service._visible_evidence(s, actor, analysis.project_id, ev)
                ):
                    fail("VKS_INVALID_EVIDENCE", "复核证据不属于当前基线与可访问来源")
                evidence.append(ev)
            if not {r.artifact_id for r in endpoints if r.artifact_id}.issubset({e.artifact_id for e in evidence}):
                fail("VKS_INVALID_EVIDENCE", "复核证据未覆盖当前两个端点的来源版本")
            relation.evidence_ids = list(dict.fromkeys(payload["evidence_ids"]))
            relation.validity_status = "valid"
            relation.row_version += 1
            # Revalidating evidence preserves the user's independent exclusion choice.
            self.service._verify_relation(s, relation)
            self.service.audit(s, actor, analysis.project_id, "relation.revalidate", relation.id)
            s.flush()
            return self.service.relation_dto(relation)


class RevalidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)


def install_versions(app, service, actor, envelope, API="/api/v1"):
    versions = VersionService(service)

    @app.post(API + "/relations/{rid}/revalidate")
    def revalidate(rid: str, payload: RevalidateInput, request: Request, a=Depends(actor), if_match: str = Header(...)):
        return envelope(request, versions.revalidate(a, rid, payload.model_dump(), if_match))
