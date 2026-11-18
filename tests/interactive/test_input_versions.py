"""Source caches invalidate on raw SQL, other processes and rolled-back writes."""

import pytest
from sqlalchemy import text
from vks_api import corpus_validation
from vks_api.bulk_association import composition
from vks_api.models import Analysis, Entity, Revision, Selection, uid
from vks_api.service import Service

from tests.interactive import test_corpus_validation as fixtures

workspace = fixtures.workspace
interactive = fixtures.interactive
assistant = fixtures.assistant
setup = fixtures.setup


def test_generated_progress_reuses_source_facts_but_not_permissions(assistant, monkeypatch):
    engine, service, owner, pid, aid, _package, _section, form, state = setup(assistant)
    calls = []
    original = corpus_validation._source_facts

    def measured(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(corpus_validation, "_source_facts", measured)
    for _ in range(3):
        assert corpus_validation.validated(engine, owner, pid, form, state[3]) == state
    assert not calls
    with service.db() as s:
        eid, rid = uid(), uid()
        s.add(Entity(id=eid, project_id=pid, type="business_point", stable_key="generated.cache-check"))
        s.flush()
        s.add(
            Revision(
                id=rid,
                entity_id=eid,
                project_id=pid,
                name="Generated",
                content="text",
                content_sha256="a" * 64,
                semantic_content_sha256="b" * 64,
            )
        )
        s.flush()
        s.add(Selection(analysis_set_id=aid, entity_id=eid, entity_revision_id=rid))
        s.get(Analysis, aid).selection_version += 1
    latest = corpus_validation.validated(engine, owner, pid, form, state[3])
    assert latest[4] == state[4] + 1 and latest[3] == state[3] and not calls
    with service.db() as s:
        s.execute(text("UPDATE user_identity SET status='disabled' WHERE id=:id"), {"id": owner.id})
    from vks_api.service import DomainError

    with pytest.raises(DomainError):
        corpus_validation.validated(engine, owner, pid, form, state[3])


def test_other_service_raw_selection_change_invalidates_composition_and_fingerprint(assistant):
    engine, service, owner, pid, aid, _package, section, form, state = setup(assistant)
    with service.db() as s:
        before = composition(s, pid, aid)
    other = Service(service.data_dir)
    try:
        with other.engine.begin() as c:
            c.execute(
                text("DELETE FROM analysis_set_entity WHERE analysis_set_id=:aid AND entity_revision_id=:rid"),
                {"aid": aid, "rid": section["entity_revision_id"]},
            )
        with service.db() as s:
            assert composition(s, pid, aid) != before
        assert corpus_validation.validated(engine, owner, pid, form, state[3]) is None
    finally:
        other.engine.dispose()


def test_other_service_root_identity_change_invalidates_the_cached_package_list(assistant):
    from vks_api.interactive_targets import selected_roots
    from vks_api.materials import ROOT
    from vks_api.reading import ReadingService

    _engine, service, _owner, pid, aid, _package, _section, _form, _state = setup(assistant)
    reader = ReadingService(service)
    with service.db() as s:
        eid, rid = uid(), uid()
        s.add(Entity(id=eid, project_id=pid, type="page", stable_key="material.cache-test"))
        s.flush()
        s.add(
            Revision(
                id=rid,
                project_id=pid,
                entity_id=eid,
                name="Synthetic root",
                content="root",
                content_sha256="a" * 64,
                semantic_content_sha256="b" * 64,
                metadata_json={ROOT: {}},
            )
        )
        s.flush()
        s.add(Selection(analysis_set_id=aid, entity_id=eid, entity_revision_id=rid))
    with service.db() as s:
        choices, _ = reader.selections(s, s.get(Analysis, aid), None)
        before = selected_roots(service, s, pid, choices)
        assert before
        removed = before[0].entity_id
    other = Service(service.data_dir)
    try:
        with other.engine.begin() as connection:
            connection.execute(text("UPDATE entity SET type='action' WHERE id=:id"), {"id": removed})
        with service.db() as s:
            choices, _ = reader.selections(s, s.get(Analysis, aid), None)
            assert removed not in {row.entity_id for row in selected_roots(service, s, pid, choices)}
    finally:
        other.engine.dispose()


def test_rollback_cannot_reuse_a_speculative_projection_key(assistant):
    engine, service, owner, pid, aid, _package, section, form, state = setup(assistant)
    rid = section["entity_revision_id"]
    with service.db() as s:
        original = composition(s, pid, aid)
        token = s.scalar(text("SELECT token FROM input_epoch WHERE id=1"))
    with pytest.raises(RuntimeError), service.db() as s:
        s.execute(text("UPDATE entity_revision SET content_sha256=:h WHERE id=:id"), {"h": "c" * 64, "id": rid})
        speculative_token = s.scalar(text("SELECT token FROM input_epoch WHERE id=1"))
        speculative = composition(s, pid, aid)
        assert speculative != original
        raise RuntimeError("rollback")
    with service.db() as s:
        assert s.scalar(text("SELECT token FROM input_epoch WHERE id=1")) == token
        assert composition(s, pid, aid) == original
        s.execute(text("UPDATE entity_revision SET content_sha256=:h WHERE id=:id"), {"h": "d" * 64, "id": rid})
        assert s.scalar(text("SELECT token FROM input_epoch WHERE id=1")) != speculative_token
        assert composition(s, pid, aid) not in {original, speculative}
    assert corpus_validation.validated(engine, owner, pid, form, state[3]) is None


def test_completion_prefetches_ready_dependencies_and_keeps_individual_rejections(assistant):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import event
    from vks_api.inference_pipeline import InferencePipeline

    _engine, service, _owner, _pid, _aid, _package, section, _form, _state = setup(assistant)
    rid = section["entity_revision_id"]
    statements = []

    def counted(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT entity_revision."):
            statements.append(statement)

    event.listen(service.engine, "before_cursor_execute", counted)

    def read(i):
        with service.db() as s:
            row = s.get(Revision, rid)
            assert row is not None
            if i == 2:
                raise ValueError("individual rejection")
            return row.content_sha256

    with InferencePipeline(8, service=service) as pipeline, ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(pipeline.complete, lambda i=i: read(i), revision_ids=[rid]) for i in range(8)]
        for i, task in enumerate(tasks):
            if i == 2:
                with pytest.raises(ValueError, match="individual rejection"):
                    task.result(timeout=5)
            else:
                assert task.result(timeout=5)
    # Fresh source rows are loaded as one group, never eight per-item SELECTs.
    assert len(statements) < 8
