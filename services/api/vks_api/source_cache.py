"""Project-private syntax/dependency cache on disposable checkpoints."""

from sqlalchemy import select, delete
from .models import Job, Project, now
from .jobs import Checkpoint, current_actor
from .service import DomainError, JOB_SCOPES
from packages.analyzers.syntax_cache import canonical, MAX_PACKET_BYTES

MAX_PROJECT_BYTES = 128 * 1024 * 1024
MAX_PROJECT_UNITS = 16384
MAX_BUFFER_BYTES = 4 * 1024 * 1024
STAGES = {
    "java": "SOURCE_JAVA_UNIT",
    "xml": "SOURCE_XML_UNIT",
    "dependency_summary": "SOURCE_DEPENDENCY_SUMMARY",
    "semantic": "SOURCE_SEMANTIC_SHARD",
}


class SourceUnitCache:
    def __init__(self, service, jid, attempt):
        self.service, self.jid, self.attempt = service, jid, attempt
        self.pending = {}
        self.pending_bytes = 0

    def _job(self, s):
        job = s.get(Job, self.jid, populate_existing=True)
        if (
            not job
            or job.attempt != self.attempt
            or job.state not in ("running", "cancelling")
            or job.cancel_requested
            or job.lease_until
            and job.lease_until <= now()
        ):
            raise DomainError("VKS_CANCELLED", "解析缓存任务已失效", 409)
        actor = current_actor(self.service, job.created_by, job.project_id, s, job=job)
        self.service.require(s, actor, job.project_id, JOB_SCOPES[job.kind])
        if job.kind in ("index", "suggest_links"):
            self.service.require(s, actor, job.project_id, "source:read")
        return job

    def load(self, kind, key, processor):
        with self.service.db() as s:
            job = self._job(s)
            if (kind, key, processor) in self.pending:
                return self.pending[kind, key, processor]
            return s.scalar(
                select(Checkpoint.result_json)
                .join(Job, Checkpoint.job_id == Job.id)
                .where(
                    Job.project_id == job.project_id,
                    Checkpoint.stage == STAGES[kind],
                    Checkpoint.input_hash == key,
                    Checkpoint.processor_version == processor,
                )
                .order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc())
                .limit(1)
            )

    def store(self, kind, key, processor, packet):
        size = len(canonical(packet))
        if size > MAX_PACKET_BYTES:
            return
        with self.service.db() as s:
            self._job(s)
        previous = self.pending.get((kind, key, processor))
        self.pending_bytes += size - (previous.get("_cache_bytes", 0) if previous else 0)
        self.pending[kind, key, processor] = dict(packet, _cache_bytes=size)
        if len(self.pending) >= 64 or self.pending_bytes >= MAX_BUFFER_BYTES:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        with self.service.db() as s:
            row = s.get(Job, self.jid)
            if row:
                s.scalar(select(Project).where(Project.id == row.project_id).with_for_update())
            job = self._job(s)
            for (kind, key, processor), data in self.pending.items():
                previous = s.scalar(
                    select(Checkpoint).where(
                        Checkpoint.job_id == self.jid,
                        Checkpoint.stage == STAGES[kind],
                        Checkpoint.input_hash == key,
                        Checkpoint.processor_version == processor,
                    )
                )
                if previous:
                    previous.result_json = data
                else:
                    s.add(
                        Checkpoint(
                            job_id=self.jid,
                            stage=STAGES[kind],
                            input_hash=key,
                            processor_version=processor,
                            result_json=data,
                        )
                    )
            s.flush()
            rows = s.execute(
                select(Checkpoint.id, Checkpoint.result_json["_cache_bytes"].as_integer())
                .join(Job, Checkpoint.job_id == Job.id)
                .where(Job.project_id == job.project_id, Checkpoint.stage.in_(list(STAGES.values())))
                .order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc())
            ).all()
            total = 0
            evicted = []
            for index, (cid, stored_size) in enumerate(rows):
                total += stored_size if type(stored_size) is int and stored_size > 0 else MAX_PACKET_BYTES
                if index >= MAX_PROJECT_UNITS or total > MAX_PROJECT_BYTES:
                    evicted.append(cid)
            for start in range(0, len(evicted), 500):
                s.execute(delete(Checkpoint).where(Checkpoint.id.in_(evicted[start : start + 500])))
        self.pending.clear()
        self.pending_bytes = 0
