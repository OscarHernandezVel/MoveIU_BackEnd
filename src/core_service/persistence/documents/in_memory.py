"""Thread-safe in-memory document store with TTL."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core_service.clock import Clock, SystemClock
from core_service.errors import ValidationError
from core_service.persistence.documents.store import Document, DocumentStore


@dataclass
class _Entry:
    payload: str  # canonical JSON text: forces copies and rejects non-serialisable data
    expires_at: float | None


def serialize(document: Document) -> str:
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValidationError("document_not_serializable", details=[str(error)]) from error


class InMemoryDocumentStore(DocumentStore):
    """Documents live in dictionaries; each read returns a fresh copy (no aliasing)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._collections: dict[str, dict[str, _Entry]] = {}
        self._sets: dict[tuple[str, str], set[str]] = {}
        self._lock = threading.RLock()

    # ----- hooks overridden by durable subclasses (called while the lock is held)
    def _record_put(self, collection: str, key: str, entry: _Entry) -> None:
        """Called after a write."""

    def _record_delete(self, collection: str, key: str) -> None:
        """Called after a removal."""

    # ------------------------------------------------------------------ helpers
    def _expiry(self, ttl_seconds: float | None) -> float | None:
        if ttl_seconds is None:
            return None
        if ttl_seconds <= 0:
            raise ValidationError("invalid_ttl")
        return self._clock.now() + ttl_seconds

    def _live_entry(self, collection: str, key: str) -> _Entry | None:
        entry = self._collections.get(collection, {}).get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= self._clock.now():
            del self._collections[collection][key]
            self._record_delete(collection, key)
            return None
        return entry

    def _store(self, collection: str, key: str, payload: str, expires_at: float | None) -> None:
        entry = _Entry(payload, expires_at)
        self._collections.setdefault(collection, {})[key] = entry
        self._record_put(collection, key, entry)

    # --------------------------------------------------------------------- API
    def put(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> None:
        payload = serialize(document)
        with self._lock:
            self._store(collection, key, payload, self._expiry(ttl_seconds))

    def get(self, collection: str, key: str) -> Document | None:
        with self._lock:
            entry = self._live_entry(collection, key)
            return None if entry is None else json.loads(entry.payload)

    def delete(self, collection: str, key: str) -> bool:
        with self._lock:
            existed = self._live_entry(collection, key) is not None
            if existed:
                del self._collections[collection][key]
                self._record_delete(collection, key)
            return existed

    def set_if_absent(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> bool:
        payload = serialize(document)
        with self._lock:
            if self._live_entry(collection, key) is not None:
                return False
            self._store(collection, key, payload, self._expiry(ttl_seconds))
            return True

    def increment(self, collection: str, key: str, amount: int = 1, ttl_seconds: float | None = None) -> int:
        with self._lock:
            entry = self._live_entry(collection, key)
            current = 0 if entry is None else int(json.loads(entry.payload)["value"])
            updated = current + amount
            expires_at = entry.expires_at if entry is not None else self._expiry(ttl_seconds)
            self._store(collection, key, serialize({"value": updated}), expires_at)
            return updated

    def find(self, collection: str, predicate: Callable[[Document], bool] | None = None) -> list[Document]:
        with self._lock:
            result: list[Document] = []
            for key in list(self._collections.get(collection, {})):
                entry = self._live_entry(collection, key)
                if entry is None:
                    continue
                document = json.loads(entry.payload)
                if predicate is None or predicate(document):
                    result.append(document)
            return result

    def keys(self, collection: str) -> list[str]:
        with self._lock:
            return [k for k in list(self._collections.get(collection, {})) if self._live_entry(collection, k)]

    def add_to_set(self, collection: str, key: str, member: str) -> bool:
        with self._lock:
            members = self._sets.setdefault((collection, key), set())
            before = len(members)
            members.add(member)
            return len(members) > before

    def remove_from_set(self, collection: str, key: str, member: str) -> bool:
        with self._lock:
            members = self._sets.get((collection, key))
            if members is None or member not in members:
                return False
            members.discard(member)
            if not members:
                del self._sets[(collection, key)]
            return True

    def set_members(self, collection: str, key: str) -> set[str]:
        with self._lock:
            return set(self._sets.get((collection, key), ()))

    def purge_expired(self) -> int:
        with self._lock:
            removed = 0
            for collection in list(self._collections):
                for key in list(self._collections[collection]):
                    if self._live_entry(collection, key) is None:
                        removed += 1
            return removed

    # ------------------------------------------------------------ for subclasses
    def _snapshot(self) -> list[tuple[str, str, _Entry]]:
        """Every live entry (used by compaction)."""
        with self._lock:
            live: list[tuple[str, str, _Entry]] = []
            for collection, items in self._collections.items():
                for key in list(items):
                    entry = self._live_entry(collection, key)
                    if entry is not None:
                        live.append((collection, key, entry))
            return live

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {name: len(items) for name, items in self._collections.items()}
