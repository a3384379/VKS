"""Host-aware admission for association model requests."""

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from threading import Condition
from time import monotonic

from .association_limits import host_request_ceiling


def _sample():
    cpus = os.cpu_count() or 1
    load = os.getloadavg()[0] / cpus if hasattr(os, "getloadavg") else 0
    available = None
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/bin/memory_pressure", "-Q"], capture_output=True, text=True, timeout=1, check=True
            )
            match = re.search(r"System-wide memory free percentage: (\d+)%", result.stdout)
            if match:
                available = int(match[1]) / 100
        elif sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as file:
                values = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", file.read(), re.MULTILINE))
            if values.get("MemTotal") and values.get("MemAvailable"):
                available = int(values["MemAvailable"]) / int(values["MemTotal"])
    except (OSError, subprocess.SubprocessError):
        pass
    return load, available


class AssociationPressure:
    def __init__(self, ceiling, sample=_sample, clock=monotonic):
        self.ceiling, self.sample, self.clock = ceiling, sample, clock
        self.condition = Condition()
        self.limit = min(4, ceiling)
        self.active = self.healthy = 0
        self.last_sample = float("-inf")
        self.cooldown = 0.0

    def refresh(self):
        now = self.clock()
        if now - self.last_sample < 2:
            return
        self.last_sample = now
        load, free = self.sample()
        if load >= 0.65 or (free is not None and free <= 0.25):
            self.limit = max(1, self.limit // 2)
            self.healthy = 0
        elif free is not None and free >= 0.35 and load <= 0.45 and now >= self.cooldown:
            self.healthy += 1
            if self.healthy >= 3:
                self.limit = min(self.ceiling, self.limit + 2)
                self.healthy = 0
        else:
            self.healthy = 0

    @contextmanager
    def request(self):
        with self.condition:
            while True:
                self.refresh()
                if self.active < self.limit:
                    self.active += 1
                    break
                self.condition.wait(0.5)
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def throttled(self):
        with self.condition:
            self.limit = max(1, self.limit // 2)
            self.healthy = 0
            self.cooldown = self.clock() + 20


ASSOCIATION_PRESSURE = AssociationPressure(host_request_ceiling())
