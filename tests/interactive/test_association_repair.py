"""Transient failures repair asynchronously without resetting the original job."""

import json
from copy import deepcopy

from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.bulk_association import BulkAssociationService
from vks_api.jobs import Checkpoint
from vks_api.models import Job
from vks_api.service import DomainError

from tests.interactive import test_bulk_association as bulk

materials = bulk.materials


def test_repairs_only_failed_targets_and_preserves_parent_progress(materials, monkeypatch):
    service, actor, _manager, pid, aid, *_rest, model = bulk.setup(materials, monkeypatch)
    failed = []

    def temporary(config, body):
        data = json.loads(body["input"][1]["content"])
        if data["control"]["selector"] == "#add" and not failed:
            failed.append(True)
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "temporary provider failure", 503, retryable=True)
        return model(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", temporary)
    parent = bulk.enqueue(BulkAssociationService(service), actor, pid, aid, concurrency=3)
    service.process_job(parent["id"])
    result = service.get_job(actor, parent["id"])
    assert result["state"] == "succeeded" and result["result"]["repair"]["items"] == 1
    child_id = result["result"]["repair"]["job_id"]
    with service.db() as session:
        checkpoints = {
            row.id: deepcopy(row.result_json)
            for row in session.scalars(select(Checkpoint).where(Checkpoint.job_id == parent["id"]))
        }
        child = session.get(Job, child_id)
        assert len(child.input_json["items"]) == 1
        assert child.input_json["_repair_round"] == 1
        assert child.input_json["_repair_parent_job_id"] == parent["id"]
    service.process_job(child_id)
    done = service.get_job(actor, child_id)
    assert done["state"] == "succeeded" and done["result"]["saved"] == 1
    assert done["result"]["pending"] == 0 and "repair" not in done["result"]
    assert service.get_job(actor, parent["id"]) == result
    with service.db() as session:
        assert checkpoints == {
            row.id: row.result_json
            for row in session.scalars(select(Checkpoint).where(Checkpoint.job_id == parent["id"]))
        }


def test_repair_is_bounded_and_never_recursively_retries(materials, monkeypatch):
    service, actor, _manager, pid, aid, *_ = bulk.setup(materials, monkeypatch)

    def unavailable(*args):
        raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "still unavailable", 503, retryable=True)

    monkeypatch.setattr(ai_gateway, "request_model", unavailable)
    parent = bulk.enqueue(BulkAssociationService(service), actor, pid, aid)
    service.process_job(parent["id"])
    child = service.get_job(actor, parent["id"])["result"]["repair"]["job_id"]
    service.process_job(child)
    assert "repair" not in service.get_job(actor, child)["result"]
    with service.db() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "associate_all")))) == 2


def test_evidence_gaps_do_not_generate_automatic_paid_retries(materials, monkeypatch):
    service, actor, _manager, pid, aid, *_ = bulk.setup(materials, monkeypatch)
    monkeypatch.setattr(
        ai_gateway, "request_model", lambda _config, body: bulk.synthetic_response(body, confidence=0.2)
    )
    parent = bulk.enqueue(BulkAssociationService(service), actor, pid, aid)
    service.process_job(parent["id"])
    result = service.get_job(actor, parent["id"])
    assert result["result"]["pending"] == 7 and "repair" not in result["result"]
