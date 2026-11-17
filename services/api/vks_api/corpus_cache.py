"""Bounded reuse of immutable revision text, never of authorization decisions."""

from collections import OrderedDict
from types import SimpleNamespace

from sqlalchemy import select

from .models import Artifact, Entity, Revision, Selection
from .immutable_facts import revision_fact


def corpus_rows(assistant, s, actor, pid, choices, *, analysis_id=None):
    """Refresh selected identities and artifact policies in every transaction.

    Only revision facts enter the shared cache. Current entities, project grants,
    source/trace permissions and artifacts are read again before filtering. The
    selected IDs include document mappings, so removed/replaced chapters never
    survive a corpus switch. No mutable binding or permission result is cached.
    """
    from .reading import chunks

    complete = assistant.service._can(s, actor, pid, "source:read") and assistant.service._can(
        s, actor, pid, "trace:read"
    )
    selected = []
    # These identities are refreshed on every preparation. Fully authorized
    # corpus readers only need current ownership, type and stable keys; avoid
    # materializing thousands of ORM instances merely to validate those facts.
    if analysis_id:
        conditions = (
            Selection.analysis_set_id == analysis_id,
            Entity.project_id == pid,
            Entity.type.in_(("document", "section")),
        )
        if complete:
            selected.extend(
                (SimpleNamespace(id=eid, project_id=project, type=kind, stable_key=stable), rid)
                for eid, project, kind, stable, rid in s.execute(
                    select(Entity.id, Entity.project_id, Entity.type, Entity.stable_key, Selection.entity_revision_id)
                    .join(Selection, Selection.entity_id == Entity.id)
                    .where(*conditions)
                )
                if choices.get(eid) == rid
            )
        else:
            selected.extend(
                (entity, rid)
                for entity, rid in s.execute(
                    select(Entity, Selection.entity_revision_id)
                    .join(Selection, Selection.entity_id == Entity.id)
                    .where(*conditions)
                )
                if choices.get(entity.id) == rid
            )
    else:
        for batch in chunks(choices):
            conditions = (Entity.id.in_(batch), Entity.project_id == pid, Entity.type.in_(("document", "section")))
            if complete:
                selected.extend(
                    (SimpleNamespace(id=eid, project_id=project, type=kind, stable_key=stable), choices[eid])
                    for eid, project, kind, stable in s.execute(
                        select(Entity.id, Entity.project_id, Entity.type, Entity.stable_key).where(*conditions)
                    )
                )
            else:
                selected.extend((entity, choices[entity.id]) for entity in s.scalars(select(Entity).where(*conditions)))
    key = (pid, frozenset((entity.id, rid) for entity, rid in selected))
    with assistant.lock:
        cache = getattr(assistant, "corpora", None)
        if cache is None:
            assistant.corpora = cache = OrderedDict()
        revisions = cache.get(key)
        if revisions is None:
            revisions = {}
            for batch in chunks([rid for _, rid in selected]):
                for row in s.scalars(select(Revision).where(Revision.id.in_(batch), Revision.project_id == pid)):
                    revisions[row.id] = revision_fact(row)
            cache[key] = revisions
            while len(cache) > 2:
                cache.popitem(last=False)
        cache.move_to_end(key)
    retained = s.info.setdefault("vks_prefetched", {})
    retained.update({(Entity, entity.id): entity for entity, _ in selected})
    retained.update({(Revision, row.id): row for row in revisions.values()})
    # Artifacts and their source dependencies carry mutable visibility. Always
    # fetch them in this transaction, even when all text came from the cache.
    assistant.links.reader.reading.preload(s, revisions.values())
    result = []
    for entity, rid in selected:
        row = revisions.get(rid)
        art = retained.get((Artifact, row.artifact_id)) if row and row.artifact_id else None
        if not row or row.entity_id != entity.id or row.project_id != pid:
            continue
        if row.artifact_id and (not art or art.project_id != pid):
            continue
        if complete or assistant.service._visible_revision(s, actor, pid, row):
            result.append((entity, row))
    return result


def corpus_projection(assistant, s, rows, policy_for, *, for_model, allow_source):
    """Reuse mapped chapter facts only after current identities and policies agree."""
    from .service import canonical, sha

    # rows has already passed this transaction's exact selection, ownership and
    # current reader authorization checks. Include current document stable keys
    # and entity types because those participate in the authoritative mapping.
    identity = tuple((entity.id, row.id, entity.type, entity.stable_key) for entity, row in rows)
    with assistant.lock:
        cache = getattr(assistant, "corpus_mappings", None)
        if cache is None:
            assistant.corpus_mappings = cache = OrderedDict()
        facts = cache.get(identity)
        if facts is None:
            facts = []
            sections = {r.id for e, r in rows if e.type == "section"}
            for entity, document in rows:
                if entity.type != "document":
                    continue
                for chapter in assistant.links.documents._sections(s, entity, document):
                    if chapter.id in sections:
                        facts.append(
                            (
                                {
                                    "revision_id": chapter.id,
                                    "name": chapter.name,
                                    "content": chapter.content,
                                    "document_revision_id": document.id,
                                    "document_name": document.name,
                                },
                                chapter.content_sha256,
                                chapter.artifact_id,
                            )
                        )
            cache[identity] = facts
            while len(cache) > 2:
                cache.popitem(last=False)
        cache.move_to_end(identity)
    policies = {}
    for _chapter, _sha, aid in facts:
        if aid not in policies:
            artifact = s.info["vks_prefetched"].get((Artifact, aid)) or s.get(Artifact, aid)
            policies[aid] = (policy_for(artifact), artifact.sensitivity)
    # Current policies and current model eligibility are re-evaluated before a
    # derived fact projection is reused. Permission decisions are never looked
    # up from this cache, and no previous reader's allowed IDs are substituted.
    policy_key = sha(canonical(policies))
    projection_key = (identity, policy_key, for_model, allow_source)
    with assistant.lock:
        cache = getattr(assistant, "corpus_projections", None)
        if cache is None:
            assistant.corpus_projections = cache = OrderedDict()
        found = cache.get(projection_key)
        if found is None:
            chapters, revisions = [], {}
            for chapter, digest, aid in facts:
                policy, sensitivity = policies[aid]
                if for_model and (
                    sensitivity == "confidential" or policy.get("requires_source_read") and not allow_source
                ):
                    continue
                chapters.append(chapter)
                revisions[chapter["revision_id"]] = {
                    "sha": digest,
                    "artifact": aid,
                    "policy": policy,
                    "document": chapter["document_revision_id"],
                }
            found = chapters, sha(canonical(revisions))
            cache[projection_key] = found
            while len(cache) > 4:
                cache.popitem(last=False)
        cache.move_to_end(projection_key)
    return [dict(chapter) for chapter in found[0]], found[1]
