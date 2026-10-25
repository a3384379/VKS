from __future__ import annotations

import signal
from threading import Event

from vks_api.jobs import dispatch_pending, reconcile
from vks_api.service import Service


def main():
    service = Service()
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(1):
        try:
            reconcile(service)
            dispatch_pending(service)
        except Exception:
            continue
    service.engine.dispose()


if __name__ == "__main__":
    main()
