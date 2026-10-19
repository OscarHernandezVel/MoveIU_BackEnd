"""Circuit breaker around the AI provider.

While the provider is failing, calls are rejected immediately (no waiting for timeouts) and the
caller falls back to the rule-based analyzer. The recent outcomes live in a fixed-size
:class:`CircularArray`: the breaker opens when at least ``failure_threshold`` of the last
``window`` calls failed, and lets one probe through after ``recovery_seconds`` (half-open).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from core_service.clock import Clock
from core_service.data_structures import CircularArray
from core_service.errors import ServiceUnavailableError

T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(ServiceUnavailableError):
    default_code = "ai_unavailable"


class CircuitBreaker:
    def __init__(
        self, clock: Clock, failure_threshold: int = 5, window: int = 10, recovery_seconds: float = 60.0
    ) -> None:
        if failure_threshold < 1 or window < failure_threshold:
            raise ValueError("window must be >= failure_threshold >= 1")
        self._clock = clock
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._outcomes: CircularArray[bool] = CircularArray(window)  # True = success
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._refresh()
            return self._state

    def _refresh(self) -> None:
        if self._state is BreakerState.OPEN and self._clock.now() - self._opened_at >= self._recovery:
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = False

    def _failures(self) -> int:
        return sum(1 for ok in self._outcomes if not ok)

    def call(self, operation: Callable[[], T]) -> T:
        """Run ``operation`` if the breaker allows it; record its outcome."""
        with self._lock:
            self._refresh()
            if self._state is BreakerState.OPEN or (self._state is BreakerState.HALF_OPEN and self._probe_in_flight):
                raise CircuitOpenError()
            if self._state is BreakerState.HALF_OPEN:
                self._probe_in_flight = True
        try:
            result = operation()
        except Exception:
            self._record(False)
            raise
        self._record(True)
        return result

    def _record(self, success: bool) -> None:
        with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._probe_in_flight = False
                if success:
                    self._state = BreakerState.CLOSED
                    self._outcomes.clear()
                else:
                    self._trip()
                return
            self._outcomes.push(success)
            if not success and self._failures() >= self._threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock.now()
        self._outcomes.clear()
