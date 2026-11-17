"""Target reads keep exact citations, legacy anchors and transaction-bound policies."""

from copy import deepcopy

import pytest

from sqlalchemy import event, select
from vks_api import reading
from vks_api.documents import DocumentService
from vks_api.interactive import DOMTarget, InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import Analysis, Artifact, Entity, Relation, Revision, Selection
from vks_api.service import Actor

from tests.interactive import test_knowledge, test_materials
from tests.interactive.test_knowledge import form

workspace = test_knowledge.workspace
interactive = test_knowledge.interactive
materials = test_materials.materials


def save(links, owner, pid, aid, package, section, *, selector="#add", label="Add"):
    body = form(aid, package, section)
    body["target"].update(selector=selector, label=label)
    with links.service.db() as s:
        version = s.get(Analysis, aid).selection_version
    result = links.save(owner, pid, InteractiveBinding.model_validate(body), version, "save:" + selector)
    return result, DOMTarget.model_validate(body["target"])


def target(links, owner, pid, aid, package, locator, *, release=None):
    return links.binding(owner, pid, None if release else aid, release, package, locator)


def test_targeted_binding_matches_full_read_for_legacy_identity_and_open_steps(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    saved, locator = save(links, owner, pid, aid, package, section)
    save(links, owner, pid, aid, package, section, selector="#other")
    with service.db() as s:
        edge = s.get(Relation, saved["binding_id"])
        action = s.get(Revision, edge.from_revision_id)
        # Some old imports used a different stable key and omitted optional
        # locator defaults. Reading must resolve the metadata, not a new key.
        s.get(Entity, action.entity_id).stable_key = "legacy.action.identity"
        anchor = deepcopy(action.metadata_json)
        anchor["interactive_anchor"]["target"].pop("state")
        anchor["interactive_anchor"]["target"]["open_steps"] = ["#open-legacy-dialog"]
        action.metadata_json = anchor
    expected = next(b for b in links.bindings(owner, pid, aid) if b["id"] == saved["binding_id"])
    assert target(links, owner, pid, aid, package, locator) == expected
    assert (
        target(links, owner, pid, aid, package, locator.model_copy(update={"open_steps": ["#different"]})) == expected
    )
    assert target(links, owner, pid, aid, package, locator.model_copy(update={"state": "#another-dialog"})) is None
    assert target(links, owner, pid, aid, package, locator.model_copy(update={"frame_path": ["elsewhere"]})) is None


def test_targeted_binding_does_not_hydrate_unrelated_explanations(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    saved, locator = save(links, owner, pid, aid, package, section)
    unrelated = [save(links, owner, pid, aid, package, section, selector=f"#other-{i}")[0] for i in range(12)]
    with service.db() as s:
        unrelated_points = {s.get(Relation, row["binding_id"]).to_revision_id for row in unrelated}
    loaded = set()

    def observed(session, instance):
        if isinstance(instance, Revision):
            loaded.add(instance.id)

    event.listen(service.Session.class_, "loaded_as_persistent", observed)
    try:
        actual = target(links, owner, pid, aid, package, locator)
    finally:
        event.remove(service.Session.class_, "loaded_as_persistent", observed)
    assert actual["id"] == saved["binding_id"] and actual["sections"]
    assert not loaded & unrelated_points


def test_targeted_binding_keeps_exclusion_and_hides_currently_restricted_source(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    saved, locator = save(links, owner, pid, aid, package, section)
    with service.db() as s:
        s.get(Relation, saved["binding_id"]).excluded = True
    expected = links.bindings(owner, pid, aid)[0]
    assert expected["excluded"] and target(links, owner, pid, aid, package, locator) == expected
    limited = Actor(owner.id, frozenset({"knowledge:read", "knowledge:edit"}), True)
    with service.db() as s:
        artifact = s.get(Artifact, package)
        artifact.metadata_json = {**artifact.metadata_json, "requires_source_read": True}
    assert links.bindings(limited, pid, aid) == []
    assert target(links, limited, pid, aid, package, locator) is None


def test_targeted_binding_preserves_partial_and_orphan_citation_status(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    document = links.document(owner, pid, catalog["documents"][0]["revision_id"], aid, native=False)
    body = form(aid, package, section)
    body["section_revision_ids"] = [r["revision_id"] for r in document["sections"]][:2]
    assert len(body["section_revision_ids"]) == 2
    saved = links.save(owner, pid, InteractiveBinding.model_validate(body), catalog["selection_version"], "two")
    locator = DOMTarget.model_validate(body["target"])
    removed = body["section_revision_ids"][0]
    with service.db() as s:
        chapter = s.get(Revision, removed)
        s.delete(s.get(Selection, (aid, chapter.entity_id)))
        s.get(Analysis, aid).selection_version += 1
    expected = links.bindings(owner, pid, aid)[0]
    assert expected["validity_status"] == "outdated" and len(expected["sections"]) == 1
    assert target(links, owner, pid, aid, package, locator) == expected
    doc_info = next(d for d in catalog["documents"] if d["revision_id"] == document["revision_id"])
    DocumentService(service).save_draft(
        owner,
        doc_info["id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": document["revision_id"],
            "markdown": "# Replacement\n\n## New\n\nNew rule.",
            "locked_section_ids": [],
        },
        1,
    )
    expected = links.bindings(owner, pid, aid)[0]
    assert expected["id"] == saved["binding_id"] and expected["sections"] == []
    assert target(links, owner, pid, aid, package, locator) == expected


def test_targeted_frozen_binding_reads_the_release_manifest(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    _, locator = save(links, owner, pid, aid, package, section)
    release = service.save_snapshot(owner, pid, {"analysis_set_id": aid}, "targeted-snapshot")
    assert (
        target(links, owner, pid, aid, package, locator, release=release["id"])
        == links.bindings(owner, pid, release_id=release["id"])[0]
    )


def test_artifact_policy_reuses_only_the_immutable_read_transaction(interactive, monkeypatch):
    _web, service, _owner, _pid, _aid, _grant, package, _section, _raw = interactive
    calls = []
    original = reading._artifact_policy

    def observed(s, artifact, active=None):
        if active is None:
            calls.append(artifact.id)
        return original(s, artifact, active)

    monkeypatch.setattr(reading, "_artifact_policy", observed)
    with reading.ReadingService(service).db() as s:
        art = s.get(Artifact, package)
        first = reading.artifact_policy(s, art)
        assert reading.artifact_policy(s, art) == first
        assert calls.count(package) == 1
    with service.db() as s:
        art = s.get(Artifact, package)
        reading.artifact_policy(s, art)
        art.metadata_json = {**art.metadata_json, "requires_source_read": True}
        s.flush()
        assert reading.artifact_policy(s, art)["requires_source_read"]
    with reading.ReadingService(service).db() as s:
        assert reading.artifact_policy(s, s.get(Artifact, package))["requires_source_read"]
    assert calls.count(package) == 4


def test_incremental_anchor_index_refreshes_new_and_removed_selection(interactive):
    _web, service, owner, pid, aid, _grant, package, section, _raw = interactive
    links = InteractiveKnowledgeService(service)
    _, existing = save(links, owner, pid, aid, package, section)
    assert target(links, owner, pid, aid, package, existing)
    new = existing.model_copy(update={"selector": "#new-after-negative"})
    assert target(links, owner, pid, aid, package, new) is None
    saved, new = save(links, owner, pid, aid, package, section, selector=new.selector)
    assert target(links, owner, pid, aid, package, new)["id"] == saved["binding_id"]
    with service.db() as s:
        edge = s.get(Relation, saved["binding_id"])
        action = s.get(Revision, edge.from_revision_id)
        s.delete(s.get(Selection, (aid, action.entity_id)))
        s.get(Analysis, aid).selection_version += 1
    assert target(links, owner, pid, aid, package, new) is None


def test_material_root_facts_survive_a_failed_write_transaction(materials):
    from tests.interactive import test_materials

    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    test_materials.install(service, owner, manager, pid, aid)
    links = InteractiveKnowledgeService(service)
    with pytest.raises(RuntimeError, match="rollback"):
        with service.db() as s:
            _, _, choices, _ = links.reader.scope(s, owner, pid, aid, None)
            first = links._packages(s, owner, pid, choices)
            raise RuntimeError("rollback")
    with links.reader.reading.db() as s:
        _, _, choices, _ = links.reader.scope(s, owner, pid, aid, None)
        assert links._packages(s, owner, pid, choices) == first
    assert links.catalog(owner, pid, aid)["packages"]
