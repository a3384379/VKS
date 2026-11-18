"""Fresh, lightweight fences for an already prepared immutable corpus.

Authorization is checked in the current transaction. SQL aggregation avoids
materializing thousands of unchanged revision/identity objects for each fence.
A changed invariant always falls back to the authoritative full preparation.
"""

from collections import OrderedDict
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import Text, cast, func, or_, select

from .ai_gateway import ModelPolicy
from .knowledge import SourceSnapshot
from .materials import StudioWorkspace
from .model_connections import ModelConnection
from .models import Artifact, Entity, Revision, Selection
from .service import canonical, sha


@dataclass
class Prepared:
    state: tuple
    artifact_ids: tuple
    snapshot_ids: tuple
    revision_ids: tuple
    fingerprint: str
    key_fields: dict | None = None


def aggregate(s, query):
    rows = query.subquery()
    values = [rows.c[name] for name in rows.c.keys()]
    if s.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import aggregate_order_by

        value = func.json_agg(aggregate_order_by(func.json_build_array(*values), values[0]))
    else:
        value = func.json_group_array(func.json_array(*values))
    return s.scalar(select(cast(value, Text))) or "[]"


def policy_row(s, pid):
    scoped = s.info.get("vks_read_batch") or s.info.get("vks_completion_batch")
    cached = s.info.setdefault("vks_model_policies", {}) if scoped else {}
    if pid not in cached:
        cached[pid] = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
    return cached[pid]


def identity(actor, pid, form, key):
    return (
        actor,
        pid,
        form.analysis_set_id,
        form.release_id,
        form.package_id,
        key,
        canonical(form.model_dump(exclude={"mode", "refresh"})),
    )


def fingerprint(assistant, s, actor, pid, form, sources):
    from .inference_pipeline import READ_SESSION, WRITE_SESSION

    writer = WRITE_SESSION.get() or READ_SESSION.get()
    batch = s.info.get("vks_completion_batch") or s.info.get("vks_read_batch")
    if writer is None or writer != (assistant.service, s) or not batch or s.bind.dialect.name != "sqlite":
        return _fingerprint(assistant, s, actor, pid, form, sources)
    # SQLite's outer BEGIN IMMEDIATE excludes concurrent mutation. These batches
    # write only derived points, budget entries and progress, never corpus or
    # grants. RAG settings live in a separate DB and are still read each time.
    settings = canonical(assistant.rag.store.settings(pid).model_dump())
    key = (actor, pid, form.analysis_set_id, form.release_id, sources, settings)
    cache = s.info.setdefault("vks_batch_fingerprints", {})
    result = cache.get(key)
    if result is None:
        result = _fingerprint(assistant, s, actor, pid, form, sources)
        if result is not None:
            cache[key] = result
    if result is None:
        return None
    analysis, _release = assistant.links.reader.reading.baseline(s, actor, pid, form.analysis_set_id, None)
    return result[0], analysis.selection_version, result[2]


def _fingerprint(assistant, s, actor, pid, form, sources):
    if not form.analysis_set_id or form.release_id:
        return None
    analysis, release = assistant.links.reader.reading.baseline(s, actor, pid, form.analysis_set_id, None)
    # These checks read current grants/credential state in this transaction.
    if not assistant.service._can(s, actor, pid, "source:read") or not assistant.service._can(
        s, actor, pid, "trace:read"
    ):
        return None
    editable = analysis.state != "frozen" and assistant.service._can(s, actor, pid, "knowledge:edit")
    from .input_versions import cached_projection

    source_facts = cached_projection(
        assistant.service, s, ("corpus", pid, analysis.id, sources),
        lambda: _source_facts(s, analysis.id, sources),
    )
    settings = assistant.rag.store.settings(pid).model_dump()
    row = policy_row(s, pid)
    policy = row.settings if row else None
    connection = (
        s.get(ModelConnection, settings.get("connection_id"))
        if settings.get("connection_id")
        else None
    )
    connection = connection.settings if connection and connection.project_id == pid else None
    workspace = s.get(StudioWorkspace, pid)
    workspace = workspace.analysis_set_id if workspace else None
    facts = sha(
        canonical(
            [
                analysis.id,
                analysis.state,
                workspace,
                *source_facts,
                policy,
                settings,
                connection,
                editable,
            ]
        )
    )
    return facts, analysis.selection_version, editable


