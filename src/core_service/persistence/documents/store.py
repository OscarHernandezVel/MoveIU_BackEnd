"""Non-relational document store contract.

Documents are schema-less JSON objects grouped in *collections*, addressed by key, with
optional time-to-live. The core service uses it for data whose shape evolves quickly or
that is naturally short-lived: live bus state, the on-screen alert inbox, refresh-token
allow-list, de-duplication keys, AI run traces and ML training samples.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

Document = dict[str, Any]


class DocumentStore(ABC):
    @abstractmethod
    def put(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> None:
        """Insert or replace a document."""

    @abstractmethod
    def get(self, collection: str, key: str) -> Document | None:
        """Return a copy of the document, or ``None`` when missing or expired."""

    @abstractmethod
    def delete(self, collection: str, key: str) -> bool:
        """Delete a document; returns whether it existed."""

    @abstractmethod
    def set_if_absent(self, collection: str, key: str, document: Document, ttl_seconds: float | None = None) -> bool:
        """Atomically insert only when the key is free. Returns True when this call inserted it."""

    @abstractmethod
    def increment(self, collection: str, key: str, amount: int = 1, ttl_seconds: float | None = None) -> int:
        """Atomically add ``amount`` to the counter stored at ``key`` and return the new value."""

    @abstractmethod
    def find(self, collection: str, predicate: Callable[[Document], bool] | None = None) -> list[Document]:
        """Return copies of every live document of the collection matching ``predicate``."""

    @abstractmethod
    def keys(self, collection: str) -> list[str]:
        """Return the live keys of a collection."""

    @abstractmethod
    def add_to_set(self, collection: str, key: str, member: str) -> bool:
        """Add ``member`` to the set stored at ``key``. Returns True when it was not there yet."""

    @abstractmethod
    def remove_from_set(self, collection: str, key: str, member: str) -> bool:
        """Remove ``member`` from the set; returns whether it was there."""

    @abstractmethod
    def set_members(self, collection: str, key: str) -> set[str]:
        """Members of the set stored at ``key`` (empty when it does not exist)."""

    @abstractmethod
    def purge_expired(self) -> int:
        """Physically remove expired documents; returns how many were removed."""

    def get_many(self, collection: str, keys: list[str]) -> dict[str, Document]:
        """Documents of several keys at once (missing or expired keys are left out).

        Stores with a network round-trip per call override it with a single batched read."""
        found: dict[str, Document] = {}
        for key in keys:
            document = self.get(collection, key)
            if document is not None:
                found[key] = document
        return found

    def exists(self, collection: str, key: str) -> bool:
        return self.get(collection, key) is not None

    def ping(self) -> bool:
        """Cheap health probe used by the readiness check."""
        probe = "__health__"
        self.put("_health", probe, {"ok": True}, ttl_seconds=5)
        return self.get("_health", probe) is not None
