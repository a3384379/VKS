"""Reusable private task stages tied to exact inputs, code, attempt and grants."""

from functools import lru_cache
from pathlib import Path
import json
from sqlalchemy import select
from .jobs import Checkpoint
from .models import Job
from .service import DomainError, canonical, sha

MAX_STAGE_BYTES = 32 * 1024 * 1024


@lru_cache(maxsize=1)
def processor_version():
    files = (
        "knowledge_tasks.py",
        "task_checkpoints.py",
        "task_planning.py",
        "module_discovery.py",
        "ai_gateway.py",
        "workbench_v2.py",
        "journeys.py",
        "reading.py",
    )
    sources = b"".join(Path(__file__).with_name(name).read_bytes() for name in files)
    sources += (Path(__file__).resolve().parents[3] / "packages/journey_dsl/__init__.py").read_bytes()
    return "knowledge-stages-v3:" + sha(sources)[:32]


class TaskCheckpoints:
    def __init__(self, tasks, jid, attempt):
        self.tasks, self.service, self.jid, self.attempt = tasks, tasks.service, jid, attempt

    def _job(self, s):
        job = s.get(Job, self.jid)
        if not job or job.attempt != self.attempt:
            raise DomainError("VKS_CANCELLED", "旧任务 attempt 不得复用或提交阶段", 409)
        self.tasks._validate(s, job)
        return job

    @staticmethod
    def input_hash(job, inputs):
        plan = {key: value for key, value in job.input_json.items() if not key.startswith("_")}
        return sha(
            canonical(
                dict(
                    plan=plan,
                    inputs=inputs,
                    authorization=job.input_json.get("_authorization"),
                    selection_version=job.input_json["_selection_version"],
                    model_policy_sha256=job.input_json.get("_model_policy_sha256"),
                    historical=job.input_json.get("_historical_revision_ids", []),
                )
            )
        )

    def run(self, stage, inputs, operation, *, track=True):
        if track:
            self.tasks._stage(self.jid, stage, self.attempt)
        processor = processor_version()
        with self.service.db() as s:
            job = self._job(s)
            input_hash = self.input_hash(job, inputs)
            point = s.scalar(
                select(Checkpoint).where(
                    Checkpoint.job_id == self.jid,
                    Checkpoint.stage == "KNOWLEDGE_" + stage.upper(),
                    Checkpoint.input_hash == input_hash,
                    Checkpoint.processor_version == processor,
                )
            )
            key = point.result_json.get("object_key") if point else None
        if isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key):
            try:
                path = self.service.data_dir / "objects" / key
                if path.stat().st_size > MAX_STAGE_BYTES:
                    raise ValueError("Stage over quota")
                raw = path.read_bytes()
                if sha(raw) != key:
                    raise ValueError("Stage digest mismatch")
                packet = json.loads(raw)
                if (
                    packet["input_hash"] != input_hash
                    or packet["processor"] != processor
                    or packet["stage"] != stage
                    or not isinstance(packet["result"], dict)
                ):
                    raise ValueError("Stage identity mismatch")
                if track:
                    self._complete(stage, cached=True)
                return packet["result"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        result = operation()
        raw = canonical(dict(stage=stage, input_hash=input_hash, processor=processor, result=result)).encode()
        if len(raw) > MAX_STAGE_BYTES:
            raise DomainError("VKS_LIMIT_EXCEEDED", "知识阶段产物超过检查点配额")
        with self.service.db() as s:
            self._job(s)
            key = self.service._put_object(raw)
            point = s.scalar(
                select(Checkpoint).where(
                    Checkpoint.job_id == self.jid,
                    Checkpoint.stage == "KNOWLEDGE_" + stage.upper(),
                    Checkpoint.input_hash == input_hash,
                    Checkpoint.processor_version == processor,
                )
            )
            if point:
                point.result_json = {"object_key": key}
            else:
                s.add(
                    Checkpoint(
                        job_id=self.jid,
                        stage="KNOWLEDGE_" + stage.upper(),
                        input_hash=input_hash,
                        processor_version=processor,
                        result_json={"object_key": key},
                    )
                )
        if track:
            self._complete(stage, cached=False)
        return result

    def _complete(self, stage, cached):
        with self.service.db() as s:
            job = self._job(s)
            history = [dict(row) for row in job.input_json["_stage_history"]]
            if not history or history[-1]["stage"] != stage:
                raise DomainError("VKS_STATE_CONFLICT", "任务阶段已改变", 409)
            history[-1].update(state="complete", cached="true" if cached else "false")
            job.input_json = dict(job.input_json, _stage_history=history)
