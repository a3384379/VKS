"""Cooperative cancellation for in-memory processors; no-op outside a job scope."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic


class AnalysisCancelled(Exception):
    """The owning job requested cancellation before another unit was scheduled."""


_ACTIVE = ContextVar("vks_analysis_cancellation", default=None)


@contextmanager
def cancellation_scope(callback, interval_seconds=0.1):
    state = {"callback": callback, "interval": max(0, float(interval_seconds)), "last": float("-inf")}
    token = _ACTIVE.set(state)
    try:
        checkpoint(force=True)
        yield
        checkpoint(force=True)
    finally:
        _ACTIVE.reset(token)


def checkpoint(force=False):
    state = _ACTIVE.get()
    if state is None:
        return
    current = monotonic()
    if force or current - state["last"] >= state["interval"]:
        state["last"] = current
        if state["callback"]():
            raise AnalysisCancelled("Analysis cancelled before scheduling the next unit")
