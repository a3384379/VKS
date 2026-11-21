"""Actual model request capacity, independent of browser worker count."""

import os

# Instance capacity is an I/O budget, separate from browser runtimes. A fixed
# process setting keeps persistent reservations consistent across workers; all
# API/Celery processes for an instance must use the same value.
MAX_REQUEST_LIMIT = 128
GLOBAL_REQUEST_LIMIT = int(os.environ.get("VKS_AI_CONCURRENCY", "128"))
if not 1 <= GLOBAL_REQUEST_LIMIT <= MAX_REQUEST_LIMIT:
    raise ValueError("VKS_AI_CONCURRENCY must be between 1 and 128")
CLAIM_LOCK = 851392741
FULL_MODEL_JOBS = ("associate_all", "reassociation_preview", "explanation_batch")


def request_concurrency(lanes):
    lanes = min(4, max(1, lanes or 3))
    # A single lane remains deliberately serial for diagnostics and recovery.
    # Parallel scanning has a separate CPU budget; short model requests can use
    # more I/O slots without creating additional browser runtimes.
    return 1 if lanes == 1 else GLOBAL_REQUEST_LIMIT


def host_request_ceiling():
    return min(GLOBAL_REQUEST_LIMIT, max(2, 2 * (os.cpu_count() or 1)), 32)


def defer_request_job(s, job):
    """Leave work queued and broker/local delivery retryable without an attempt."""
    from sqlalchemy import select

    from .jobs import DispatchState
    from .models import Outbox
    from .service import future

    out = s.scalar(select(Outbox).where(Outbox.job_id == job.id))
    if out:
        out.dispatched = False
    dispatch = s.scalar(select(DispatchState).where(DispatchState.job_id == job.id))
    if dispatch:
        dispatch.state, dispatch.next_attempt_at = "pending", future(1)


def active_request_slots(s):
    """Count unexpired full-batch reservations and individual page requests."""
    from sqlalchemy import select

    from .models import Job, now

    return sum(
        1 if pipeline else request_concurrency(lanes)
        for pipeline, lanes in s.execute(
            select(
                Job.input_json["scan_pipeline"].as_boolean(),
                Job.input_json["concurrency"].as_integer(),
            ).where(
                Job.kind.in_(FULL_MODEL_JOBS),
                Job.state.in_(("running", "cancelling")),
                Job.lease_until > now(),
            )
        )
    )


def claim_full_association(service, s, job):
    """Reserve a full job's entire pool against the same persistent global cap.

    The caller marks it running in this transaction after authorization. The
    PostgreSQL advisory lock matches page-job claims, so a page/full race cannot
    each observe the same free slots and over-admit work on different workers.
    """
    from sqlalchemy import text

    if service.engine.dialect.name == "postgresql":
        s.execute(text(f"SELECT pg_advisory_xact_lock({CLAIM_LOCK})"))
    required = request_concurrency(job.input_json.get("concurrency", 3))
    if active_request_slots(s) + required > GLOBAL_REQUEST_LIMIT:
        defer_request_job(s, job)
        return False
    return True
