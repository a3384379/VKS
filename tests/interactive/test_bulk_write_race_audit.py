"""Independent late-model tests exercise the writer used by pipeline children."""

import json

import pytest
from vks_api import ai_gateway
from vks_api.bulk_association import BulkAssociationService
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import Analysis, Relation

from tests.interactive import test_bulk_association, test_materials

materials = test_materials.materials


@pytest.mark.parametrize("change", ["insert_manual", "exclude"])
def test_model_response_cannot_overwrite_a_manual_insert_or_restore_an_exclusion(materials, monkeypatch, change):
    service, owner, _, pid, aid, prototype, _, calls, ordinary = test_bulk_association.setup(
        materials, monkeypatch, source='<title>收费项</title><button id="add">新增收费项</button>'
    )
    bulk, links = BulkAssociationService(service), InteractiveKnowledgeService(service)
    if change == "exclude":
        baseline = test_bulk_association.enqueue(bulk, owner, pid, aid)
        service.process_job(baseline["id"])
        prior = next(b for b in links.bindings(owner, pid, aid) if b["target"]["selector"] == "#add")
    else:
        prior = None
    touched, writes, failures = [], [], []

    def mutate_while_model_is_away(config, body):
        data = json.loads(body["input"][1]["content"])
        if data["control"]["selector"] == "#add" and not touched:
            touched.append(data["control"])
            if change == "insert_manual":
                with service.db() as s:
                    expected = str(s.get(Analysis, aid).selection_version)
                try:
                    writes.append(
                        links.save(
                            owner,
                            pid,
                            InteractiveBinding(
                                analysis_set_id=aid,
                                package_id=prototype["artifact_id"],
                                target=data["control"],
                                point={"title": "人工确认", "summary": "模型等待期间由用户保存的说明"},
                                section_revision_ids=[data["chapters"][0]["revision_id"]],
                            ),
                            expected,
                            "manual-during-inference",
                        )
                    )
                except Exception as error:
                    failures.append(error)
                    raise
            else:
                # The versioned relation mutation is the same persisted flag
                # consulted by both the normal and grouped model writer.
                with service.db() as s:
                    s.get(Relation, prior["id"]).excluded = True
                    s.get(Analysis, aid).selection_version += 1
        return ordinary(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", mutate_while_model_is_away)
    job = test_bulk_association.enqueue(bulk, owner, pid, aid, mode="all")
    service.process_job(job["id"])
    done = service.get_job(owner, job["id"])
    assert done["state"] == "succeeded", done
    assert len(touched) == 1 and calls
    if change == "insert_manual":
        assert writes, failures
    current = next(b for b in links.bindings(owner, pid, aid) if b["target"]["selector"] == "#add")
    if change == "insert_manual":
        assert current["origin"] == "manual"
        assert current["title"] == "人工确认" and current["summary"] == "模型等待期间由用户保存的说明"
    else:
        assert current["excluded"] is True
        assert current["point_revision_id"] == prior["point_revision_id"]
    assert done["result"]["saved"] < done["result"]["total"]
