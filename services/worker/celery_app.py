from __future__ import annotations


from celery import Celery

from vks_api.connection import broker_url

celery = Celery("vks", broker=broker_url())
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="vks-index",
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    broker_transport_options={"confirm_publish": True},
    task_soft_time_limit=1800,
    task_time_limit=1860,
)
_service = None


def service():
    global _service
    if _service is None:
        from vks_api.service import Service

        _service = Service()
    return _service


@celery.task(name="vks.execute_job", bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def execute_job(self, job_id):
    from uuid import UUID

    UUID(job_id)
    try:
        service().process_job(job_id)
    except Exception:
        # Never return a DB/credential/broker exception body to a public result backend.
        raise self.retry(
            exc=RuntimeError("VKS_WORKER_DEPENDENCY_UNAVAILABLE"), countdown=min(60, 2**self.request.retries)
        )
