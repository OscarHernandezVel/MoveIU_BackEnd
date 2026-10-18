"""Time source abstraction so that every time-dependent rule is testable."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """Returns the current time as seconds since the Unix epoch."""

    @abstractmethod
    def now(self) -> float: ...


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Manually controlled clock for tests and simulations."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self._now += seconds
        return self._now

    def set(self, value: float) -> None:
        self._now = float(value)
