"""Per-bus reading history stored as a LIFO stack.

The top of the stack is the most recent reading, so the *live state* of a bus is
``peek()`` (O(1)); the door/camera cross-check looks only at the top ``k`` entries
(O(k)); and the terminal auto-correction empties the stack with ``drain()``.

Two things survive the terminal reset because they are *not* part of the door/camera comparison:
:meth:`ReadingHistory.latest` (the newest reading ever accepted, needed to recognise late and
duplicate readings and to audit a bus that is standing at the terminal) and its timestamp.

Sharing between replicas
------------------------
With several replicas a bus may be handled by a different one on every reading. The store can keep
a compact snapshot (the newest ``shared_size`` entries) in the document store; a replica that finds
its own history *behind* the live state restores that snapshot before processing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from core_service.data_structures import Stack
from core_service.errors import DomainError
from core_service.models import Reading
from core_service.persistence.documents import DocumentStore

log = logging.getLogger(__name__)

COLLECTION = "reading_history"


@dataclass(frozen=True)
class HistoryEntry:
    reading: Reading
    outbound_progress: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"reading": self.reading.to_dict(), "progress": self.outbound_progress}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        return cls(Reading.from_dict(data["reading"]), data.get("progress"))


class ReadingHistory:
    """Bounded stack of :class:`HistoryEntry` (the oldest entries fall off the bottom)."""

    def __init__(self, bus_id: str, max_size: int = 120) -> None:
        self.bus_id = bus_id
        self._stack: Stack[HistoryEntry] = Stack(max_size=max_size)
        self._latest: HistoryEntry | None = None
        self._before_latest: HistoryEntry | None = None

    def __len__(self) -> int:
        return len(self._stack)

    def __iter__(self) -> Iterator[HistoryEntry]:
        """Most recent first."""
        return iter(self._stack)

    def push(self, reading: Reading, outbound_progress: float | None = None) -> HistoryEntry:
        entry = HistoryEntry(reading, outbound_progress)
        self._stack.push(entry)
        self._before_latest, self._latest = self._latest, entry
        return entry

    def discard_latest(self) -> HistoryEntry | None:
        """Undo the last ``push`` (used to roll back a failed processing pipeline)."""
        if self._stack.is_empty():
            return None
        self._latest = self._before_latest
        return self._stack.pop()

    def current(self) -> HistoryEntry | None:
        """Top of the stack: the newest reading *since the last terminal reset*."""
        return self._stack.peek_or_none()

    def latest(self) -> HistoryEntry | None:
        """The newest reading accepted, even if a terminal reset emptied the stack after it."""
        return self._latest

    def recent(self, count: int) -> list[HistoryEntry]:
        return self._stack.peek_many(count)

    def reset(self) -> int:
        """Terminal auto-correction: empty the history and return how many entries were dropped.
        :meth:`latest` is kept on purpose."""
        return len(self._stack.drain())

    # ---------------------------------------------------------------- snapshots
    def snapshot(self, limit: int) -> dict[str, Any]:
        return {
            "latest": None if self._latest is None else self._latest.to_dict(),
            "entries": [entry.to_dict() for entry in self._stack.peek_many(limit)],  # newest first
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Replace the content with a snapshot (only ever moves the history *forward*)."""
        latest_raw = data.get("latest")
        latest = None if latest_raw is None else HistoryEntry.from_dict(latest_raw)
        if latest is not None and self._latest is not None and latest.reading.ts <= self._latest.reading.ts:
            return
        self._stack.clear()
        for raw in reversed(data.get("entries", [])):  # snapshot is newest first: push the oldest first
            self._stack.push(HistoryEntry.from_dict(raw))
        self._latest = latest
        self._before_latest = None


class ReadingHistoryStore:
    """Thread-safe registry of one :class:`ReadingHistory` per bus."""

    def __init__(
        self,
        max_size: int = 120,
        shared: DocumentStore | None = None,
        shared_size: int = 40,
        shared_ttl_seconds: float = 900.0,
    ) -> None:
        self._max_size = max_size
        self._shared = shared
        self._shared_size = shared_size
        self._shared_ttl = shared_ttl_seconds
        self._histories: dict[str, ReadingHistory] = {}
        self._lock = threading.Lock()

    def get(self, bus_id: str) -> ReadingHistory:
        with self._lock:
            history = self._histories.get(bus_id)
            if history is None:
                history = ReadingHistory(bus_id, self._max_size)
                self._histories[bus_id] = history
            return history

    def peek(self, bus_id: str) -> ReadingHistory | None:
        with self._lock:
            return self._histories.get(bus_id)

    def bus_ids(self) -> list[str]:
        with self._lock:
            return list(self._histories)

    # ------------------------------------------------------------ shared snapshot
    def sync(self, bus_id: str, live_ts: float | None) -> ReadingHistory:
        """Local history, first catching up with the shared snapshot when it is behind the live
        state (another replica processed newer readings, or this process just restarted)."""
        history = self.get(bus_id)
        if self._shared is None or live_ts is None:
            return history
        latest = history.latest()
        if latest is not None and latest.reading.ts >= live_ts:
            return history
        try:
            document = self._shared.get(COLLECTION, bus_id)
        except DomainError:
            log.warning("Shared reading history unavailable for %s; continuing with the local one", bus_id)
            return history
        if document is not None:
            history.restore(document)
        return history

    def persist(self, bus_id: str) -> None:
        """Best effort: the snapshot is an optimisation, so a failure must not fail the reading."""
        if self._shared is None:
            return
        history = self.peek(bus_id)
        if history is None:
            return
        try:
            self._shared.put(COLLECTION, bus_id, history.snapshot(self._shared_size), ttl_seconds=self._shared_ttl)
        except DomainError:
            log.warning("Could not share the reading history of %s", bus_id)

    def latest_entry(self, bus_id: str) -> HistoryEntry | None:
        """Newest reading of a bus known by this replica or by the shared snapshot."""
        local = self.get(bus_id).latest()
        if self._shared is None:
            return local
        try:
            document = self._shared.get(COLLECTION, bus_id)
        except DomainError:
            return local
        raw = None if document is None else document.get("latest")
        if raw is None:
            return local
        remote = HistoryEntry.from_dict(raw)
        if local is None or remote.reading.ts > local.reading.ts:
            return remote
        return local