def _source_facts(s, analysis_id, sources):
    selected = aggregate(
        s,
        select(
            Entity.id.label("id"),
            Entity.project_id,
            Entity.type,
            Entity.stable_key,
            Revision.id.label("revision"),
            Revision.entity_id,
            Revision.project_id.label("revision_project"),
            Revision.artifact_id,
            Revision.content_sha256,
            Revision.semantic_content_sha256,
        )
        .join(Selection, Selection.entity_id == Entity.id)
        .join(Revision, Revision.id == Selection.entity_revision_id)
        .where(Selection.analysis_set_id == analysis_id, Entity.type.not_in(("action", "business_point")))
        .order_by(Entity.id),
    )
    artifact_ids, snapshot_ids, revision_ids = sources
    artifacts = aggregate(
        s,
        select(
            Artifact.id.label("id"),
            Artifact.project_id,
            Artifact.snapshot_id,
            Artifact.kind,
            Artifact.sensitivity,
            Artifact.relative_path,
            Artifact.sha256,
            Artifact.size_bytes,
            Artifact.object_key,
            cast(Artifact.metadata_json, Text).label("metadata"),
        )
        .where(
            or_(
                Artifact.id.in_(artifact_ids),
                Artifact.snapshot_id.in_(snapshot_ids) & ~Artifact.relative_path.startswith("prototype-points/"),
            ),
        )
        .order_by(Artifact.id),
    )
    snapshots = aggregate(
        s,
        select(
            SourceSnapshot.id.label("id"),
            SourceSnapshot.project_id,
            SourceSnapshot.source_key,
            SourceSnapshot.provider_revision,
            SourceSnapshot.content_sha256,
            SourceSnapshot.role,
        )
        .where(SourceSnapshot.id.in_(snapshot_ids))
        .order_by(SourceSnapshot.id),
    )
    provenance = aggregate(
        s,
        select(
            Revision.id.label("id"),
            Revision.project_id,
            Revision.entity_id,
            Revision.artifact_id,
            Revision.content_sha256,
            cast(Revision.metadata_json, Text).label("metadata"),
            Entity.project_id.label("entity_project"),
            Entity.type,
            Entity.stable_key,
        )
        .join(Entity, Entity.id == Revision.entity_id)
        .where(Revision.id.in_(revision_ids))
        .order_by(Revision.id),
    )
    return sha(selected), sha(artifacts), sha(snapshots), sha(provenance)


def source_artifacts(s):
    artifacts = {key[1]: row for key, row in s.info.get("vks_prefetched", {}).items() if key[0] is Artifact}
    for row in s.identity_map.values():
        if isinstance(row, Artifact):
            artifacts[row.id] = row
    return artifacts


def requires_full_preparation(s):
    # Evidence membership, scenario capture sets and generated-document legacy
    # detection consult facts beyond the simple artifact/revision invariant.
    # Keep their authoritative policy traversal instead of reusing a template.
    for artifact in source_artifacts(s).values():
        metadata = artifact.metadata_json or {}
        if (
            "input_provenance" in metadata
            or "input_revision_provenance" in metadata
            or "journey_binding_revisions" in metadata
            or "journey_capture_artifacts" in metadata
            or artifact.relative_path.startswith(("journeys/", "task-reports/", "documents/generated-"))
        ):
            return True
    return False


def sources_for(s):
    artifacts = source_artifacts(s)
    ids, snapshots, revisions = set(artifacts), {a.snapshot_id for a in artifacts.values()}, set()
    for artifact in artifacts.values():
        metadata = artifact.metadata_json or {}
        original = metadata.get("original_artifact_id")
        if isinstance(original, str):
            ids.add(original)
        for name in (
            "prototype_input_artifact_ids",
            "prototype_surface_artifact_ids",
            "journey_input_artifacts",
            "journey_capture_artifacts",
        ):
            values = metadata.get(name)
            if isinstance(values, list):
                ids.update(v for v in values if isinstance(v, str))
        for name in ("input_provenance", "input_revision_provenance"):
            values = metadata.get(name)
            if isinstance(values, list):
                ids.update(
                    v["artifact_id"] for v in values if isinstance(v, dict) and isinstance(v.get("artifact_id"), str)
                )
                revisions.update(
                    v["revision_id"] for v in values if isinstance(v, dict) and isinstance(v.get("revision_id"), str)
                )
        values = metadata.get("journey_binding_revisions")
        if isinstance(values, list):
            revisions.update(v for v in values if isinstance(v, str))
    return tuple(sorted(ids)), tuple(sorted(snapshots)), tuple(sorted(revisions))


