"""Bounded local preparation, remote I/O and durable completion stages.

Slots cover only remote requests, not saves/checkpoints. Each target still has
its original independent prompt, response and fresh transactional save fences.
"""

import os
from concurrent.futures import Future
from contextlib import nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from itertools import count
from queue import Empty, PriorityQueue
from threading import BoundedSemaphore, Thread
from time import monotonic

CURRENT_PIPELINE = ContextVar("vks_inference_pipeline", default=None)
WRITE_SESSION = ContextVar("vks_inference_write_session", default=None)
READ_SESSION = ContextVar("vks_inference_read_session", default=None)


@dataclass
class Completion:
    fn: object
    revision_ids: tuple
    analysis_set_id: str | None
    usage_id: str | None
    binding: tuple | None

    def __call__(self):
        return self.fn()


@dataclass
class ReadRequest:
    fn: object
    vector_lookup: tuple | None
    binding: tuple | None

    def __call__(self):
        return self.fn()


class InferencePipeline:
    def __init__(self, capacity, *, service=None, scope=None):
        self.capacity = capacity
        self.service = service
        self.scope = scope
        self.local_capacity = min(64, capacity)
        self.preparation = BoundedSemaphore(self.local_capacity)
        self.remote = BoundedSemaphore(capacity)
        # A little ready work hides preparation/commit latency without creating
        # another unbounded queue of HTTP requests or SQLite writers.
        self.workers = 2 * capacity + self.local_capacity
        self.batch_size = int(os.environ.get("VKS_AI_SAVE_BATCH_SIZE", "32"))
        if not 1 <= self.batch_size <= 128:
            raise ValueError("VKS_AI_SAVE_BATCH_SIZE must be between 1 and 128")
        self.sequence = count()
        self.completions = PriorityQueue()
        self.writer = Thread(target=self._write, name="vks-infer-save", daemon=True)
        self.reads = PriorityQueue()
        self.new_query_vectors = {}
        self.reader = Thread(target=self._read, name="vks-infer-prepare", daemon=True)

    def __enter__(self):
        self.token = CURRENT_PIPELINE.set(self)
        self.writer.start()
        self.reader.start()
        return self

    def __exit__(self, *exc):
        CURRENT_PIPELINE.reset(self.token)
        self.completions.put((100, next(self.sequence), None))
        self.reads.put((100, next(self.sequence), None))
        self.writer.join()
        self.reader.join()

    def prepare(self, fn):
        with self.preparation:
            return fn()

    def request(self, fn):
        with self.remote:
            return fn()

    def complete(self, fn, *, priority=False, revision_ids=(), analysis_set_id=None, usage_id=None, binding=None):
        result = Future()
        if revision_ids or usage_id:
            fn = Completion(fn, tuple(revision_ids), analysis_set_id, usage_id, binding)
        self.completions.put((0 if priority else 1, next(self.sequence), (copy_context(), fn, result)))
        return result.result()

    def read(self, fn, *, priority=False, vector_lookup=None, binding=None):
        if self.service is None or self.capacity == 1:
            return fn()
        result = Future()
        if vector_lookup is not None or binding is not None:
            fn = ReadRequest(fn, vector_lookup, binding)
        self.reads.put((0 if priority else 1, next(self.sequence), (copy_context(), fn, result)))
        return result.result()

    def _read(self):
        from .reading import ReadingService

        while True:
            first = self.reads.get()
            if first[2] is None:
                return
            pending = [first]
            deadline = monotonic() + 0.1
            while len(pending) < 32:
                try:
                    entry = self.reads.get(timeout=max(0, deadline - monotonic()))
                except Empty:
                    break
                if entry[2] is None:
                    self.reads.put(entry)
                    break
                pending.append(entry)
            outcomes = []
            try:
                # Ready independent requests share ONE current read snapshot.
                # No permission decision survives this transaction or batch.
                with ReadingService(self.service).db() as session:
                    from sqlalchemy import event

                    retained = set()
                    event.listen(session, "loaded_as_persistent", lambda _s, row, kept=retained: kept.add(row))
                    session.info["vks_read_batch"] = True
                    if self.scope and self.service.personal_mode and session.bind.dialect.name == "sqlite":
                        from .association_snapshot import load

                        load(session, self.scope)
                    groups = {}
                    targets = [
                        fn.binding
                        for _p, _seq, (_ctx, fn, _future) in pending
                        if isinstance(fn, ReadRequest) and fn.binding
                    ]
                    if targets:
                        from .batch_write_inputs import prefetch_targets

                        prefetch_targets(self.service, session, targets)
                    for _p, _seq, (context, fn, _future) in pending:
                        if isinstance(fn, ReadRequest) and fn.vector_lookup:
                            rag, actor, pid, key = fn.vector_lookup
                            groups.setdefault((rag, actor, pid), (context, set()))[1].add(key)
                    for (rag, actor, pid), (context, keys) in groups.items():

                        def prefetch(rag=rag, actor=actor, pid=pid, keys=keys):
                            token = READ_SESSION.set((self.service, session))
                            try:
                                rag.prefetch_queries(actor, pid, keys)
                            finally:
                                READ_SESSION.reset(token)

                        context.run(prefetch)
                    for _priority, _seq, (context, fn, _future) in pending:

                        def run(fn=fn, session=session):
                            token = READ_SESSION.set((self.service, session))
                            try:
                                return fn()
                            finally:
                                READ_SESSION.reset(token)

                        try:
                            outcomes.append((True, context.run(run)))
                        except Exception as exc:  # noqa: BLE001 - preserve each caller's rejection
                            outcomes.append((False, exc))
            except Exception as exc:  # noqa: BLE001 - reject every caller on snapshot failure
                outcomes = [(False, exc)] * len(pending)
            for (_priority, _seq, (_ctx, _fn, future)), (success, value) in zip(pending, outcomes, strict=True):
                if success:
                    future.set_result(value)
                else:
                    future.set_exception(value)

    def _write(self):
        while True:
            first = self.completions.get()
            if first[2] is None:
                return
            pending = [first]
            deadline = monotonic() + (0.02 if first[0] == 0 else 0.5)
            while len(pending) < self.batch_size:
                try:
                    entry = self.completions.get(timeout=max(0, deadline - monotonic()))
                except Empty:
                    break
                if entry[2] is None:
                    self.completions.put(entry)
                    break
                pending.append(entry)
                if entry[0] == 0 and first[0] != 0:
                    break
            pending.sort(key=lambda entry: entry[:2])
            outcomes = []
            session = None
            after_commit = []
            try:
                # Batch size is a ceiling. Stop after 250ms of work so new user
                # edits/cancellation and ready reservations can take the writer.
                with self.service.db() if self.service else nullcontext() as session:
                    if session:
                        from sqlalchemy import event

                        # A short batch uses normal ORM identity semantics.
                        # Keep loaded rows alive so repeated per-item checks do
                        # not lose them from SQLAlchemy's weak identity map.
                        # This holds objects, never authorization decisions.
                        retained = set()
                        event.listen(session, "loaded_as_persistent", lambda _s, row, kept=retained: kept.add(row))
                        event.listen(session, "pending_to_persistent", lambda _s, row, kept=retained: kept.add(row))
                        session.info["vks_completion_batch"] = True
                        if self.scope and self.service.personal_mode and session.bind.dialect.name == "sqlite":
                            from .association_snapshot import load

                            load(session, self.scope)
                        from .batch_write_inputs import prefetch

                        prefetch(
                            self.service,
                            session,
                            [fn for _p, _seq, (_ctx, fn, _future) in pending if isinstance(fn, Completion)],
                        )
                    began = monotonic()
                    for i, (_priority, _sequence, (context, fn, _future)) in enumerate(pending):
                        progress = (
                            {k: dict(v) for k, v in session.info.get("vks_progress_pending", {}).items()}
                            if session
                            else {}
                        )
                        before = dict(session.info.get("vks_before_commit", {})) if session else {}
                        after = dict(session.info.get("vks_after_commit", {})) if session else {}

                        def run(fn=fn, session=session):
                            token = WRITE_SESSION.set((self.service, session)) if self.service else None
                            try:
                                with session.begin_nested() if session else nullcontext():
                                    return fn()
                            finally:
                                if token is not None:
                                    WRITE_SESSION.reset(token)

                        try:
                            outcomes.append((True, context.run(run)))
                        except Exception as exc:  # noqa: BLE001 - propagate the exact error to its caller
                            if session:
                                session.info["vks_progress_pending"] = progress
                                session.info["vks_before_commit"] = before
                                session.info["vks_after_commit"] = after
                                session.info.pop("vks_batch_fingerprints", None)
                                session.info.pop("vks_batch_job_scopes", None)
                                session.info.pop("vks_batch_bindings", None)
                                session.info.pop("vks_evidence_rows", None)
                                session.info.pop("vks_selection_maps", None)
                                session.info.pop("vks_analysis_releases", None)
                                session.info.pop("vks_ledgers", None)
                                session.info.pop("vks_ready_edges", None)
                                session.info.pop("vks_evidence_queried", None)
                                session.info.pop("vks_ready_entities", None)
                                session.info.pop("vks_ready_idempotency", None)
                                session.info.pop("vks_new_artifact_ids", None)
                                session.info.pop("vks_known_artifacts", None)
                                session.info.pop("vks_known_revisions", None)
                            outcomes.append((False, exc))
                        if monotonic() - began >= 0.25 and i + 1 < len(pending):
                            for remaining in pending[i + 1 :]:
                                self.completions.put(remaining)
                            pending = pending[: i + 1]
                            break
                    if session:
                        for callback in session.info.get("vks_before_commit", {}).values():
                            callback()
                        after_commit = list(session.info.get("vks_after_commit", {}).values())
                for callback in after_commit:
                    callback()
            except Exception as exc:  # noqa: BLE001 - never acknowledge a failed commit
                outcomes = [(False, exc)] * len(pending)
            finally:
                if session:
                    session.info.clear()
            # Only committed items wake callers or enter in-memory reuse caches.
            for (_priority, _seq, (_ctx, _fn, future)), (success, value) in zip(pending, outcomes, strict=True):
                if success:
                    future.set_result(value)
                else:
                    future.set_exception(value)
