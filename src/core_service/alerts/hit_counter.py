"""Requires several consecutive proximity hits before notifying (avoids GPS jitter).

The streak of an (alert, bus) pair is a tiny document ``{count, last_at}``. In one process it lives
in a dictionary; with several replicas it lives in the shared document store (with a TTL of one
window, so abandoned streaks disappear by themselves), because consecutive readings of a bus may be
handled by different replicas.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from core_service.clock import Clock
from core_service.persistence.documents import DocumentStore

COLLECTION = "hit_streaks"


@dataclass
class _Streak:
    count: int
    last_at: float


class ConsecutiveHitCounter:
    def __init__(self, clock: Clock, window_seconds: float = 20.0, store: DocumentStore | None = None) -> None:
        self._clock = clock
        self._window = window_seconds
        self._store = store
        self._streaks: dict[str, dict[str, _Streak]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(alert_id: str, bus_id: str) -> str:
        return f"{alert_id}:{bus_id}"

    def hit(self, alert_id: str, bus_id: str) -> int:
        """Register a hit and return the length of the current streak. A gap longer than the
        window restarts the streak."""
        now = self._clock.now()
        if self._store is not None:
            key = self._key(alert_id, bus_id)
            document = self._store.get(COLLECTION, key)
            count = 1
            if document is not None and now - float(document["last_at"]) <= self._window:
                count = int(document["count"]) + 1
            self._store.put(COLLECTION, key, {"count": count, "last_at": now}, ttl_seconds=self._window * 2)
            return count
        with self._lock:
            per_bus = self._streaks.setdefault(alert_id, {})
            streak = per_bus.get(bus_id)
            if streak is not None and now - streak.last_at <= self._window:
                streak.count += 1
                streak.last_at = now
            else:
                streak = _Streak(1, now)
                per_bus[bus_id] = streak
            return streak.count

    def reset(self, alert_id: str, bus_id: str) -> None:
        if self._store is not None:
            self._store.delete(COLLECTION, self._key(alert_id, bus_id))
            return
        with self._lock:
            self._streaks.get(alert_id, {}).pop(bus_id, None)

    def forget(self, alert_id: str) -> None:
        if self._store is not None:
            for key in self._store.keys(COLLECTION):
                if key.startswith(f"{alert_id}:"):
                    self._store.delete(COLLECTION, key)
            return
        with self._lock:
            self._streaks.pop(alert_id, None)

    def sweep(self) -> int:
        """Drop stale streaks to keep memory bounded; returns how many were removed."""
        if self._store is not None:
            return 0  # the store expires them by TTL
        now = self._clock.now()
        removed = 0
        with self._lock:
            for alert_id in list(self._streaks):
                per_bus = self._streaks[alert_id]
                for bus_id in list(per_bus):
                    if now - per_bus[bus_id].last_at > self._window:
                        del per_bus[bus_id]
                        removed += 1
                if not per_bus:
                    del self._streaks[alert_id]
        return removed
