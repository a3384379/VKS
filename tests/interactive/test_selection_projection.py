"""SQLite JSON projection keeps the exact fresh live map and frozen manifest."""

from types import SimpleNamespace

from sqlalchemy import delete, select
from vks_api.models import Entity, Revision, Selection
from vks_api.reading import ReadingService
from vks_api.service import sha

from tests.interactive import test_materials

materials = test_materials.materials


def workspace(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    test_materials.install(service, owner, manager, pid, aid)
    return service, owner, pid, aid


def original(s, aid):
    return dict(
        s.execute(
            select(Selection.entity_id, Selection.entity_revision_id).where(Selection.analysis_set_id == aid)
        ).all()
    )


def test_json_selection_map_matches_scalar_rows_including_escaped_ids(materials):
    service, _owner, pid, aid = workspace(materials)
    eid, rid = 'entity-"\\汉字', 'revision-"\\汉字'
    with service.db() as s:
        s.add(Entity(id=eid, project_id=pid, type="action", stable_key="escaped-selection"))
        s.flush()
        s.add(
            Revision(
                id=rid,
                entity_id=eid,
                project_id=pid,
                artifact_id=None,
                name="Escaped",
                content="",
                content_sha256=sha(""),
                semantic_content_sha256=sha(""),
                locator={},
                metadata_json={},
            )
        )
        s.flush()
        s.add(Selection(analysis_set_id=aid, entity_id=eid, entity_revision_id=rid))
    reader = ReadingService(service)
    with reader.db() as s:
        expected = original(s, aid)
        actual, index = reader.selections(s, SimpleNamespace(id=aid), None)
        assert actual == expected and actual[eid] == rid and index is None


def test_live_selection_projection_observes_writes_deletes_and_rollback(materials):
    service, _owner, _pid, aid = workspace(materials)
    reader, analysis = ReadingService(service), SimpleNamespace(id=aid)
    with reader.db() as s:
        before, _ = reader.selections(s, analysis, None)
    with service.db() as s:
        first, second = list(before)[:2]
        selected = s.get(Selection, (aid, first))
        selected.entity_revision_id = before[second]
        current, _ = reader.selections(s, analysis, None)
        assert current == original(s, aid) and current[first] == before[second]
        s.delete(selected)
        removed, _ = reader.selections(s, analysis, None)
        assert removed == original(s, aid) and first not in removed
        s.rollback()
    with reader.db() as s:
        assert reader.selections(s, analysis, None) == (before, None)


def test_missing_and_empty_analysis_return_empty_maps(materials):
    _web, service, _owner, _manager, projects = materials
    reader = ReadingService(service)
    with reader.db() as s:
        assert reader.selections(s, None, None) == ({}, None)
        assert reader.selections(s, SimpleNamespace(id=projects[0][1]), None) == ({}, None)
        assert reader.selections(s, SimpleNamespace(id="not-present"), None) == ({}, None)


def test_release_selection_uses_frozen_manifest_after_live_map_changes(materials):
    service, owner, pid, aid = workspace(materials)
    reader = ReadingService(service)
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "selection-manifest")
    with reader.db() as s:
        analysis, frozen = reader.baseline(s, owner, pid, release_id=release["id"])
        expected, index = reader.selections(s, analysis, frozen)
        assert expected and index is not None
    with service.db() as s:
        s.execute(delete(Selection).where(Selection.analysis_set_id == aid))
    with reader.db() as s:
        analysis, frozen = reader.baseline(s, owner, pid, release_id=release["id"])
        actual, index = reader.selections(s, analysis, frozen)
        assert actual == expected and index is not None
        assert reader.selections(s, analysis, None) == ({}, None)
