"""Coordinate request starts and rate-limit cooldowns across extraction years."""

from __future__ import annotations

import threading
import time


class RequestPacer:
    def __init__(self, interval: float, cooldown: float):
        self.interval = interval
        self.cooldown = cooldown
        self.condition = threading.Condition()
        self.next_start = 0.0
        self.blocked_until = 0.0

    def acquire(self) -> None:
        with self.condition:
            while True:
                now = time.monotonic()
                delay = max(self.next_start, self.blocked_until) - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    return
                self.condition.wait(min(delay, 60.0))

    def rate_limited(self, retry_after: float | None = None) -> float:
        delay = max(self.cooldown, retry_after or 0.0)
        with self.condition:
            self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
            self.condition.notify_all()
        return delay