def remember(assistant, s, actor, pid, form, state, *, key_fields=None):
    if not form.analysis_set_id or form.release_id or requires_full_preparation(s):
        return
    sources = sources_for(s)
    result = fingerprint(assistant, s, actor, pid, form, sources)
    if result is None:
        return
    with assistant.lock:
        cache = getattr(assistant, "prepared_invariants", None)
        if cache is None:
            assistant.prepared_invariants = cache = OrderedDict()
            assistant.prepared_texts = OrderedDict()
        texts = assistant.prepared_texts
        if state[2] not in texts:
            texts[state[2]] = state[1]
        texts.move_to_end(state[2])
        while len(texts) > 4:
            texts.popitem(last=False)
        pooled = (state[0], texts[state[2]], *state[2:])
        prepared = Prepared(pooled, *sources, result[0], deepcopy(key_fields))
        cache[identity(actor, pid, form, state[3])] = prepared
        cache.move_to_end(identity(actor, pid, form, state[3]))
        if key_fields is not None and state[5]:
            templates = getattr(assistant, "prepared_templates", None)
            if templates is None:
                assistant.prepared_templates = templates = OrderedDict()
            scope = (actor, pid, form.analysis_set_id, form.package_id)
            templates[scope] = prepared
            templates.move_to_end(scope)
            while len(templates) > 8:
                templates.popitem(last=False)
        while len(cache) > 512:
            cache.popitem(last=False)


def validated(assistant, actor, pid, form, key, *, session=None):
    with assistant.lock:
        cache = getattr(assistant, "prepared_invariants", {})
        prepared = cache.get(identity(actor, pid, form, key))
    if prepared is None:
        return None
    with nullcontext(session) if session is not None else assistant.links.reader.reading.db() as s:
        result = fingerprint(
            assistant, s, actor, pid, form, (prepared.artifact_ids, prepared.snapshot_ids, prepared.revision_ids)
        )
        if result is None or result[0] != prepared.fingerprint:
            return None
        return (*prepared.state[:4], result[1], result[2])


def template_state(assistant, s, actor, pid, form, generation_contract):
    """Internal batch-only reuse after a fresh check of all shared input facts."""
    from .association_inference import CURRENT_BATCH

    batch = CURRENT_BATCH.get()
    if batch is None or batch.actor != actor or batch.pid != pid or not form.analysis_set_id or form.release_id:
        return None
    scope = (actor, pid, form.analysis_set_id, form.package_id)
    with assistant.lock:
        prepared = getattr(assistant, "prepared_templates", {}).get(scope)
    if prepared is None or prepared.key_fields["generation_contract"] != generation_contract:
        return None
    # Never hold an assistant/template lock while opening a DB read or write.
    result = fingerprint(
        assistant, s, actor, pid, form, (prepared.artifact_ids, prepared.snapshot_ids, prepared.revision_ids)
    )
    if result is None or not result[2] or result[0] != prepared.fingerprint:
        return None
    fields = deepcopy(prepared.key_fields)
    fields["target"] = form.target.model_dump(exclude={"open_steps"})
    fields["context"] = form.context.model_dump()
    key = sha(canonical(fields))
    state = (fields["config"], prepared.state[1], prepared.state[2], key, result[1], result[2])
    record = Prepared(
        state, prepared.artifact_ids, prepared.snapshot_ids, prepared.revision_ids, prepared.fingerprint, fields
    )
    with assistant.lock:
        cache = assistant.prepared_invariants
        cache[identity(actor, pid, form, key)] = record
        cache.move_to_end(identity(actor, pid, form, key))
        while len(cache) > 512:
            cache.popitem(last=False)
    return state
