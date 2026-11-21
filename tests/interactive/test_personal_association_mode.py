"""Personal tasks omit membership checks but keep cancellation and data fences."""

import json

from vks_api import ai_gateway, identity
from vks_api.bulk_association import BulkAssociationService
from vks_api.models import Job

from tests.interactive import test_bulk_association

materials = test_bulk_association.materials


def test_personal_task_uses_owner_without_identity_or_capability_checks(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, calls, _ = test_bulk_association.setup(materials, monkeypatch)
    service.personal_mode = True
    bulk = BulkAssociationService(service)
    job = test_bulk_association.enqueue(bulk, actor, pid, aid, concurrency=3)

    def forbidden(*args, **kwargs):
        raise AssertionError("Personal association task performed a membership/capability check")

    monkeypatch.setattr(identity, "authorize_project", forbidden)
    service.process_job(job["id"])
    with service.db() as s:
        saved = s.get(Job, job["id"])
        assert saved.state == "succeeded" and saved.result_json["saved"] == 7
    assert len(calls) == 6


def test_personal_mode_still_cancels_and_preserves_the_original_input(materials, monkeypatch):
    service, actor, _, pid, aid, _, _, _calls, provider = test_bulk_association.setup(materials, monkeypatch)
    service.personal_mode = True
    bulk = BulkAssociationService(service)
    job = test_bulk_association.enqueue(bulk, actor, pid, aid, concurrency=3)
    with service.db() as s:
        original = json.loads(json.dumps(s.get(Job, job["id"]).input_json))

    def cancel(config, body):
        with service.db() as s:
            s.get(Job, job["id"]).cancel_requested = True
        return provider(config, body)

    monkeypatch.setattr(ai_gateway, "request_model", cancel)
    service.process_job(job["id"])
    timing = service.get_job(actor, job["id"])["timing"]
    assert timing["started_at"] and timing["baseline"] == 0
    with service.db() as s:
        stopped = s.get(Job, job["id"])
        assert stopped.state == "cancelled"
        assert stopped.input_json == {**original, "_run_started_at": timing["started_at"], "_run_baseline": 0}
