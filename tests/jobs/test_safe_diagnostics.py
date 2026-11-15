"""Internal job failures expose code locations, never exception payloads."""

import json

from tests.jobs import test_durable_dispatch

setup = test_durable_dispatch.setup


def test_unexpected_job_failure_logs_type_and_location_without_payload(setup, monkeypatch, caplog):
    service, owner, project = setup
    job = service.demo_import(owner, project["id"], "diagnostic")

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic-secret-exception-payload")

    monkeypatch.setattr(service, "_commit_bundle", interrupted)
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "failed"
    assert result["error"]["code"] == "VKS_INTERNAL_ERROR"
    assert "RuntimeError" in caplog.text and "interrupted" in caplog.text
    assert "synthetic-secret-exception-payload" not in caplog.text + json.dumps(result)
