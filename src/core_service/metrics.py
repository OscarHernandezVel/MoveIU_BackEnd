"""Tiny in-process metrics registry (counters and latency windows)."""

from __future__ import annotations

import threading
from typing import Any

from core_service.data_structures import Array, CircularArray


class MetricsRegistry:
    """Thread-safe counters plus a sliding window of observations per metric.

    Percentiles are computed on the window with the project's own merge sort.
    """

    def __init__(self, window: int = 512) -> None:
        self._window = window
        self._counters: dict[str, int] = {}
        self._observations: dict[str, CircularArray[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        rendered = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        return f"{name}{{{rendered}}}"

    def increment(self, name: str, amount: int = 1, /, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def counter(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            window = self._observations.get(name)
            if window is None:
                window = CircularArray(self._window)
                self._observations[name] = window
            window.push(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            windows = {name: Array.from_iterable(ring) for name, ring in self._observations.items()}
        summaries: dict[str, dict[str, float]] = {}
        for name, values in windows.items():
            ordered = values.merge_sorted()
            count = len(ordered)
            summaries[name] = {
                "count": float(count),
                "p50": ordered[int(0.50 * (count - 1))],
                "p95": ordered[int(0.95 * (count - 1))],
                "max": ordered[count - 1],
            }
        return {"counters": counters, "observations": summaries}
