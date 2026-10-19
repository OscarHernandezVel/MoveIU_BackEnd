"""Per-device rate limit for *unsolicited* on-screen alerts (sliding window).

Each device owns a circular array holding the times of its last ``max_alerts`` alerts. A new
alert is allowed unless all of those fall inside the window. Alerts the passenger explicitly
asked for (proximity) bypass the throttle.
"""

from __future__ import annotations

import threading
from typing import Protocol

from core_service.clock import Clock
from core_service.data_structures import CircularArray
from core_service.security.rate_limiter import RateLimiter


class Throttle(Protocol):
    def allow(self, device_id: str) -> bool: ...

    def sweep(self) -> None: ...


class LimiterThrottle:
    """Throttle backed by any :class:`RateLimiter` (e.g. the Redis one shared by every replica)."""

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    def allow(self, device_id: str) -> bool:
        return self._limiter.check(device_id).allowed

    def sweep(self) -> None:
        return None  # the shared store expires its keys by itself


class AlertThrottle:
    def __init__(self, max_alerts: int, window_seconds: float, clock: Clock, max_devices: int = 10_000) -> None:
        self._max_alerts = max_alerts
        self._window = window_seconds
        self._clock = clock
        self._max_devices = max_devices
        self._rings: dict[str, CircularArray[float]] = {}
        self._lock = threading.Lock()

    def allow(self, device_id: str) -> bool:
        """Register an alert for ``device_id`` when the limit permits it."""
        now = self._clock.now()
        with self._lock:
            ring = self._rings.get(device_id)
            if ring is None:
                if len(self._rings) >= self._max_devices:
                    self._sweep_locked(now)
                ring = CircularArray(self._max_alerts)
                self._rings[device_id] = ring
            if ring.is_full() and now - ring.oldest() < self._window:
                return False
            ring.push(now)
            return True

    def _sweep_locked(self, now: float) -> None:
        for device_id in list(self._rings):
            ring = self._rings[device_id]
            if len(ring) == 0 or now - ring.newest() >= self._window:
                del self._rings[device_id]

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked(self._clock.now())
