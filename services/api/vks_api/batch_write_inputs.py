"""Fetch ready write dependencies together inside the current SQLite snapshot."""

from sqlalchemy import or_, select

from .models import Analysis, Entity, Evidence, Idempotency, Relation, Revision
from .reading import ReadingService


def prefetch(service, session, completions, *, action_seeds=None):
    from .ai_gateway import ModelLedger, ModelUsage

    usage_ids = {item.usage_id for item in completions if item.usage_id}
    if usage_ids:
        usages = list(session.scalars(select(ModelUsage).where(ModelUsage.id.in_(usage_ids))))
        list(session.scalars(select(ModelLedger).where(ModelLedger.id.in_({row.ledger_id for row in usages}))))
    wanted = {rid for item in completions for rid in item.revision_ids}
    if not wanted:
        return
    reader = ReadingService(service)
    aids = {item.analysis_set_id for item in completions if item.analysis_set_id}
    if session.bind.dialect.name != "sqlite" or not aids:
        reader.revision_rows(session, wanted)
        return
    from .interactive_targets import selected_roots
    from .service import canonical, sha

    # All identities and replay keys for ready targets are looked up together,
    # including explicit negative entries for targets which have no row yet.
    identities = session.info.setdefault("vks_ready_entities", {})
    for aid in aids:
        analysis = session.get(Analysis, aid)
        choices, _ = reader.selections(session, analysis, None)
        bindings = [item.binding for item in completions if item.analysis_set_id == aid and item.binding]
        for pid in {binding[0] for binding in bindings}:
            roots = selected_roots(service, session, pid, choices)
            packages = {row.artifact_id: row.entity_id for row in roots}
            keys = set()
            for _pid, package, target in bindings:
                if _pid != pid:
                    continue
                signature = target.model_dump(exclude={"open_steps"})
                key = (
                    "interactive."
                    + sha(canonical({"package": packages.get(package, package), "target": signature}))[:40]
                )
                keys.add(("action", key))
            for kind, key in keys:
                identities[(pid, kind, key)] = None
            for row in (
                session.scalars(
                    select(Entity).where(
                        Entity.project_id == pid,
                        Entity.type == "action",
                        Entity.stable_key.in_([key for _kind, key in keys]),
                    )
                )
                if keys
                else ()
            ):
                identities[(pid, row.type, row.stable_key)] = row
    replay = session.info.setdefault("vks_ready_idempotency", {})
    replay_keys = {"assistant." + key for key in usage_ids}
    for usage in usages if usage_ids else ():
        replay[(usage.project_id, usage.actor_id, "interactive.binding.save", "assistant." + usage.id)] = None
    for row in (
        session.scalars(
            select(Idempotency).where(
                Idempotency.operation == "interactive.binding.save",
                Idempotency.key.in_(replay_keys),
            )
        )
        if replay_keys
        else ()
    ):
        replay[(row.project_id, row.actor_id, row.operation, row.key)] = row
    # Discover graph endpoints without separately hydrating each frontier and
    # its artifact/provenance closure. Hydrate their union together below.
    kinds = (
        dict.fromkeys(action_seeds, "action")
        if action_seeds is not None
        else dict(
            session.execute(
                select(Revision.id, Entity.type)
                .join(Entity, Entity.id == Revision.entity_id)
                .where(Revision.id.in_(wanted))
            ).all()
        )
    )
    points = {rid for rid, kind in kinds.items() if kind == "business_point"}
    actions = {rid for rid, kind in kinds.items() if kind == "action"}
    seeds = points | actions
    bindings = (
        select(Relation.from_revision_id.label("action"), Relation.to_revision_id.label("point"))
        .where(
            Relation.analysis_set_id.in_(aids),
            Relation.relation_type == "has_business_point",
            or_(Relation.from_revision_id.in_(actions), Relation.to_revision_id.in_(points)),
        )
        .cte("ready_bindings")
    )
    edges = (
        list(
            session.scalars(
                select(Relation).where(
                    Relation.analysis_set_id.in_(aids),
                    or_(
                        Relation.from_revision_id.in_(seeds),
                        Relation.to_revision_id.in_(seeds),
                        Relation.from_revision_id.in_(select(bindings.c.point)),
                        Relation.to_revision_id.in_(select(bindings.c.point)),
                        Relation.from_revision_id.in_(select(bindings.c.action)),
                        Relation.to_revision_id.in_(select(bindings.c.action)),
                    ),
                )
            )
        )
        if seeds
        else []
    )
    connected = [
        edge
        for edge in edges
        if edge.relation_type == "has_business_point"
        and (edge.from_revision_id in actions or edge.to_revision_id in points)
    ]
    points |= {edge.to_revision_id for edge in connected}
    actions |= {edge.from_revision_id for edge in connected}
    cache = session.info.setdefault("vks_ready_edges", {})
    for aid in aids:
        for rid in points | actions:
            cache[(aid, rid)] = [
                edge
                for edge in edges
                if edge.analysis_set_id == aid and rid in (edge.from_revision_id, edge.to_revision_id)
            ]
    related = {rid for edge in edges for rid in (edge.from_revision_id, edge.to_revision_id)}
    rows = reader.revision_rows(session, wanted | related)
    # Citations in one ready batch may span many documents. Resolve all of
    # their selected document revisions together, then validate the complete
    # chapter maps once in this snapshot, rather than one query per document.
    document_keys = {}
    for row in rows:
        if (key := row.metadata_json.get("document_key")) is not None:
            document_keys.setdefault(row.project_id, set()).add(key)
    if document_keys:
        from .documents import DocumentService
        from .interactive import InteractiveKnowledgeService

        document_ids = set()
        for aid in aids:
            analysis = session.get(Analysis, aid)
            if analysis and (keys := document_keys.get(analysis.project_id)):
                choices, _ = reader.selections(session, analysis, None)
                document_ids.update(
                    InteractiveKnowledgeService._document_selections(session, analysis.project_id, choices, keys)
                )
        documents = reader.revision_rows(session, document_ids)
        DocumentService.prefetch_section_members(
            session, [(session.get(Entity, row.entity_id), row) for row in documents]
        )
    evidence_ids = {eid for edge in edges for eid in edge.evidence_ids}
    keys = {"prototype." + rid for rid in wanted | related}
    evidence = list(
        session.scalars(
            select(Evidence).where(
                Evidence.analysis_set_id.in_(aids),
                or_(Evidence.stable_key.in_(keys), Evidence.id.in_(evidence_ids)),
            )
        )
    )
    retained = session.info.setdefault("vks_prefetched", {})
    retained.update({(Evidence, row.id): row for row in evidence})
    reader.preload(session, evidence=evidence)
    cached = session.info.setdefault("vks_evidence_rows", {})
    for row in evidence:
        cached[(row.analysis_set_id, row.stable_key, row.artifact_id, row.content_sha256)] = row
    session.info.setdefault("vks_evidence_queried", set()).update((aid, key) for aid in aids for key in keys)

    from .models import Artifact

    session.info.setdefault("vks_known_artifacts", {}).update(
        {
            (row.project_id, row.snapshot_id, row.relative_path): row
            for row in session.identity_map.values()
            if isinstance(row, Artifact)
        }
    )
    session.info.setdefault("vks_known_revisions", {}).update(
        {(row.entity_id, row.content_sha256): row for row in session.identity_map.values() if isinstance(row, Revision)}
    )


def prefetch_targets(service, session, targets):
    from types import SimpleNamespace

    from .interactive_targets import selected_targets

    groups = {}
    for pid, aid, package, target in targets:
        if aid and package and target:
            groups.setdefault((pid, aid), []).append((package, target))
    completions = []
    reader = ReadingService(service)
    for (pid, aid), bindings in groups.items():
        analysis = session.get(Analysis, aid)
        if not analysis or analysis.project_id != pid:
            continue
        choices, _ = reader.selections(session, analysis, None)
        revisions = set()
        for package, target in bindings:
            revisions.update(
                selected_targets(service, session, pid, choices, package, target, type(target), normalize=True)
            )
        completions.append(SimpleNamespace(revision_ids=revisions, analysis_set_id=aid, usage_id=None, binding=None))
    prefetch(
        service,
        session,
        completions,
        action_seeds={rid for item in completions for rid in item.revision_ids},
    )
